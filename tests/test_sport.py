"""Authenticated training, conflicting saves, validation and migration preservation."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_training_workspace(tmp_path):
    script = r'''
import asyncio, copy
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
