"""Authenticated training, conflicting saves, validation and migration preservation."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_training_workspace(tmp_path):
    script = r'''
import asyncio, copy, json, re
from datetime import date, timedelta
import httpx
from sqlalchemy import select, text
from app.main import app
from app.auth import create_session_token, serializer, delete_user
from app.database import User, Service, UserServiceAccess, SportWorkspace, async_session, init_db, engine
from app.services.sport import today

async def check():
    await init_db(); await init_db()
    async with async_session() as db:
        service=await db.scalar(select(Service).where(Service.slug=='sport'))
        nutrition=await db.scalar(select(Service).where(Service.slug=='nutrition'))
        assert service.route=='/sport' and nutrition.route=='/nutrition'
        users=[User(email=f'sport-{i}@example.test',password_hash='unused',name='<script>test</script>',must_change_password=False) for i in range(3)]
        db.add_all(users); await db.flush()
        db.add_all([UserServiceAccess(user_id=u.id,service_id=service.id) for u in users[:2]])
        db.add(UserServiceAccess(user_id=users[0].id,service_id=nutrition.id)); await db.commit()
        owner,other,denied=users
    def client(user=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',cookies={'session':create_session_token(user)} if user else {},headers={'x-csrf-token':serializer.dumps({'csrf':user.session_key})} if user else {})
    async with client(owner) as a,client(other) as b,client(denied) as no,client() as anon:
        assert (await anon.get('/sport')).status_code==302
        assert (await anon.get('/nutrition')).status_code==302
        assert (await anon.get('/sport/api/state')).status_code==401
        assert (await no.get('/sport/api/state')).status_code==403
        assert (await a.get('/nutrition')).status_code==200
        assert (await b.get('/nutrition')).status_code==302
        menu=(await a.get('/')).text
        assert '/sport' in menu and '/nutrition' in menu
        page=await a.get('/sport'); assert page.status_code==200
        assert '<script>test</script>' not in page.text and 'global-navigation' not in page.text
        assert 'private, no-store'==page.headers['cache-control']
        boot=json.loads(re.search(r'<script id="sport-boot" type="application/json">(.*?)</script>',page.text,re.S).group(1))
        assert len(boot['catalog'])==1133 and boot['catalog']['front']['name']=='Фронтальный присед'
        shared_id=next(key for key,row in boot['catalog'].items() if 'One Leg Dumbbell Squat' in row['en'])
        initial=(await a.get('/sport/api/state')).json()
        assert initial['today']==today() and initial['revision']==0 and initial['state']['sessions']=={}
        doc=initial['state']; d=today()
        doc['sessions'][d]={'template':'home','originalTemplate':'lower','status':'partial','sets':{'squat':[{'weight':'0','reps':'12','done':True},{'weight':'','reps':'','done':False},{'weight':'','reps':'','done':False}]},'duration':18,'note':'Personal note <script>alert(1)</script>','revisions':['Зал → дом']}
        payload={'revision':0,'state':doc}
        assert (await a.put('/sport/api/state',json=payload,headers={'x-csrf-token':''})).status_code==403
        assert (await a.put('/sport/api/state',json=payload,headers={'origin':'https://evil.test'})).status_code==403
        assert (await a.put('/sport/api/state',json=payload)).status_code==200
        assert (await a.put('/sport/api/state',json=payload)).status_code==409
        assert (await b.get('/sport/api/state')).json()['state']['sessions']=={}
        doc=(await a.get('/sport/api/state')).json()['state']
        assert doc['sessions'][d]['duration']==18
        undo=copy.deepcopy(doc); undo['undo']=None
        doc['undo']={'snapshot':undo,'label':'Previous change'}
        writes=await asyncio.gather(*[a.put('/sport/api/state',json={'revision':1,'state':doc}) for _ in range(2)])
        assert sorted(r.status_code for r in writes)==[200,409]
        async def reject(change):
            bad=copy.deepcopy(doc);change(bad)
            r=await a.put('/sport/api/state',json={'revision':2,'state':bad})
            assert r.status_code==422,r.text
        await reject(lambda v:v['sessions'][d].update(status='done'))
        await reject(lambda v:v['sessions'][d]['sets']['squat'][0].update(reps=''))
        await reject(lambda v:v['sessions'][d]['sets']['squat'][0].update(weight='NaN'))
        await reject(lambda v:v['sessions'][d].update(customIds=['squat','squat']))
        await reject(lambda v:v['sessions'][d].update(template='unknown'))
        await reject(lambda v:v['sessions'].update({'2026-02-30':v['sessions'][d]}))
        tomorrow=(date.fromisoformat(d)+timedelta(days=1)).isoformat()
        await reject(lambda v:v['sessions'].update({tomorrow:v['sessions'][d]}))
        await reject(lambda v:v.update(user_id=other.id))
        assert (await a.put('/sport/api/state',content=b'x'*(4*1024*1024+1),headers={'content-type':'application/json'})).status_code==413
        assert (await a.get('/sport/api/state')).json()['revision']==2
        custom={'title':'Дом и спина <script>example</script>','place':'Дома','minutes':25,'desc':'Своя тренировка','exercises':[{'id':'bridge','sets':1,'reps':'12–15','rest':45,'note':'Пауза'},{'id':'row','sets':2,'reps':'10','rest':60,'note':''}]}
        doc['templates']['own_workout']=copy.deepcopy(custom)
        previous=(date.fromisoformat(d)-timedelta(days=1)).isoformat()
        doc['sessions'][previous]={'template':'own_workout','originalTemplate':'own_workout','plan':copy.deepcopy(custom),'originalPlan':copy.deepcopy(custom),'status':'done','duration':25,'sets':{'bridge':[{'weight':'0','reps':'15','done':True}],'row':[{'weight':'10','reps':'10','done':True},{'weight':'10','reps':'10','done':True}]}}
        assert (await a.put('/sport/api/state',json={'revision':2,'state':doc})).status_code==200
        assert 'own_workout' not in (await b.get('/sport/api/state')).json()['state']['templates']
        doc=(await a.get('/sport/api/state')).json()['state']
        doc['templates']['own_workout']['title']='Новая версия'
        doc['templates']['own_workout']['exercises']=[{'id':'squat','sets':4,'reps':'8–10','rest':90}]
        assert (await a.put('/sport/api/state',json={'revision':3,'state':doc})).status_code==200
        latest=(await a.get('/sport/api/state')).json()['state']
        assert latest['sessions'][previous]['plan']==custom
        assert latest['sessions'][previous]['sets']['bridge'][0]['reps']=='15'
        assert latest['templates']['own_workout']['exercises'][0]['sets']==4
        bad=copy.deepcopy(latest);bad['sessions'][previous]['sets']['row'].pop()
        assert (await a.put('/sport/api/state',json={'revision':4,'state':bad})).status_code==422
        bad=copy.deepcopy(latest);bad['templates']['own_workout']['exercises'][0]['reps']='15–8'
        assert (await a.put('/sport/api/state',json={'revision':4,'state':bad})).status_code==422
        latest['preferences'].update(days=[0],week={'0':'own_workout'},scheduleStart=d,scheduleEnd=(date.fromisoformat(d)+timedelta(days=27)).isoformat())
        assert (await a.put('/sport/api/state',json={'revision':4,'state':latest})).status_code==200
        assert (await a.post('/sport/api/exercises/import',json={'name':'Тест','url':'file:///etc/passwd'})).status_code==422
        assert (await a.post('/sport/api/exercises/import',json={'name':'Тест','url':'https://evil.test/exercises/squat'})).status_code==422
        assert (await no.post('/sport/api/exercises/import',json={'name':'Тест','url':'https://evil.test/exercises/squat'})).status_code==403
        latest=(await a.get('/sport/api/state')).json()['state']
        latest['exercises']['split_own']={'name':'Болгарские выпады','muscle':'Ноги','equipment':'Гантели','instructions':'Своя инструкция','videoUrl':'https://www.youtube.com/watch?v=abcdefghijk'}
        future=(date.fromisoformat(d)+timedelta(days=2)).isoformat()
        oldplan=copy.deepcopy(latest['templates']['own_workout'])
        latest['sessions'][future]={'template':'own_workout','originalTemplate':'own_workout','plan':copy.deepcopy(oldplan),'originalPlan':copy.deepcopy(oldplan),'status':'planned','sets':{},'weightUnits':{'squat':'lbs'},'manual':True,'note':'Сохрани меня'}
        assert (await a.put('/sport/api/state',json={'revision':5,'state':latest})).status_code==200
        latest=(await a.get('/sport/api/state')).json()['state']
        assert latest['exercises']['split_own']['name']=='Болгарские выпады'
        assert not (await b.get('/sport/api/state')).json()['state']['exercises']
        latest['templates']['own_workout']['exercises']=[{'id':'split_own','sets':2,'reps':'10','rest':60}]
        assert (await a.put('/sport/api/state',json={'revision':6,'state':latest})).status_code==200
        latest=(await a.get('/sport/api/state')).json()['state']
        assert latest['sessions'][future]['plan']['exercises'][0]['id']=='split_own'
        assert latest['sessions'][future]['weightUnits']=={}
        assert latest['sessions'][future]['originalPlan']==oldplan
        assert latest['sessions'][future]['note']=='Сохрани меня'
        assert latest['sessions'][previous]['plan']==custom
        del latest['templates']['own_workout']; latest['deletedTemplates'].append('own_workout')
        latest['preferences'].update(days=[],week={})
        assert (await a.put('/sport/api/state',json={'revision':7,'state':latest})).status_code==200
        reloaded=(await a.get('/sport/api/state')).json()['state']
        assert 'own_workout' not in reloaded['templates']
        assert reloaded['sessions'][previous]['plan']==custom
        assert reloaded['sessions'][future]['plan']['exercises'][0]['id']=='split_own'
        reloaded['templates']['shared_workout']={'title':'Из общего каталога','place':'В зале','minutes':20,'exercises':[{'id':shared_id,'sets':2,'reps':'10','rest':60}]}
        assert (await a.put('/sport/api/state',json={'revision':8,'state':reloaded})).status_code==200
        common=(await a.get('/sport/api/state')).json()['state']
        assert common['templates']['shared_workout']['exercises'][0]['id']==shared_id
        assert shared_id not in common['exercises']
        assert len(common['exercises'])==1  # The personal exercise remains separate and intact.
        # Session-specific set count and entry units survive a save/reload; weights stay kg.
        training=common['sessions'][d]
        original=copy.deepcopy(training['originalPlan'])
        template=copy.deepcopy(common['templates']['home'])
        training['status']='started'
        training['plan']['exercises'][0]['sets']=4
        training['sets']['squat'].append({'weight':'','reps':'','done':False})
        training['sets']['squat'][1]={'weight':'45.359237','reps':'10','done':True}  # 100 lbs
        training['weightUnits']={'squat':'lbs'}
        assert (await a.put('/sport/api/state',json={'revision':9,'state':common})).status_code==200
        saved=(await a.get('/sport/api/state')).json()['state']
        assert saved['sessions'][d]['weightUnits']=={'squat':'lbs'}
        assert saved['sessions'][d]['sets']['squat'][1]['weight']=='45.359237'
        assert saved['sessions'][d]['originalPlan']==original
        assert saved['templates']['home']==template
        saved['sessions'][d]['plan']['exercises'][0]['sets']=3
        saved['sessions'][d]['sets']['squat'].pop()
        assert (await a.put('/sport/api/state',json={'revision':10,'state':saved})).status_code==200
        saved=(await a.get('/sport/api/state')).json()['state']
        assert len(saved['sessions'][d]['sets']['squat'])==3
        assert saved['sessions'][d]['sets']['squat'][1]['weight']=='45.359237'
        for units in [{'squat':'pounds'},{'unknown':'lbs'}]:
            bad=copy.deepcopy(saved);bad['sessions'][d]['weightUnits']=units
            assert (await a.put('/sport/api/state',json={'revision':11,'state':bad})).status_code==422
    await delete_user(owner.id)
    async with async_session() as db:
        assert await db.get(SportWorkspace,owner.id) is None
        assert (await db.execute(text('PRAGMA foreign_key_check'))).all()==[]
    await engine.dispose()
asyncio.run(check())
'''
    env = {**os.environ, "SECRET_KEY": "sport-regression-only", "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/sport.db",
           "ADMIN_EMAIL": "unused@example.test", "ADMIN_PASSWORD": "test-only-password", "YANDEX_TOKEN": "",
           "OPENEXCHANGERATES_APP_ID": "", "AWG_DB": str(tmp_path / "awg.db"), "ACTIVITY_DIR": str(tmp_path / "activity")}
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sport_migration_preserves_workspaces(tmp_path):
    path = tmp_path / "migration.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{path}"}
    def upgrade(target):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stdout + result.stderr
    upgrade("005_job_file_comments")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(id,email,password_hash,session_key) VALUES(9,'migration@example.test','unused','only-test')")
        original='{"version":1,"jobs":[],"events":[],"goal":4}'
        db.execute("INSERT INTO job_search_workspaces VALUES(9,7,?,CURRENT_TIMESTAMP)", (original,))
        before=db.execute("SELECT id,name,slug,route FROM services ORDER BY id").fetchall()
    upgrade("head");upgrade("head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT revision,document FROM job_search_workspaces").fetchone()==(7,original)
        assert db.execute("SELECT id,name,slug,route FROM services ORDER BY id").fetchall()[:len(before)]==before
        assert db.execute("SELECT COUNT(*) FROM services WHERE slug IN ('sport','nutrition')").fetchone()==(2,)
        assert db.execute("SELECT COUNT(*) FROM sport_workspaces").fetchone()==(0,)
        assert db.execute("PRAGMA integrity_check").fetchall()==[("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall()==[]


def test_legacy_program_upgrades_without_rewriting_workouts():
    import copy
    from app.services.sport import SportState, upgrade_workspace

    legacy = {"version": 1, "preferences": {"goal": "Своя цель", "time": "13:00", "days": [1, 2, 3, 4, 5, 6]},
              "sessions": {"2026-10-01": {"template": "lower", "originalTemplate": "lower", "status": "planned", "sets": {}, "customIds": ["squat", "rdl"]}}, "events": [], "undo": None}
    before = copy.deepcopy(legacy)
    upgraded = upgrade_workspace(legacy)
    assert legacy == before
    assert upgraded["preferences"]["time"] == "13:00"
    assert upgraded["preferences"]["week"] == {"1": "upper", "2": "lower", "3": "lower", "4": "lower", "5": "lower", "6": "lower"}
    assert [e["id"] for e in upgraded["sessions"]["2026-10-01"]["plan"]["exercises"]] == ["squat", "rdl"]
    upgraded["templates"]["lower"]["exercises"].pop()
    assert len(upgraded["sessions"]["2026-10-01"]["originalPlan"]["exercises"]) == 5
    assert upgrade_workspace(upgraded) == upgraded
    assert SportState.model_validate(upgraded).sessions["2026-10-01"].template == "lower"


def test_exercise_catalog_and_future_template_changes():
    import copy
    import pytest
    from app.services.sport import SportState, empty_workspace, refresh_future_workouts, upgrade_workspace

    original = empty_workspace()
    custom = {'name': 'Болгарские выпады <b>тест</b>', 'muscle': 'Ноги', 'equipment': 'Гантели',
              'instructions': 'Свой текст', 'sourceUrl': 'https://www.muscleandstrength.com/exercises/example.html'}
    original['exercises']['own_split'] = custom
    plan = copy.deepcopy(original['templates']['lower'])
    for day in ['2026-10-16', '2026-10-17', '2026-10-18', '2026-10-19', '2026-10-20', '2026-10-21']:
        original['sessions'][day] = {'template': 'lower', 'originalTemplate': 'lower', 'status': 'planned',
                                     'plan': copy.deepcopy(plan), 'originalPlan': copy.deepcopy(plan),
                                     'sets': {}, 'manual': True, 'note': 'Сохранить заметку'}
    original['sessions']['2026-10-19']['startedAt'] = 123
    original['sessions']['2026-10-20']['sets']['front'] = [dict(weight='10', reps='', done=False)] + [dict(weight='', reps='', done=False) for _ in range(2)]
    original['sessions']['2026-10-21'].update(status='moved', movedTo='2026-10-22')
    updated = copy.deepcopy(original)
    updated['templates']['lower']['exercises'] = [{'id': 'own_split', 'sets': 2, 'reps': '8–10', 'rest': 60}]
    result = refresh_future_workouts(original, updated, '2026-10-17')
    assert result['sessions']['2026-10-18']['plan'] == updated['templates']['lower']
    assert result['sessions']['2026-10-18']['originalPlan'] == plan
    assert result['sessions']['2026-10-18']['note'] == 'Сохранить заметку'
    for day in ['2026-10-16', '2026-10-17', '2026-10-19', '2026-10-20', '2026-10-21']:
        assert result['sessions'][day] == original['sessions'][day]
    assert updated['sessions']['2026-10-18']['plan'] == plan  # Pure transformation.
    SportState.model_validate(result)
    for change in [lambda v:v['exercises']['own_split'].update(sourceUrl='javascript:alert(1)'),
                   lambda v:v['exercises'].update(press=custom),
                   lambda v:v['templates']['lower']['exercises'][0].update(id='nonexistent')]:
        bad = copy.deepcopy(result); change(bad)
        with pytest.raises(ValueError):
            SportState.model_validate(bad)
    del result['templates']['lower']
    result['deletedTemplates'].append('lower')
    result['preferences'].update(days=[1], week={'1': 'upper'})
    assert 'lower' not in upgrade_workspace(result)['templates']
    assert upgrade_workspace(upgrade_workspace(result)) == upgrade_workspace(result)
    SportState.model_validate(result)
    assert result['sessions']['2026-10-18']['plan']['exercises'][0]['id'] == 'own_split'


def test_muscle_strength_import_is_bounded_and_rejects_arbitrary_hosts():
    import asyncio
    import httpx
    import pytest
    from app.services.sport_lookup import exercise_url, import_exercise, parse_exercise

    url = 'https://www.muscleandstrength.com/exercises/example-squat.html'
    html = '''<html><head><meta property="og:image" content="https://www.muscleandstrength.com/images/squat.jpg"></head><body>
    <h1>Example Squat Video Exercise Guide</h1><iframe data-src="https://www.youtube.com/embed/abcdefghijk"></iframe>
    <ul><li>Target Muscle Group <a>Quads</a></li><li>Exercise Type Strength</li><li>Equipment Required Dumbbell</li><li>Mechanics Compound</li></ul>
    <h2>Example Squat Instructions</h2><ol><li>First step <script>alert('x')</script></li><li>Second step</li></ol>
    <h2>Example Squat Tips</h2><ul><li>Not an instruction</li></ul></body></html>'''
    definition = parse_exercise(html, 'Моё название', url)
    assert definition['name'] == 'Моё название' and definition['en'] == 'Example Squat'
    assert definition['muscle'] == 'Квадрицепсы' and definition['equipment'] == 'Гантели'
    assert definition['instructions'] == 'First step\nSecond step'
    assert definition['videoUrl'] == 'https://www.youtube.com/embed/abcdefghijk'
    for bad in ['http://www.muscleandstrength.com/exercises/squat', 'https://evil.test/exercises/squat',
                'https://www.muscleandstrength.com.evil.test/exercises/squat',
                'https://www.muscleandstrength.com@127.0.0.1/exercises/squat',
                'https://www.muscleandstrength.com:8443/exercises/squat',
                'https://www.muscleandstrength.com/exercises/../../admin',
                'https://www.muscleandstrength.com/exercises/squat?redirect=http://127.0.0.1']:
        with pytest.raises(ValueError):
            exercise_url(bad)
    async def check():
        hits = []
        def reply(request):
            hits.append(str(request.url));return httpx.Response(200, text=html)
        actual = await import_exercise('Моё название', url, transport=httpx.MockTransport(reply))
        assert actual == definition and hits == [url]
        with pytest.raises(ValueError, match='заблокировал'):
            await import_exercise('Тест', url, transport=httpx.MockTransport(lambda r:httpx.Response(403)))
        redirects = []
        def redirect(request):
            redirects.append(str(request.url));return httpx.Response(302, headers={'location':'http://127.0.0.1/private'})
        with pytest.raises(ValueError):
            await import_exercise('Тест', url, transport=httpx.MockTransport(redirect))
        assert redirects == [url]  # Never request a redirect outside the allowlist.
        with pytest.raises(ValueError, match='слишком большая'):
            await import_exercise('Тест', url, transport=httpx.MockTransport(lambda r:httpx.Response(200, content=b'x'*(2*1024*1024+1))))
        with pytest.raises(ValueError):
            await import_exercise('Тест', url, transport=httpx.MockTransport(lambda r:httpx.Response(200, text='<h1>Just a moment...</h1>')))
    asyncio.run(check())
