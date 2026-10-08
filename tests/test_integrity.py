"""Regression checks for access, migration, money and session integrity."""
import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

WORK = Path(tempfile.mkdtemp(prefix='zib-integrity-'))
os.environ.update(SECRET_KEY='regression-test-only-secret', DATABASE_URL=f'sqlite+aiosqlite:///{WORK}/main.db', ADMIN_EMAIL='test@example.test', ADMIN_PASSWORD='test-only-password', ACTIVITY_DIR=str(WORK / 'activity'), AWG_DB=str(WORK / 'awg.db'), OPENEXCHANGERATES_APP_ID='')
import httpx
from sqlalchemy import select, text
from app.main import app
from app.database import init_db, async_session, engine, User, FinanceAccount, FinanceEvent, FinanceEventMember, Service, UserServiceAccess, Transaction, Category, CategoryBudget, RecurringTransaction
from app.auth import create_session_token, serializer, update_user_password, delete_user, hash_password
from app.services import currency, activity
from app.services.scheduler import process_recurring_transactions
from app.services.recurrence import advance_run


def run(coro):
    return asyncio.run(coro)


def test_full_integrity_regression():
    async def check():
        await init_db()
        await init_db()  # migration is safe on restart
        async with async_session() as session:
            assert await session.scalar(text('PRAGMA foreign_keys')) == 1
            accounts = [FinanceAccount(name='A'), FinanceAccount(name='B')]
            session.add_all(accounts); await session.flush()
            users = [User(email=f'{i}@example.test', password_hash=hash_password('test-only-password'), finance_account_id=a.id, must_change_password=False) for i,a in enumerate(accounts)]
            session.add_all(users); await session.flush()
            service = Service(name='Finance', slug='finance', route='/finance'); session.add(service); await session.flush()
            session.add_all(UserServiceAccess(user_id=u.id, service_id=service.id) for u in users)
            events = [FinanceEvent(account_id=a.id, name=str(a.id), default_currency='USD') for a in accounts]
            session.add_all(events); await session.flush()
            a,b = users; own,other = events
            category = Category(account_id=a.finance_account_id,name='Food');session.add(category);await session.flush()
            session.add_all([CategoryBudget(category_id=category.id,event_id=None,monthly_limit=100,currency='USD'),CategoryBudget(category_id=category.id,event_id=own.id,monthly_limit=10,currency='USD')])
            rec = RecurringTransaction(event_id=own.id,amount=-3,currency='USD',frequency='monthly',anchor_day=31,next_run=datetime(2026,1,31,12))
            session.add(rec)
            # Existing unconverted money must never be treated as dollars.
            session.add(Transaction(account_id=accounts[0].id,event_id=own.id,amount=-1000,currency='KZT'))
            await session.commit()
            aid,bid,eid,fid,token = a.id,b.id,own.id,other.id,other.webhook_token
        csrf = serializer.dumps({'csrf':a.session_key})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=True),base_url='http://test',cookies={'session':create_session_token(a)}) as client:
            for path in [f'/finance/events/{fid}/settings',f'/finance/add?event={fid}',f'/finance/api/stats?event_id={fid}',f'/finance/export.csv?event_id={fid}']:
                assert (await client.get(path)).status_code == 404, path
            assert (await client.post(f'/finance/events/{fid}/archive',data={'csrf_token':csrf})).status_code == 404
            assert (await client.post('/finance/add',data={'amount':'-1','event_id':fid,'currency':'USD','csrf_token':csrf})).status_code == 404
            assert (await client.post('/finance/add',data={'amount':'-1','event_id':eid,'currency':'USD'})).status_code == 403
            response = await client.post('/finance/add',data={'amount':'-12','event_id':eid,'category':'Food','currency':'USD','csrf_token':csrf})
            assert response.status_code == 302
            assert 'budget_alert=1' in response.headers['location']
            stats = (await client.get('/finance/api/stats')).json()
            assert stats['expenses'] == 12 and stats['unconverted_count'] == 1
            await process_recurring_transactions(); await process_recurring_transactions()
            async with async_session() as session:
                txs=(await session.scalars(select(Transaction).where(Transaction.source=='recurring'))).all()
                assert len(txs)==1 and txs[0].amount_usd == -3 and txs[0].exchange_rate == 1
                session.add(FinanceEventMember(user_id=aid,event_id=fid,role='member'));await session.commit()
            # Members see transactions, but do not obtain management tokens.
            assert (await client.get(f'/finance/api/stats?event_id={fid}')).status_code == 200
            assert (await client.get(f'/finance/events/{fid}/settings')).status_code == 404
            async with async_session() as session:
                u=await session.get(User,aid);u.must_change_password=True;await session.commit()
            assert (await client.get('/finance/api/stats')).status_code == 403
            assert (await client.get('/weather')).headers['location'] == '/change-password'
            await update_user_password(aid,'another-test-password')
            assert (await client.get('/finance/api/stats')).status_code == 401
        folder=activity.breakdown_dir()/str(bid);folder.mkdir(parents=True);(folder/'2026-01-31.json').write_text('{}')
        await delete_user(bid)
        assert not folder.exists()
        async with async_session() as session:
            replacement=User(email='replacement@example.test',password_hash='unused');session.add(replacement);await session.commit()
            assert replacement.id > bid
            assert (await session.execute(text('PRAGMA foreign_key_check'))).all()==[]
        with pytest.raises(currency.CurrencyRateUnavailable):
            await currency.get_exchange_rate('XYZ','USD')
        with pytest.raises(currency.CurrencyRateUnavailable):
            await currency.convert_to_usd(Decimal('NaN'),'USD')
        await engine.dispose()
    run(check())


