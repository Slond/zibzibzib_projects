"""Per-user activity diary: window titles, phone locations, nearby computers."""

import json
import os
import logging
import math
import re
import shutil
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert

from app.config import ACTIVITY_DIR
from app.database import (
    ActivityDevice,
    ActivityEvent,
    ActivityPlace,
    ActivitySettings,
    ActivityDay,
    async_session,
    generate_webhook_token,
)

logger = logging.getLogger(__name__)

DEFAULT_RADIUS_M = 80

DEFAULT_SKIP_APPS = "\n".join(
    [
        "1Password",
        "Bitwarden",
        "KeePass",
        "KeePassXC",
        "Messages",
        "Сообщения",
        "Telegram",
        "WhatsApp",
        "Signal",
        "Mail",
        "Почта",
        "Spark",
        "Outlook",
    ]
)
DEFAULT_TIMEZONE = "Asia/Almaty"
PLATFORM_LABELS = {"mac": "Mac", "windows": "Windows", "iphone": "iPhone"}
MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]

BROWSERS = {
    "safari": "Safari",
    "google chrome": "Chrome",
    "chrome": "Chrome",
    "firefox": "Firefox",
    "mozilla firefox": "Firefox",
    "arc": "Arc",
    "microsoft edge": "Edge",
    "msedge": "Edge",
    "brave browser": "Brave",
    "brave": "Brave",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "yandex": "Яндекс Браузер",
    "yandex browser": "Яндекс Браузер",
    "chromium": "Chromium",
    "orion": "Orion",
    "dia": "Dia",
}
TITLE_SUFFIXES = (
    " - Google Chrome",
    " — Google Chrome",
    " - Mozilla Firefox",
    " — Mozilla Firefox",
    " - Microsoft Edge",
    " — Microsoft Edge",
    " - Brave",
    " — Brave",
    " - Arc",
    " — Arc",
)


@dataclass
class DeviceAuth:
    id: int
    user_id: int
    name: str
    platform: str


@dataclass
class IngestResult:
    status: str
    reason: str | None = None
    event_id: int | None = None
    place: str | None = None
    analysis: str | None = None

    def as_dict(self) -> dict:
        payload = {"status": self.status}
        if self.reason:
            payload["reason"] = self.reason
        if self.event_id is not None:
            payload["event_id"] = self.event_id
        if self.place:
            payload["place"] = self.place
        if self.analysis:
            payload["analysis"] = self.analysis
        return payload


