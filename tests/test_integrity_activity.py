"""End-to-end ingestion, revisions, lifecycle and stale-worker publication."""

import asyncio

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
import httpx
from sqlalchemy import select, func, text
from test_integrity import (
    WORK,
    run,
    init_db,
    async_session,
    app,
    User,
    create_session_token,
)
from app.database import (
    ActivityDevice,
    ActivityEvent,
    ActivitySettings,
    ActivityDay,
    ActivityPlace,
)
from app.services import activity
from app.services import activity_pipeline as pipeline


def test_activity_ingestion_and_day_lifecycle(monkeypatch):
    async def check():
        await init_db()
        async with async_session() as session:
            user = User(
                email="activity-test@example.test",
                password_hash="unused",
                is_admin=True,
                must_change_password=False,
            )
            stranger = User(
                email="activity-stranger@example.test",
                password_hash="unused",
                is_admin=True,
                must_change_password=False,
            )
            session.add_all([user, stranger])
            await session.flush()
            devices = [
                ActivityDevice(user_id=user.id, name="Same name", platform="mac"),
                ActivityDevice(user_id=user.id, name="Same name", platform="windows"),
                ActivityDevice(user_id=user.id, name="Phone", platform="iphone"),
            ]
            session.add_all(devices)
            await session.flush()
            settings = ActivitySettings(
                user_id=user.id,
                timezone="Asia/Almaty",
                sample_minutes=1,
                idle_minutes=3,
                retention_days=0,
            )
            session.add(settings)
            await session.commit()
            uid = user.id
            did = devices[0].id
            phone = devices[2]
            token = devices[0].token
        auth = await activity.authenticate_device(token)
        past = datetime.now(timezone.utc) - timedelta(days=2)
        events = [
            dict(
                client_event_id=uuid.uuid4().hex,
                captured_at=(past + timedelta(minutes=i)).isoformat(),
                app_name="Editor",
                window_title=f"file {i}",
                idle_seconds=0,
                sample_minutes=1,
                actor="auto",
            )
            for i in (2, 0, 1)
        ]
        headers = {"Authorization": "Bearer " + token, "X-Activity-Client-Version": "2"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client:
            assert (await client.get("/activity/api/config")).json()["device_id"] == did
            response = await client.post("/activity/api/batch", json={"events": events})
            assert response.status_code == 200, response.text
            assert all(r["status"] == "ok" for r in response.json()["results"])
            repeated = await client.post("/activity/api/batch", json={"events": events})
            assert repeated.json() == response.json()
            assert (
                await client.post(
                    "/activity/api/batch",
                    headers={"Authorization": ""},
                    json={"events": events},
                )
            ).status_code == 401
            assert (
                await client.post("/activity/api/batch", json={"events": []})
            ).status_code == 400
            assert (
                await client.post("/activity/api/batch", content=b"{")
            ).status_code == 400
            assert (
                await client.post(
                    "/activity/api/batch", content=b"x" * (512 * 1024 + 1)
                )
            ).status_code == 413
            too_old = dict(
                events[0],
                client_event_id=uuid.uuid4().hex,
                captured_at=(past - timedelta(days=100)).isoformat(),
            )
            assert (
                await client.post("/activity/api/batch", json={"events": [too_old]})
            ).json()["results"][0]["status"] == "error"
            assert (
                await client.post(
                    "/activity/api/batch",
                    headers={"Authorization": "Bearer " + phone.token},
                    json={"events": events},
                )
            ).status_code == 404
        day = (
            past.astimezone(activity.zone_or_default("Asia/Almaty")).date().isoformat()
        )
        async with async_session() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ActivityEvent)
                    .where(ActivityEvent.user_id == uid)
                )
                == 3
            )
            job = await session.get(ActivityDay, (uid, day))
            assert job.revision == 1
            assert (await session.get(ActivityDevice, did)).client_version == "2"
            await session.commit()
        assert pipeline.process_day(WORK / "main.db", uid, day)
        async with async_session() as session:
            job = await session.get(ActivityDay, (uid, day))
            assert job.revision == job.processed_revision == 1
            assert json.loads(job.document)["source"] == "deterministic"
        # A late event invalidates a previously published historical day.
        late = dict(
            events[0],
            client_event_id=uuid.uuid4().hex,
            captured_at=(past + timedelta(minutes=4)).isoformat(),
        )
        await activity.record_screens(auth, [late])
        async with async_session() as session:
            job = await session.get(ActivityDay, (uid, day))
            assert job.revision == 2 and job.processed_revision == 1
        original = pipeline.assemble

        def assembly_with_concurrent_change(*args, **kw):
            doc = original(*args, **kw)
            with sqlite3.connect(WORK / "main.db") as con:
                con.execute(
                    "UPDATE activity_days SET revision=revision+1,document=NULL WHERE user_id=? AND day=?",
                    (uid, day),
                )
            return doc

        monkeypatch.setattr(pipeline, "assemble", assembly_with_concurrent_change)
        assert not pipeline.process_day(WORK / "main.db", uid, day)
        monkeypatch.setattr(pipeline, "assemble", original)
        assert pipeline.process_day(WORK / "main.db", uid, day)
        # Foreign users see their own empty day and cannot rename/delete ours.
        assert not await activity.rename_device(stranger.id, did, "wrong")
        assert not await activity.delete_device(stranger.id, did)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": create_session_token(stranger)},
        ) as client:
            assert (await client.get("/activity/status?date=" + day)).json()[
                "revision"
            ] == 0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": create_session_token(user)},
        ) as client:
            page = await client.get("/activity?date=" + day)
            assert page.status_code == 200 and "file 0" in page.text
            assert (await client.get("/activity/devices")).status_code == 200
            assert (await client.get("/activity/settings")).status_code == 200
        # Renaming only changes display text; identity and events remain intact.
        assert await activity.rename_device(uid, did, "Renamed")
        assert pipeline.process_day(WORK / "main.db", uid, day)
        async with async_session() as session:
            job = await session.get(ActivityDay, (uid, day))
            doc = json.loads(job.document)
            assert {r["device_id"] for r in doc["screen"]} == {did}
            assert {r["device"] for r in doc["screen"]} == {"Renamed"}
        # Default retention never silently erases existing history.
        await activity.purge_activity_history()
        async with async_session() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ActivityEvent)
                    .where(ActivityEvent.user_id == uid)
                )
                == 4
            )
        # Explicit retention removes raw events and their derived history.
        assert (
            await activity.update_settings(
                uid, "Asia/Almaty", "1", "3", "", retention_days="1"
            )
            is None
        )
        await activity.purge_activity_history()
        async with async_session() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ActivityEvent)
                    .where(ActivityEvent.user_id == uid)
                )
                == 0
            )
            assert await session.get(ActivityDay, (uid, day)) is None
        # Device deletion invalidates the cached current day as well as removing raw records.
        current = dict(
            late,
            client_event_id=uuid.uuid4().hex,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )
        await activity.record_screens(auth, [current])
        current_day = (
            datetime.now(activity.zone_or_default("Asia/Almaty")).date().isoformat()
        )
        assert pipeline.process_day(WORK / "main.db", uid, current_day)
        assert await activity.delete_device(uid, did)
        assert pipeline.process_day(WORK / "main.db", uid, current_day)
        async with async_session() as session:
            job = await session.get(ActivityDay, (uid, current_day))
            assert not json.loads(job.document)["screen"]
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
        result = await activity.record_location(
            await activity.authenticate_device(phone.token), 43, 76, float("nan"), None
        )
        assert result.status == "error"

    run(check())