def test_monthly_anchor_and_invalid_interval():
    january=datetime(2026,1,31,12,tzinfo=timezone.utc)
    february=advance_run(january,'monthly',anchor_day=31)
    assert february.day==28
    assert advance_run(february,'monthly',anchor_day=31).day==31
    with pytest.raises(ValueError): advance_run(january,'every_n_days',0)


def test_migrate_legacy_database():
    legacy=WORK/'legacy.db'
    con=sqlite3.connect(legacy)
    con.executescript("CREATE TABLE finance_accounts(id INTEGER PRIMARY KEY,name VARCHAR NOT NULL,created_at DATETIME); CREATE TABLE users(id INTEGER PRIMARY KEY,email VARCHAR NOT NULL,password_hash VARCHAR NOT NULL,name VARCHAR,is_admin BOOLEAN,must_change_password BOOLEAN,finance_account_id INTEGER,created_at DATETIME); CREATE TABLE transactions(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,amount NUMERIC NOT NULL,category VARCHAR,description VARCHAR,timestamp DATETIME,source VARCHAR,created_by_user_id INTEGER); CREATE TABLE finance_categories(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,name VARCHAR NOT NULL,icon VARCHAR,usage_count INTEGER); INSERT INTO finance_accounts(id,name) VALUES(1,'legacy'); INSERT INTO users(id,email,password_hash,finance_account_id) VALUES(1,'legacy@test','unused',1); INSERT INTO transactions(id,account_id,amount) VALUES(1,1,-1500);")
    con.close()
    result=subprocess.run([sys.executable,'-m','alembic','upgrade','head'],env={**os.environ,'DATABASE_URL':f'sqlite+aiosqlite:///{legacy}'},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    con=sqlite3.connect(legacy)
    assert con.execute('SELECT amount,event_id,currency FROM transactions').fetchone()==(-1500,1,'KZT')
    assert con.execute('SELECT session_key FROM users').fetchone()[0]
    assert 'AUTOINCREMENT' in con.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
    assert con.execute('PRAGMA foreign_key_check').fetchall()==[]
    con.close()


def test_access_logs_hide_tokens():
    from app.services.logging_utils import redact_tokens
    assert redact_tokens('/activity/api/ingest/private-token') == '/activity/api/ingest/[redacted]'
    assert redact_tokens('/finance/api/webhook/private-token/categories') == '/finance/api/webhook/[redacted]/categories'
    assert redact_tokens('https://provider.test/?app_id=private-key&currency=USD') == 'https://provider.test/?app_id=[redacted]&currency=USD'