def zone_or_default(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def naive_utc(dt: datetime | None = None) -> datetime:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_client_time(value: str | None) -> datetime:
    now = naive_utc()
    if not value or not str(value).strip():
        return now
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise ValueError("bad captured_at")
    parsed = naive_utc(parsed)
    if parsed > now + timedelta(minutes=5) or parsed < now - timedelta(days=90):
        raise ValueError("captured_at outside the accepted 90-day window")
    return parsed


def clip(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def skip_names(raw: str | None) -> list[str]:
    source = raw if raw and raw.strip() else DEFAULT_SKIP_APPS
    return [line.strip() for line in source.splitlines() if line.strip()]


def app_is_skipped(app_name: str | None, names: list[str]) -> bool:
    """Exact name, or a longer name contained in the app title.

    Short names stay exact so «Mail» does not hide Gmail.
    """
    if not app_name:
        return False
    app = app_name.casefold()
    for name in names:
        token = name.casefold()
        if not token:
            continue
        if app == token or (len(token) >= 6 and token in app):
            return True
    return False


def is_sensitive_app(app_name: str | None, raw_skip: str | None) -> bool:
    return app_is_skipped(app_name, skip_names(raw_skip))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(min(1.0, a)))


def match_place(
    places: list, wifi_ssid: str | None, lat: float | None, lon: float | None
):
    if wifi_ssid:
        wanted = wifi_ssid.strip()
        for place in places:
            if place.wifi_ssid and place.wifi_ssid.strip() == wanted:
                return place
    if lat is None or lon is None:
        return None
    best = None
    best_distance = None
    for place in places:
        if place.latitude is None or place.longitude is None:
            continue
        radius = place.radius_m or DEFAULT_RADIUS_M
        distance = haversine_m(lat, lon, place.latitude, place.longitude)
        if distance <= radius and (best_distance is None or distance < best_distance):
            best = place
            best_distance = distance
    return best


def browser_name(app_name: str | None) -> str | None:
    if not app_name:
        return None
    return BROWSERS.get(app_name.strip().casefold())


def clean_window_title(title: str | None) -> str | None:
    if not title:
        return None
    cleaned = title.strip()
    for suffix in TITLE_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    return cleaned or None


def screen_summary(
    app_name: str | None, window_title: str | None, sensitive: bool
) -> str:
    if sensitive:
        return app_name or "Личное приложение"
    if browser_name(app_name) and window_title:
        return window_title[:120]
    if app_name and window_title and window_title.casefold() != app_name.casefold():
        return f"{app_name} — {window_title[:120]}"
    if app_name:
        return app_name
    if window_title:
        return window_title[:120]
    return "Окно"


def location_summary(place) -> str:
    if place and place.note:
        return place.note
    if place:
        return place.name
    return "Геолокация"


def activity_root() -> Path:
    root = Path(ACTIVITY_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def screenshot_file(rel: str | None) -> Path | None:
    if not rel:
        return None
    root = activity_root()
    path = (root / rel).resolve()
    if path != root and root in path.parents and path.is_file():
        return path
    return None


def remove_screenshot(rel: str | None):
    path = screenshot_file(rel)
    if path:
        path.unlink(missing_ok=True)


_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CLOCK = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def breakdown_dir() -> Path:
    return Path(
        os.getenv("ACTIVITY_BREAKDOWN_DIR", activity_root().parent / "activity-days")
    )


def fmt_day(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    from app.services.activity_pipeline import bounds

    return bounds(day, tz)


async def get_or_create_settings(session, user_id: int) -> ActivitySettings:
    result = await session.execute(
        select(ActivitySettings).where(ActivitySettings.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    if row:
        if not row.skip_apps:
            row.skip_apps = DEFAULT_SKIP_APPS
        if not row.timezone:
            row.timezone = DEFAULT_TIMEZONE
        return row
    row = ActivitySettings(
        user_id=user_id,
        retention_days=0,
        timezone=DEFAULT_TIMEZONE,
        sample_minutes=1,
        idle_minutes=3,
        skip_apps=DEFAULT_SKIP_APPS,
    )
    session.add(row)
    await session.flush()
    return row


async def authenticate_device(token: str) -> DeviceAuth | None:
    if not token or len(token) < 16:
        return None
    async with async_session() as session:
        result = await session.execute(
            select(ActivityDevice).where(ActivityDevice.token == token)
        )
        device = result.scalar_one_or_none()
        if not device:
            return None
        return DeviceAuth(
            id=device.id,
            user_id=device.user_id,
            name=device.name,
            platform=device.platform,
        )


async def device_config(device: DeviceAuth, client_version: str | None = None) -> dict:
    async with async_session() as session:
        row = await session.get(ActivityDevice, device.id)
        if row and row.user_id == device.user_id:
            row.last_seen_at = naive_utc()
            if client_version:
                row.client_version = clip(client_version, 20)
        settings = await get_or_create_settings(session, device.user_id)
        payload = {
            "sample_minutes": settings.sample_minutes or 1,
            "idle_minutes": settings.idle_minutes or 3,
            "skip_apps": skip_names(settings.skip_apps),
            "device_id": device.id,
            "protocol_version": 2,
        }
        await session.commit()
        return payload


async def _places_for(session, user_id: int) -> list[ActivityPlace]:
    result = await session.execute(
        select(ActivityPlace)
        .where(ActivityPlace.user_id == user_id)
        .order_by(ActivityPlace.name)
    )
    return list(result.scalars().all())


async def _last_event(
    session, user_id: int, device_id: int, kind: str
) -> ActivityEvent | None:
    result = await session.execute(
        select(ActivityEvent)
        .where(
            ActivityEvent.user_id == user_id,
            ActivityEvent.device_id == device_id,
            ActivityEvent.kind == kind,
        )
        .order_by(ActivityEvent.recorded_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def dirty_day(session, user_id, day, clear=False):
    values = dict(
        user_id=user_id,
        day=str(day),
        revision=1,
        processed_revision=0,
        attempts=0,
        next_run_at=naive_utc() + timedelta(seconds=2),
    )
    changes = dict(
        revision=ActivityDay.revision + 1,
        next_run_at=values["next_run_at"],
        attempts=0,
        error=None,
    )
    if clear:
        changes["document"] = None
    await session.execute(
        insert(ActivityDay)
        .values(**values)
        .on_conflict_do_update(index_elements=["user_id", "day"], set_=changes)
    )


async def dirty_event(session, user_id, recorded_at, settings, interval=45):
    tz = zone_or_default(settings.timezone)
    local = naive_utc(recorded_at).replace(tzinfo=timezone.utc).astimezone(tz)
    for day in {local.date(), (local + timedelta(minutes=interval)).date()}:
        await dirty_day(session, user_id, day)


async def invalidate_user_days(session, user_id):
    settings = await get_or_create_settings(session, user_id)
    tz = zone_or_default(settings.timezone)
    rows = (
        await session.execute(
            select(
                ActivityEvent.recorded_at,
                ActivityEvent.kind,
                ActivityEvent.sample_minutes,
            ).where(ActivityEvent.user_id == user_id)
        )
    ).all()
    days = set()
    for stamp, kind, interval in rows:
        stamp = naive_utc(stamp).replace(tzinfo=timezone.utc)
        minutes = (
            45 if kind == "location" else (interval or settings.sample_minutes or 1)
        )
        days.update(
            {
                stamp.astimezone(tz).date().isoformat(),
                (stamp + timedelta(minutes=minutes)).astimezone(tz).date().isoformat(),
            }
        )
    days.update(
        (
            await session.scalars(
                select(ActivityDay.day).where(ActivityDay.user_id == user_id)
            )
        ).all()
    )
    for day in days:
        await dirty_day(session, user_id, day, clear=True)


async def record_screens(device, samples):
    results = []
    async with async_session() as session:
        row = await session.get(ActivityDevice, device.id)
        if not row or row.user_id != device.user_id:
            return [
                IngestResult(status="error", reason="unknown device") for _ in samples
            ]
        settings = await get_or_create_settings(session, device.user_id)
        places = await _places_for(session, device.user_id)
        dirty = set()
        for payload in samples:
            try:
                recorded_at = parse_client_time(payload.get("captured_at"))
                event_id = payload.get("client_event_id") or None
                if event_id and (
                    not isinstance(event_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", event_id)
                ):
                    raise ValueError("bad client_event_id")
                if event_id and not isinstance(payload.get("captured_at"), str):
                    raise ValueError("captured_at required")
                idle = payload.get("idle_seconds")
                idle = float(idle) if idle is not None else None
                if idle is not None and (
                    not math.isfinite(idle) or not 0 <= idle <= 31536000
                ):
                    raise ValueError("bad idle_seconds")
                interval = int(
                    payload.get("sample_minutes") or settings.sample_minutes or 1
                )
                if not 1 <= interval <= 60:
                    raise ValueError("bad sample_minutes")
                actor = payload.get("actor") or None
                if actor not in (None, "auto", "human", "agent"):
                    raise ValueError("bad actor")
            except (ValueError, TypeError, OverflowError) as exc:
                results.append(IngestResult(status="error", reason=str(exc)))
                continue
            app = clip(payload.get("app_name"), 120)
            title = clip(payload.get("window_title"), 300)
            wifi = clip(payload.get("wifi_ssid"), 128)
            if not app and not title:
                results.append(IngestResult(status="skipped", reason="no_window"))
                continue
            # Timestamp order is not a rate limit: offline batches may arrive out of order.
            if not event_id:
                last = await _last_event(session, device.user_id, device.id, "screen")
                if (
                    last
                    and 0
                    <= (recorded_at - naive_utc(last.recorded_at)).total_seconds()
                    < 45
                ):
                    results.append(IngestResult(status="skipped", reason="too_soon"))
                    continue
            sensitive = is_sensitive_app(app, settings.skip_apps)
            title = None if sensitive else clean_window_title(title)
            place = match_place(places, wifi, None, None)
            values = dict(
                user_id=device.user_id,
                device_id=device.id,
                place_id=place.id if place else None,
                recorded_at=recorded_at,
                kind="screen",
                app_name=app,
                window_title=title,
                wifi_ssid=wifi,
                summary=screen_summary(app, title, sensitive),
                analysis_status="skipped",
                client_event_id=event_id,
                idle_seconds=idle,
                sample_minutes=interval,
                actor=actor,
            )
            # One real write transaction contains both the event and its day invalidation.
            stored_id = await session.scalar(
                insert(ActivityEvent)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["device_id", "client_event_id"])
                .returning(ActivityEvent.id)
            )
            if stored_id is None:
                stored_id = await session.scalar(
                    select(ActivityEvent.id).where(
                        ActivityEvent.device_id == device.id,
                        ActivityEvent.client_event_id == event_id,
                    )
                )
                results.append(IngestResult(status="ok", event_id=stored_id))
                continue
            tz = zone_or_default(settings.timezone)
            local = recorded_at.replace(tzinfo=timezone.utc).astimezone(tz)
            dirty.update({local.date(), (local + timedelta(minutes=interval)).date()})
            results.append(
                IngestResult(
                    status="ok", event_id=stored_id, place=place.name if place else None
                )
            )
        row.last_seen_at = naive_utc()
        for day in dirty:
            await dirty_day(session, device.user_id, day)
        await session.commit()
    return results


async def record_screen(
    device,
    app_name,
    window_title,
    wifi_ssid,
    captured_at,
    client_event_id=None,
    idle_seconds=None,
    sample_minutes=None,
    actor=None,
):
    return (
        await record_screens(
            device,
            [
                dict(
                    app_name=app_name,
                    window_title=window_title,
                    wifi_ssid=wifi_ssid,
                    captured_at=captured_at,
                    client_event_id=client_event_id,
                    idle_seconds=idle_seconds,
                    sample_minutes=sample_minutes,
                    actor=actor,
                )
            ],
        )
    )[0]


async def record_location(
    device: DeviceAuth,
    lat: float,
    lon: float,
    accuracy: float | None,
    captured_at: str | None,
) -> IngestResult:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return IngestResult(status="error", reason="bad coordinates")
    if accuracy is not None and (not math.isfinite(accuracy) or accuracy < 0):
        return IngestResult(status="error", reason="bad accuracy")
    try:
        recorded_at = parse_client_time(captured_at)
    except ValueError as exc:
        return IngestResult(status="error", reason=str(exc))
    async with async_session() as session:
        row = await session.get(ActivityDevice, device.id)
        if not row or row.user_id != device.user_id:
            return IngestResult(status="error", reason="unknown device")
        settings = await get_or_create_settings(session, device.user_id)
        last = await _last_event(session, device.user_id, device.id, "location")
        if last and last.latitude is not None and last.longitude is not None:
            age = (recorded_at - naive_utc(last.recorded_at)).total_seconds()
            distance = haversine_m(lat, lon, last.latitude, last.longitude)
            if 0 <= age < 120 and distance < 40:
                row.last_seen_at = naive_utc()
                await session.commit()
                return IngestResult(status="skipped", reason="still")

        places = await _places_for(session, device.user_id)
        place = match_place(places, None, lat, lon)
        event = ActivityEvent(
            user_id=device.user_id,
            device_id=device.id,
            place_id=place.id if place else None,
            recorded_at=recorded_at,
            kind="location",
            latitude=lat,
            longitude=lon,
            accuracy_m=accuracy,
            summary=location_summary(place),
            analysis_status="skipped",
        )
        session.add(event)
        row.last_seen_at = naive_utc()
        await dirty_event(session, device.user_id, recorded_at, settings)
        await session.commit()
        return IngestResult(
            status="ok",
            event_id=event.id,
            place=place.name if place else None,
            analysis="skipped",
        )


async def _merged_document(user_id: int, day: date) -> tuple[dict, dict]:
    from app.services.activity_pipeline import assemble
    from app.services.activity_day import DAY_VERSION

    async with async_session() as session:
        settings = await get_or_create_settings(session, user_id)
        tz = zone_or_default(settings.timezone)
        today = datetime.now(tz).date()
        job = await session.get(ActivityDay, (user_id, day.isoformat()))
        places = await _places_for(session, user_id)
        device_count = await session.scalar(
            select(func.count())
            .select_from(ActivityDevice)
            .where(ActivityDevice.user_id == user_id)
        )
        document = None
        if job and job.document and job.revision == job.processed_revision:
            try:
                cached = json.loads(job.document)
                if cached.get("version") == DAY_VERSION:
                    document = cached
            except (ValueError, TypeError):
                pass
        if document is None:
            start, end = local_day_bounds(day, tz)
            result = await session.execute(
                select(ActivityEvent, ActivityDevice)
                .join(ActivityDevice, ActivityDevice.id == ActivityEvent.device_id)
                .where(
                    ActivityEvent.user_id == user_id,
                    ActivityDevice.user_id == user_id,
                    ActivityEvent.recorded_at >= start - timedelta(minutes=120),
                    ActivityEvent.recorded_at < end,
                )
                .order_by(ActivityEvent.recorded_at)
            )
            rows = [
                dict(
                    **{
                        c.name: getattr(e, c.name)
                        for c in ActivityEvent.__table__.columns
                    },
                    device_name=d.name,
                )
                for e, d in result.all()
            ]
            prefs = {
                c.name: getattr(settings, c.name)
                for c in ActivitySettings.__table__.columns
            }
            place_rows = [
                {c.name: getattr(p, c.name) for c in ActivityPlace.__table__.columns}
                for p in places
            ]
            document = await asyncio.to_thread(assemble, day, prefs, rows, place_rows)
            if rows and job is None:
                await dirty_day(session, user_id, day)
        await session.commit()
        extras = dict(
            day=day.isoformat(),
            day_label=fmt_day(day),
            is_today=day == today,
            prev_date=(day - timedelta(days=1)).isoformat(),
            next_date=None if day >= today else (day + timedelta(days=1)).isoformat(),
            has_places=bool(places),
            has_devices=bool(device_count),
            snapshot=job.processed_at.isoformat() if job and job.processed_at else None,
            revision=job.revision if job else 0,
            processed_revision=job.processed_revision if job else 0,
            processing=bool(job and job.revision != job.processed_revision),
            processing_error=job.error if job else None,
            processed_at=job.processed_at.replace(tzinfo=timezone.utc)
            .astimezone(tz)
            .strftime("%d.%m %H:%M:%S")
            if job and job.processed_at
            else None,
        )
    return document, extras


async def layered_page(
    user_id: int, day: date, selected_start: str | None = None
) -> dict:
    from app.services.activity_day import present_day

    try:
        document, extras = await _merged_document(user_id, day)
    except Exception:
        logger.exception("activity layered page")
        document = {
            "error": "raw",
            "body": [],
            "screen": [],
            "agent": [],
            "disputes": [],
        }
        tz_now = datetime.now(zone_or_default(None))
        today = tz_now.date()
        extras = {
            "day": day.isoformat(),
            "day_label": fmt_day(day),
            "is_today": day == today,
            "prev_date": (day - timedelta(days=1)).isoformat(),
            "next_date": None
            if day >= today
            else (day + timedelta(days=1)).isoformat(),
            "has_places": False,
            "has_devices": True,
        }
    view = present_day(document, selected_start)
    view.update(extras)
    if not extras["has_devices"]:
        view.pop("screen_sum", None)
        view.pop("agent_sum", None)
    return view


def _matching_place(places, lat: float, lon: float, ssid: str | None):
    from app.services.activity_day import STAY_RADIUS_M

    found = None
    best = None
    stay_ssid = (ssid or "").strip()
    for place in places:
        if place.latitude is None or place.longitude is None:
            continue
        distance = haversine_m(lat, lon, place.latitude, place.longitude)
        if distance > (place.radius_m or STAY_RADIUS_M):
            continue
        place_ssid = (place.wifi_ssid or "").strip()
        if stay_ssid and place_ssid and stay_ssid != place_ssid:
            continue
        if best is None or distance < best:
            found = place
            best = distance
    return found


async def remember_stay(
    user_id: int, day: date, stay_start: str, name: str
) -> str | None:
    """Store one signature for the selected stay. A second save updates that row."""
    from app.services.activity_day import STAY_RADIUS_M

    name = (name or "").strip()[:80]
    if not name:
        return "Укажите название места"
    if not stay_start:
        return "Выберите место на шкале"
    try:
        stamp = naive_utc(datetime.fromisoformat(stay_start))
    except ValueError:
        return "Выберите место на шкале"
    try:
        document, _extras = await _merged_document(user_id, day)
    except Exception:
        logger.exception("activity remember")
        return "День не собрался. Сырые события не прочитались."
    stay = next(
        (
            row
            for row in document.get("body") or []
            if row.get("kind") == "stay" and row.get("start") == stay_start
        ),
        None,
    )
    center = (stay or {}).get("center") or {}
    if stay is None or center.get("lat") is None or center.get("lon") is None:
        return "Выберите место на шкале"
    ssid = (stay.get("ssid") or "").strip()[:128]
    async with async_session() as session:
        places = await _places_for(session, user_id)
        match = _matching_place(places, center["lat"], center["lon"], ssid or None)
        if match:
            labels = (
                json.loads(match.labels_json)
                if match.labels_json
                else [
                    dict(
                        name=match.name,
                        at=str(match.created_at or datetime(1970, 1, 1)),
                    )
                ]
            )
            labels = [
                label
                for label in labels
                if naive_utc(datetime.fromisoformat(label["at"])) != stamp
            ]
            labels.append(dict(name=name, at=stamp.isoformat()))
            match.labels_json = json.dumps(labels, ensure_ascii=False)
            match.name = name
            match.corrected_at = stamp
            if ssid and not (match.wifi_ssid or "").strip():
                match.wifi_ssid = ssid
        else:
            session.add(
                ActivityPlace(
                    user_id=user_id,
                    name=name,
                    wifi_ssid=ssid or None,
                    latitude=center["lat"],
                    longitude=center["lon"],
                    radius_m=STAY_RADIUS_M,
                    created_at=stamp,
                    corrected_at=stamp,
                    labels_json=json.dumps(
                        [dict(name=name, at=stamp.isoformat())], ensure_ascii=False
                    ),
                )
            )
        await invalidate_user_days(session, user_id)
        await session.commit()
    try:
        await _merged_document(user_id, day)
    except Exception:
        logger.exception("activity remember reapply")
    return None


async def list_places(user_id: int) -> list[dict]:
    async with async_session() as session:
        places = await _places_for(session, user_id)
        return [
            {
                "id": place.id,
                "name": place.name,
                "note": place.note or "",
                "wifi_ssid": place.wifi_ssid or "",
                "latitude": place.latitude,
                "longitude": place.longitude,
                "radius_m": place.radius_m or "",
            }
            for place in places
        ]


async def latest_fix(user_id: int) -> dict | None:
    async with async_session() as session:
        result = await session.execute(
            select(ActivityEvent)
            .where(
                ActivityEvent.user_id == user_id,
                ActivityEvent.kind == "location",
                ActivityEvent.latitude.is_not(None),
                ActivityEvent.longitude.is_not(None),
            )
            .order_by(ActivityEvent.recorded_at.desc())
            .limit(1)
        )
        event = result.scalar_one_or_none()
        if not event:
            return None
        return {"lat": event.latitude, "lon": event.longitude}


def _parse_coord(value: str | None) -> float | None:
    if value is None or not str(value).strip():
        return None
    return float(str(value).strip().replace(",", "."))


async def upsert_place(
    user_id: int,
    place_id: int | None,
    name: str,
    note: str,
    wifi_ssid: str,
    latitude: str,
    longitude: str,
    radius_m: str,
) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Укажите название места"
    name = name[:80]
    note = clip(note, 200)
    wifi = clip(wifi_ssid, 128)
    try:
        lat = _parse_coord(latitude)
        lon = _parse_coord(longitude)
    except ValueError:
        return "Координаты должны быть числами"
    if (lat is None) ^ (lon is None):
        return "Укажите и широту, и долготу"
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "Координаты вне диапазона"
    radius = None
    if lat is not None:
        raw_radius = (radius_m or "").strip()
        if not raw_radius:
            radius = DEFAULT_RADIUS_M
        else:
            try:
                radius = int(float(raw_radius.replace(",", ".")))
            except ValueError:
                return "Радиус должен быть числом"
            if radius < 5 or radius > 20000:
                return "Радиус от 5 до 20000 метров"
    if not wifi and lat is None:
        return "Укажите сеть Wi‑Fi или координаты"

    async with async_session() as session:
        if place_id:
            place = await session.get(ActivityPlace, place_id)
            if not place or place.user_id != user_id:
                return "Место не найдено"
        else:
            place = ActivityPlace(user_id=user_id, name=name)
            session.add(place)
        if place_id and place.name != name:
            labels = (
                json.loads(place.labels_json)
                if place.labels_json
                else [
                    dict(
                        name=place.name,
                        at=place.created_at.replace(tzinfo=timezone.utc).isoformat(),
                    )
                ]
            )
            labels.append(dict(name=name, at=datetime.now(timezone.utc).isoformat()))
            place.labels_json = json.dumps(labels, ensure_ascii=False)
        place.name = name
        place.note = note
        place.wifi_ssid = wifi
        place.latitude = lat
        place.longitude = lon
        place.radius_m = radius
        await session.flush()
        await invalidate_user_days(session, user_id)
        await session.commit()
    return None


async def delete_place(user_id: int, place_id: int) -> bool:
    async with async_session() as session:
        place = await session.get(ActivityPlace, place_id)
        if not place or place.user_id != user_id:
            return False
        await session.execute(
            update(ActivityEvent)
            .where(ActivityEvent.place_id == place_id, ActivityEvent.user_id == user_id)
            .values(place_id=None)
        )
        await session.delete(place)
        await invalidate_user_days(session, user_id)
        await session.commit()
        return True


async def list_devices(user_id: int, tz_name: str) -> list[dict]:
    tz = zone_or_default(tz_name)
    async with async_session() as session:
        result = await session.execute(
            select(ActivityDevice)
            .where(ActivityDevice.user_id == user_id)
            .order_by(ActivityDevice.created_at.desc())
        )
        devices = []
        for device in result.scalars().all():
            seen = None
            if device.last_seen_at:
                seen_day = (
                    naive_utc(device.last_seen_at)
                    .replace(tzinfo=timezone.utc)
                    .astimezone(tz)
                )
                seen = seen_day.strftime("%d.%m %H:%M")
            devices.append(
                {
                    "id": device.id,
                    "name": device.name,
                    "platform": device.platform,
                    "platform_label": PLATFORM_LABELS.get(
                        device.platform, device.platform
                    ),
                    "token": device.token,
                    "last_seen": seen,
                    "client_version": device.client_version,
                    "update_required": device.platform != "iphone"
                    and device.client_version != "2",
                }
            )
        return devices


async def create_device(user_id: int, name: str, platform: str) -> str | None:
    if platform not in PLATFORM_LABELS:
        return "Выберите Mac, Windows или iPhone"
    name = (name or "").strip()[:80]
    if not name:
        return "Укажите название устройства"
    async with async_session() as session:
        session.add(
            ActivityDevice(
                user_id=user_id,
                name=name,
                platform=platform,
                token=generate_webhook_token(),
            )
        )
        await session.commit()
    return None


async def rename_device(user_id: int, device_id: int, name: str) -> bool:
    name = (name or "").strip()[:80]
    if not name:
        return False
    async with async_session() as session:
        device = await session.get(ActivityDevice, device_id)
        if not device or device.user_id != user_id:
            return False
        device.name = name
        await invalidate_user_days(session, user_id)
        await session.commit()
        return True


async def regenerate_device_token(user_id: int, device_id: int) -> bool:
    async with async_session() as session:
        device = await session.get(ActivityDevice, device_id)
        if not device or device.user_id != user_id:
            return False
        device.token = generate_webhook_token()
        await session.commit()
        return True


async def delete_device(user_id: int, device_id: int) -> bool:
    async with async_session() as session:
        device = await session.get(ActivityDevice, device_id)
        if not device or device.user_id != user_id:
            return False
        result = await session.execute(
            select(ActivityEvent.screenshot_path).where(
                ActivityEvent.device_id == device_id,
                ActivityEvent.user_id == user_id,
            )
        )
        for path in result.scalars().all():
            remove_screenshot(path)
        await session.execute(
            delete(ActivityEvent).where(
                ActivityEvent.device_id == device_id, ActivityEvent.user_id == user_id
            )
        )
        await session.delete(device)
        await session.flush()
        await invalidate_user_days(session, user_id)
        await session.commit()
        shutil.rmtree(breakdown_dir() / str(int(user_id)), ignore_errors=True)
        return True


async def settings_view(user_id: int) -> dict:
    async with async_session() as session:
        settings = await get_or_create_settings(session, user_id)
        await session.commit()
        return {
            "timezone": settings.timezone or DEFAULT_TIMEZONE,
            "sample_minutes": settings.sample_minutes or 1,
            "idle_minutes": settings.idle_minutes or 3,
            "skip_apps": settings.skip_apps or DEFAULT_SKIP_APPS,
            "retention_days": settings.retention_days or 0,
        }


async def update_settings(
    user_id: int,
    timezone_name: str,
    sample_minutes: str,
    idle_minutes: str,
    skip_apps: str,
    retention_days: str = "0",
) -> str | None:
    tz_name = (timezone_name or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(tz_name)
    except Exception:
        return "Неизвестная временная зона. Пример: Asia/Almaty"
    try:
        sample = int(sample_minutes)
        idle = int(idle_minutes)
        retention = int(retention_days)
    except ValueError:
        return "Интервалы должны быть числами"
    if not 1 <= sample <= 60:
        return "Интервал записи от 1 до 60 минут"
    if not 1 <= idle <= 60:
        return "Пауза простоя от 1 до 60 минут"
    if not 0 <= retention <= 3650:
        return "Хранение от 0 до 3650 дней; 0 — без автоматического удаления"
    apps = (skip_apps or "").strip()
    if len(apps) > 4000:
        return "Список приложений слишком длинный"

    async with async_session() as session:
        settings = await get_or_create_settings(session, user_id)
        settings.timezone = tz_name
        settings.sample_minutes = sample
        settings.idle_minutes = idle
        settings.skip_apps = apps or DEFAULT_SKIP_APPS
        settings.retention_days = retention
        await invalidate_user_days(session, user_id)
        await session.commit()
    return None


async def purge_activity_history():
    """Apply the user's explicit event retention; zero keeps the full history."""
    async with async_session() as session:
        prefs = (await session.scalars(select(ActivitySettings))).all()
        for settings in prefs:
            if not settings.retention_days:
                continue
            tz = zone_or_default(settings.timezone)
            cutoff_day = datetime.now(tz).date() - timedelta(
                days=settings.retention_days
            )
            cutoff, _ = local_day_bounds(cutoff_day, tz)
            images = (
                await session.scalars(
                    select(ActivityEvent.screenshot_path).where(
                        ActivityEvent.user_id == settings.user_id,
                        ActivityEvent.recorded_at < cutoff,
                        ActivityEvent.screenshot_path.is_not(None),
                    )
                )
            ).all()
            for image in images:
                remove_screenshot(image)
            await session.execute(
                delete(ActivityEvent).where(
                    ActivityEvent.user_id == settings.user_id,
                    ActivityEvent.recorded_at < cutoff,
                )
            )
            await session.execute(
                delete(ActivityDay).where(
                    ActivityDay.user_id == settings.user_id,
                    ActivityDay.day < cutoff_day.isoformat(),
                )
            )
            await invalidate_user_days(session, settings.user_id)
            folder = breakdown_dir() / str(int(settings.user_id))
            if folder.exists():
                shutil.rmtree(folder)
        await session.commit()


async def delete_user_activity_data(user_id: int):
    for root in (activity_root(), breakdown_dir().resolve()):
        folder = (root / str(int(user_id))).resolve()
        if folder.parent == root and folder.exists():
            shutil.rmtree(folder)
    async with async_session() as session:
        await session.execute(delete(ActivityDay).where(ActivityDay.user_id == user_id))
        await session.execute(
            delete(ActivityEvent).where(ActivityEvent.user_id == user_id)
        )
        await session.execute(
            delete(ActivityPlace).where(ActivityPlace.user_id == user_id)
        )
        await session.execute(
            delete(ActivityDevice).where(ActivityDevice.user_id == user_id)
        )
        await session.execute(
            delete(ActivitySettings).where(ActivitySettings.user_id == user_id)
        )
        await session.commit()
