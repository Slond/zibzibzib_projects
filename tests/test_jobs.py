"""Exercise real auth, revisions, ownership, file bytes, sharing and deletion in isolation."""
import os
from pathlib import Path
import subprocess
import sys


def test_job_search_end_to_end(tmp_path):
    script = r'''
import asyncio, copy
import httpx
from sqlalchemy import select, text
from app.main import app
from app.auth import create_session_token, serializer, delete_user, revoke_service_access, grant_service_access
from app.database import User, Service, UserServiceAccess, JobSearchFile, JobSearchWorkspace, async_session, init_db, engine
from app.services.logging_utils import redact_tokens

async def check():
    await init_db(); await init_db()
    async with async_session() as db:
        service = await db.scalar(select(Service).where(Service.slug=='jobs'))
        assert service and service.route=='/jobs'
        users=[User(email=f'jobs-{i}@example.test',password_hash='unused',name='<script>test</script>',must_change_password=False) for i in range(3)]
        db.add_all(users);await db.flush()
        db.add_all(UserServiceAccess(user_id=u.id,service_id=service.id) for u in users[:2]);await db.commit()
        owner,other,denied=users
    transport=httpx.ASGITransport(app=app)
    def client(user=None):
        return httpx.AsyncClient(transport=transport,base_url='http://test',cookies={'session':create_session_token(user)} if user else {},headers={'x-csrf-token':serializer.dumps({'csrf':user.session_key})} if user else {})
    async with client(owner) as a,client(other) as b,client(denied) as no,client() as anon:
        assert (await anon.get('/jobs')).status_code==302
        assert (await anon.get('/jobs/api/state')).status_code==401
        assert (await no.get('/jobs/api/state')).status_code==403
        page=await a.get('/jobs');assert page.status_code==200
        assert 'Northstar' not in page.text and 'ДЕМО' not in page.text
        assert '<script>test</script>' not in page.text
        assert '/jobs' in (await a.get('/')).text
        initial=(await a.get('/jobs/api/state')).json();assert initial['revision']==0 and initial['state']['jobs']==[]
        assert (await a.put('/jobs/api/state',json={'revision':0,'state':initial['state']},headers={'x-csrf-token':''})).status_code==403
        assert (await a.post('/jobs/api/files?name=cv.pdf',content=b'x',headers={'origin':'https://evil.test'})).status_code==403
        raw=b'%PDF-1.4\noriginal bytes\x00\xff'
        file=(await a.post('/jobs/api/files?name=CV%20%D0%A0%D0%A3.pdf',content=raw)).json();fid=file['id']
        second=(await a.post('/jobs/api/files?name=letter.docx',content=b'PK-docx-test')).json()
        assert file['primary'] and not file['shared']
        dl=await a.get(f'/jobs/api/files/{fid}/download');assert dl.content==raw
        assert 'attachment' in dl.headers['content-disposition'] and '.pdf' in dl.headers['content-disposition']
        assert dl.headers['cache-control']=='private, no-store' and dl.headers['content-security-policy'].startswith('sandbox')
        for method,path in [('GET',f'/jobs/api/files/{fid}/download'),('POST',f'/jobs/api/files/{fid}/share'),('DELETE',f'/jobs/api/files/{fid}')]:
            assert (await b.request(method,path)).status_code==404
        assert (await a.post('/jobs/api/files?name=x.exe',content=b'x')).status_code==422
        assert (await a.post('/jobs/api/files?name=x.pdf',content=b'')).status_code==422
        assert (await a.post('/jobs/api/files?name=x.pdf',content=b'x'*(10*1024*1024+1))).status_code==413
        assert (await a.post(f'/jobs/api/files/{second["id"]}/primary')).status_code==200
        files=(await a.get('/jobs/api/state')).json()['state']['files'];assert sum(f['primary'] for f in files)==1
        assert next(f for f in files if f['id']==second['id'])['primary']
        doc={k:v for k,v in initial['state'].items() if k!='files'}
        doc['jobs']=[{'id':'job-a','company':'Own company','role':'Backend','status':'applied','applied':'2026-10-15','cv':fid,'history':[{'date':'2026-10-15','status':'applied'}],'next':{'title':'Follow up','date':'2026-10-22'}}]
        doc['events']=[{'id':'event-a','job':'job-a','title':'Interview','type':'interview','date':'2026-10-18','time':'14:00','duration':60}]
        assert (await a.put('/jobs/api/state',json={'revision':0,'state':doc})).status_code==200
        assert (await a.put('/jobs/api/state',json={'revision':0,'state':doc})).status_code==409
        assert (await b.put('/jobs/api/state',json={'revision':0,'state':doc})).status_code==422
        bad=copy.deepcopy(doc);bad['events'][0]['job']='foreign';assert (await a.put('/jobs/api/state',json={'revision':1,'state':bad})).status_code==422
        bad=copy.deepcopy(doc);bad['jobs'][0]['url']='javascript:alert(1)';assert (await a.put('/jobs/api/state',json={'revision':1,'state':bad})).status_code==422
        writes=await asyncio.gather(*[a.put('/jobs/api/state',json={'revision':1,'state':doc}) for _ in range(2)])
        assert sorted(r.status_code for r in writes)==[200,409]
        saved=(await a.get('/jobs/api/state')).json();assert saved['revision']==2 and saved['state']['jobs'][0]['cv']==fid
        assert (await b.get('/jobs/api/state')).json()['state']['jobs']==[]
        shared=(await a.post(f'/jobs/api/files/{fid}/share')).json();url=shared['share_url']
        assert (await a.post(f'/jobs/api/files/{fid}/share')).json()['share_url']==url
        assert (await anon.get(url)).content==raw
        assert redact_tokens(url)=='/jobs/shared/[redacted]'
        assert (await a.delete(f'/jobs/api/files/{fid}/share')).status_code==200
        assert (await anon.get(url)).status_code==404
        new_url=(await a.post(f'/jobs/api/files/{fid}/share')).json()['share_url'];assert new_url!=url
        await revoke_service_access(owner.id,service.id);assert (await anon.get(new_url)).status_code==404
        await grant_service_access(owner.id,service.id)
        deleted=await a.delete(f'/jobs/api/files/{fid}');assert deleted.status_code==200
        after=(await a.get('/jobs/api/state')).json();assert after['revision']==3 and after['state']['jobs'][0]['cv']==''
        assert (await anon.get(new_url)).status_code==404
        assert (await a.put('/jobs/api/state',json={'revision':2,'state':doc})).status_code in (409,422)
    await delete_user(owner.id)
    async with async_session() as db:
        assert await db.get(JobSearchWorkspace,owner.id) is None
        assert (await db.scalars(select(JobSearchFile).where(JobSearchFile.user_id==owner.id))).all()==[]
        assert (await db.execute(text('PRAGMA foreign_key_check'))).all()==[]
    await engine.dispose()
asyncio.run(check())
'''
    env = {**os.environ, "SECRET_KEY": "jobs-regression-only", "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/jobs.db",
           "ADMIN_EMAIL": "unused@example.test", "ADMIN_PASSWORD": "test-only-password", "YANDEX_TOKEN": "",
           "OPENEXCHANGERATES_APP_ID": "", "AWG_DB": str(tmp_path / "awg.db"), "ACTIVITY_DIR": str(tmp_path / "activity")}
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
