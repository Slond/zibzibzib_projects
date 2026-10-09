"""Shared pure assembly and SQLite worker; the web process never writes day files."""

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from app.services.activity_day import GeoPoint, PlaceSignature, ScreenSample, merge_day


def utc(value):
    parsed = (
        datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
    )
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def bounds(day, tz):
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(timezone.utc).replace(tzinfo=None), (
        start + timedelta(days=1)
    ).astimezone(timezone.utc).replace(tzinfo=None)


def signatures_from_places(places):
    result = []
    for p in places:
        labels = []
        if p.get("labels_json"):
            try:
                labels = json.loads(p["labels_json"])
            except (ValueError, TypeError):
                pass
        labels = labels or [
            dict(name=p["name"], at=str(p.get("created_at") or "1970-01-01"))
        ]
        for label in labels:
            result.append(
                PlaceSignature(
                    label["name"],
                    p.get("latitude"),
                    p.get("longitude"),
                    p.get("wifi_ssid"),
                    utc(label["at"]),
                    radius_m=p.get("radius_m") or 80,
                )
            )
    return result


def assemble(day, settings, rows, places, now=None):
    tz = ZoneInfo(settings.get("timezone") or "Asia/Almaty")
    skip = [
        s.strip().casefold()
        for s in (settings.get("skip_apps") or "").splitlines()
        if s.strip()
    ]
    screens = []
    points = []
    for row in rows:
        at = utc(row["recorded_at"])
        if row["kind"] == "location":
            points.append(
                GeoPoint(at, row["latitude"], row["longitude"], row.get("accuracy_m"))
            )
        elif row["kind"] == "screen":
            app = row.get("app_name") or ""
            sensitive = any(
                app.casefold() == s or (len(s) >= 6 and s in app.casefold())
                for s in skip
            )
            screens.append(
                ScreenSample(
                    at,
                    row["device_name"],
                    app,
                    "" if sensitive else (row.get("window_title") or ""),
                    row.get("wifi_ssid"),
                    row["device_id"],
                    row.get("idle_seconds"),
                    row.get("actor"),
                    row.get("sample_minutes"),
                )
            )
    document = merge_day(
        day,
        tz,
        points,
        screens,
        signatures_from_places(places),
        settings.get("sample_minutes") or 1,
        settings.get("idle_minutes") or 3,
        now=now or datetime.now(timezone.utc),
    )
    document.update(
        day=day.isoformat(),
        timezone=str(tz),
        source_latest=max((str(r["recorded_at"]) for r in rows), default=None),
    )
    return document


def process_day(db_path, user_id, day_string):
    """Load a short snapshot, assemble outside the transaction, publish with CAS."""
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute("BEGIN")
        job = con.execute(
            "SELECT * FROM activity_days WHERE user_id=? AND day=?",
            (user_id, day_string),
        ).fetchone()
        settings = con.execute(
            "SELECT * FROM activity_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        if job is None or settings is None:
            con.rollback()
            return False
        job = dict(job)
        settings = dict(settings)
        tz = ZoneInfo(settings.get("timezone") or "Asia/Almaty")
        start, end = bounds(date.fromisoformat(day_string), tz)
        begin = start - timedelta(minutes=120)
        rows = [
            dict(r)
            for r in con.execute(
                """SELECT e.*,d.name AS device_name FROM activity_events e JOIN activity_devices d ON d.id=e.device_id AND d.user_id=e.user_id WHERE e.user_id=? AND e.recorded_at>=? AND e.recorded_at<? ORDER BY e.recorded_at,e.id""",
                (user_id, str(begin), str(end)),
            )
        ]
        places = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM activity_places WHERE user_id=?", (user_id,)
            )
        ]
        con.commit()
        now = datetime.now(timezone.utc)
        document = assemble(date.fromisoformat(day_string), settings, rows, places, now)
        document.update(revision=job["revision"], processed_at=now.isoformat())
        with con:
            next_run = (
                now.replace(tzinfo=None) + timedelta(seconds=60)
                if day_string == now.astimezone(tz).date().isoformat()
                else None
            )
            changed = con.execute(
                """UPDATE activity_days SET document=?,processed_revision=?,processed_at=?,source_latest=?,next_run_at=?,attempts=0,error=NULL WHERE user_id=? AND day=? AND revision=?""",
                (
                    json.dumps(document, ensure_ascii=False),
                    job["revision"],
                    str(now.replace(tzinfo=None)),
                    document["source_latest"],
                    str(next_run) if next_run else None,
                    user_id,
                    day_string,
                    job["revision"],
                ),
            ).rowcount
        return bool(changed)
    finally:
        con.close()