def test_event_and_invalidation_rollback_together(monkeypatch):
    async def check():
        await init_db()
        async with async_session() as session:
            user = User(
                email="activity-rollback@example.test",
                password_hash="unused",
                is_admin=True,
            )
            session.add(user)
            await session.flush()
            device = ActivityDevice(
                user_id=user.id, name="Rollback", platform="windows"
            )
            session.add(device)
            await session.commit()
            token = device.token
        auth = await activity.authenticate_device(token)
        unique = uuid.uuid4().hex

        async def crash(*args, **kwargs):
            raise RuntimeError("invalidation interrupted")

        monkeypatch.setattr(activity, "dirty_day", crash)
        try:
            await activity.record_screens(
                auth,
                [
                    dict(
                        client_event_id=unique,
                        captured_at=datetime.now(timezone.utc).isoformat(),
                        app_name="Editor",
                    )
                ],
            )
        except RuntimeError:
            pass
        else:
            assert False, "Expected simulated interruption"
        async with async_session() as session:
            assert (
                await session.scalar(
                    select(ActivityEvent.id).where(
                        ActivityEvent.client_event_id == unique
                    )
                )
                is None
            )

    run(check())


def test_concurrent_retries_create_one_event():
    async def check():
        await init_db()
        async with async_session() as session:
            user = User(
                email="activity-concurrent@example.test",
                password_hash="unused",
                is_admin=True,
            )
            session.add(user)
            await session.flush()
            device = ActivityDevice(user_id=user.id, name="Concurrent", platform="mac")
            session.add(device)
            session.add(ActivitySettings(user_id=user.id))
            await session.commit()
            token = device.token
        auth = await activity.authenticate_device(token)
        unique = uuid.uuid4().hex
        payload = dict(
            client_event_id=unique,
            captured_at=datetime.now(timezone.utc).isoformat(),
            app_name="Editor",
            sample_minutes=1,
        )
        a, b = await asyncio.gather(
            activity.record_screens(auth, [payload]),
            activity.record_screens(auth, [payload]),
        )
        assert a[0].status == b[0].status == "ok" and a[0].event_id == b[0].event_id
        async with async_session() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ActivityEvent)
                    .where(ActivityEvent.client_event_id == unique)
                )
                == 1
            )

    run(check())


def test_place_edits_keep_name_history_and_delete_clears_events():
    async def check():
        await init_db()
        async with async_session() as session:
            user = User(
                email="activity-place@example.test",
                password_hash="unused",
                is_admin=True,
            )
            session.add(user)
            await session.flush()
            device = ActivityDevice(user_id=user.id, name="Location", platform="iphone")
            place = ActivityPlace(
                user_id=user.id,
                name="Old",
                latitude=43,
                longitude=76,
                radius_m=80,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(days=2),
            )
            session.add_all([device, place])
            await session.flush()
            session.add(
                ActivityEvent(
                    user_id=user.id,
                    device_id=device.id,
                    place_id=place.id,
                    recorded_at=datetime.now(timezone.utc).replace(tzinfo=None)
                    - timedelta(days=1),
                    kind="location",
                    latitude=43,
                    longitude=76,
                )
            )
            await session.commit()
            uid = user.id
            pid = place.id
        assert (
            await activity.upsert_place(uid, pid, "New", "", "", "43", "76", "80")
            is None
        )
        async with async_session() as session:
            place = await session.get(ActivityPlace, pid)
            assert [label["name"] for label in json.loads(place.labels_json)] == [
                "Old",
                "New",
            ]
        assert await activity.delete_place(uid, pid)
        async with async_session() as session:
            assert await session.get(ActivityPlace, pid) is None
            assert (
                await session.scalar(
                    select(ActivityEvent.place_id).where(ActivityEvent.user_id == uid)
                )
                is None
            )
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []

    run(check())
