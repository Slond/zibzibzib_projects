"""Per-user activity diary: window titles, phone locations, nearby computers."""
import logging
import math
import shutil
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from app.config import ACTIVITY_DIR
from app.database import (
    ActivityDevice,
    ActivityEvent,
    ActivityPlace,
    ActivitySettings,
    async_session,
    generate_webhook_token,
)

logger = logging.getLogger(__name__)

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
DEFAULT_MODEL = "gpt-4o-mini"
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
    except ValueError:
        return now
    parsed = naive_utc(parsed)
    if abs((parsed - now).total_seconds()) > 86400:
        return now
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
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def match_place(places: list, wifi_ssid: str | None, lat: float | None, lon: float | None):
    if wifi_ssid:
        wanted = wifi_ssid.strip().casefold()
        for place in places:
            if place.wifi_ssid and place.wifi_ssid.strip().casefold() == wanted:
                return place
    if lat is None or lon is None:
        return None
    best = None
    best_distance = None
    for place in places:
        if place.latitude is None or place.longitude is None:
            continue
        radius = place.radius_m or 150
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


def screen_summary(app_name: str | None, window_title: str | None, sensitive: bool) -> str:
    if sensitive:
        return "Личное приложение"
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


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "момент"
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    if rest:
        return f"{hours} ч {rest} мин"
    return f"{hours} ч"


def fmt_time(dt: datetime, tz: ZoneInfo) -> str:
    aware = dt.replace(tzinfo=timezone.utc)
    return aware.astimezone(tz).strftime("%H:%M")


def fmt_day(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    end = start + timedelta(days=1)
    return start, end


async def get_or_create_settings(session, user_id: int) -> ActivitySettings:
    result = await session.execute(
        select(ActivitySettings).where(ActivitySettings.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    if row:
        if not row.skip_apps:
            row.skip_apps = DEFAULT_SKIP_APPS
        if not row.openai_model:
            row.openai_model = DEFAULT_MODEL
        if not row.timezone:
            row.timezone = DEFAULT_TIMEZONE
        return row
    row = ActivitySettings(
        user_id=user_id,
        openai_model=DEFAULT_MODEL,
        retention_days=7,
        timezone=DEFAULT_TIMEZONE,
        sample_minutes=5,
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


async def device_config(device: DeviceAuth) -> dict:
    async with async_session() as session:
        row = await session.get(ActivityDevice, device.id)
        if row and row.user_id == device.user_id:
            row.last_seen_at = naive_utc()
        settings = await get_or_create_settings(session, device.user_id)
        payload = {
            "sample_minutes": settings.sample_minutes or 5,
            "idle_minutes": settings.idle_minutes or 3,
            "skip_apps": skip_names(settings.skip_apps),
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


async def _last_event(session, user_id: int, device_id: int, kind: str) -> ActivityEvent | None:
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


async def record_screen(
    device: DeviceAuth,
    app_name: str | None,
    window_title: str | None,
    wifi_ssid: str | None,
    captured_at: str | None,
) -> IngestResult:
    app_name = clip(app_name, 120)
    window_title = clip(window_title, 300)
    wifi_ssid = clip(wifi_ssid, 128)
    recorded_at = parse_client_time(captured_at)

    async with async_session() as session:
        row = await session.get(ActivityDevice, device.id)
        if not row or row.user_id != device.user_id:
            return IngestResult(status="error", reason="unknown device")
        settings = await get_or_create_settings(session, device.user_id)
        last = await _last_event(session, device.user_id, device.id, "screen")
        if last and (recorded_at - naive_utc(last.recorded_at)).total_seconds() < 45:
            row.last_seen_at = recorded_at
            await session.commit()
            return IngestResult(status="skipped", reason="too_soon")

        if not app_name and not window_title:
            row.last_seen_at = recorded_at
            await session.commit()
            return IngestResult(status="skipped", reason="no_window")

        places = await _places_for(session, device.user_id)
        place = match_place(places, wifi_ssid, None, None)
        sensitive = is_sensitive_app(app_name, settings.skip_apps)
        if sensitive:
            window_title = None
        else:
            window_title = clean_window_title(window_title)

        summary = screen_summary(app_name, window_title, sensitive)
        event = ActivityEvent(
            user_id=device.user_id,
            device_id=device.id,
            place_id=place.id if place else None,
            recorded_at=recorded_at,
            kind="screen",
            app_name=app_name,
            window_title=window_title,
            wifi_ssid=wifi_ssid,
            summary=summary,
            analysis_status="skipped",
        )
        session.add(event)
        row.last_seen_at = recorded_at
        await session.commit()
        return IngestResult(
            status="ok",
            event_id=event.id,
            place=place.name if place else None,
        )


async def record_location(
    device: DeviceAuth,
    lat: float,
    lon: float,
    accuracy: float | None,
    captured_at: str | None,
) -> IngestResult:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return IngestResult(status="error", reason="bad coordinates")
    recorded_at = parse_client_time(captured_at)
    async with async_session() as session:
        row = await session.get(ActivityDevice, device.id)
        if not row or row.user_id != device.user_id:
            return IngestResult(status="error", reason="unknown device")
        await get_or_create_settings(session, device.user_id)
        last = await _last_event(session, device.user_id, device.id, "location")
        if last and last.latitude is not None and last.longitude is not None:
            age = (recorded_at - naive_utc(last.recorded_at)).total_seconds()
            distance = haversine_m(lat, lon, last.latitude, last.longitude)
            if age < 120 and distance < 40:
                row.last_seen_at = recorded_at
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
        row.last_seen_at = recorded_at
        await session.commit()
        return IngestResult(
            status="ok",
            event_id=event.id,
            place=place.name if place else None,
            analysis="skipped",
        )



def _segment_from(row: dict) -> dict:
    return {
        "key": (row["place_id"], row["app_name"], (row["summary"] or "").strip(), row["device_id"]),
        "first_at": row["recorded_at"],
        "last_at": row["recorded_at"],
        "place_id": row["place_id"],
        "place_name": row["place_name"],
        "place_note": row["place_note"],
        "summary": row["summary"],
        "app_name": row["app_name"],
        "title": row["title"],
        "browser": row["browser"],
        "private": row["private"],
        "device_id": row["device_id"],
        "device_name": row["device_name"],
        "platform_label": row["platform_label"],
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "count": 1,
    }


def build_segments(rows: list[dict], gap: timedelta, tz: ZoneInfo, now_utc: datetime, pad: timedelta | None):
    segments: list[dict] = []
    for row in rows:
        current = _segment_from(row)
        if segments:
            prev = segments[-1]
            if prev["key"] == current["key"] and (row["recorded_at"] - prev["last_at"]) <= gap:
                prev["last_at"] = row["recorded_at"]
                prev["count"] += 1
                if row["summary"]:
                    prev["summary"] = row["summary"]
                continue
        segments.append(current)

    for index, seg in enumerate(segments):
        end = seg["last_at"]
        if pad is not None:
            padded = seg["last_at"] + pad
            if index + 1 < len(segments):
                nxt = segments[index + 1]["first_at"]
                if padded > nxt:
                    padded = nxt
            if padded > now_utc:
                padded = now_utc
            if padded > end:
                end = padded
        seconds = max(0, (end - seg["first_at"]).total_seconds())
        seg["start"] = seg["first_at"]
        seg["end"] = end
        seg["start_label"] = fmt_time(seg["first_at"], tz)
        seg["end_label"] = fmt_time(end, tz)
        seg["duration_label"] = fmt_duration(seconds)
        seg["seconds"] = seconds
        seg.pop("key", None)
        seg.pop("first_at", None)
        seg.pop("last_at", None)
    return segments


def _presence_line(seg: dict) -> dict:
    return {
        "device_name": seg["device_name"],
        "platform_label": seg["platform_label"],
        "app_name": seg["app_name"],
        "title": seg.get("title"),
        "browser": seg.get("browser"),
        "place_name": seg.get("place_name"),
        "place_note": seg.get("place_note"),
        "private": seg.get("private", False),
    }


def build_presence(segments: list[dict], tz: ZoneInfo) -> list[dict]:
    """Split the day so overlapping computers become one nearby block."""
    spanned = []
    for seg in segments:
        start = seg["start"]
        end = seg["end"]
        if end <= start:
            end = start + timedelta(seconds=1)
        spanned.append((start, end, seg))
    if not spanned:
        return []

    bounds = sorted({point for start, end, _seg in spanned for point in (start, end)})
    slices = []
    for t0, t1 in zip(bounds, bounds[1:]):
        if t1 <= t0:
            continue
        active = [seg for start, end, seg in spanned if start < t1 and end > t0]
        if not active:
            continue
        signature = tuple(
            sorted(
                (
                    seg["device_id"],
                    seg.get("app_name") or "",
                    seg.get("title") or "",
                    seg.get("summary") or "",
                )
                for seg in active
            )
        )
        if slices and slices[-1]["signature"] == signature and slices[-1]["end"] == t0:
            slices[-1]["end"] = t1
            continue
        slices.append({"signature": signature, "start": t0, "end": t1, "segments": active})

    blocks = []
    for item in slices:
        seconds = max(0, (item["end"] - item["start"]).total_seconds())
        lines = []
        seen = set()
        for seg in item["segments"]:
            if seg["device_id"] in seen:
                continue
            seen.add(seg["device_id"])
            lines.append(_presence_line(seg))
        lines.sort(key=lambda line: line["device_name"].casefold())
        places = {line["place_name"] for line in lines if line["place_name"]}
        place_label = next(iter(places)) if len(places) == 1 else None
        place_note = None
        if place_label:
            notes = {line["place_note"] for line in lines if line["place_name"] == place_label and line["place_note"]}
            place_note = next(iter(notes)) if len(notes) == 1 else None
        blocks.append(
            {
                "nearby": len(seen) >= 2,
                "start_label": fmt_time(item["start"], tz),
                "end_label": fmt_time(item["end"], tz),
                "duration_label": fmt_duration(seconds),
                "seconds": seconds,
                "place_label": place_label,
                "place_note": place_note,
                "lines": lines,
            }
        )
    return blocks


async def build_day(user_id: int, day: date) -> dict:
    async with async_session() as session:
        settings = await get_or_create_settings(session, user_id)
        await session.commit()
        tz = zone_or_default(settings.timezone)
        start, end = local_day_bounds(day, tz)
        result = await session.execute(
            select(ActivityEvent, ActivityPlace, ActivityDevice)
            .join(ActivityDevice, ActivityDevice.id == ActivityEvent.device_id)
            .outerjoin(ActivityPlace, ActivityPlace.id == ActivityEvent.place_id)
            .where(
                ActivityEvent.user_id == user_id,
                ActivityDevice.user_id == user_id,
                ActivityEvent.recorded_at >= start,
                ActivityEvent.recorded_at < end,
            )
            .order_by(ActivityEvent.recorded_at.asc())
        )
        screen_rows = []
        location_rows = []
        for event, place, device in result.all():
            if place and place.user_id != user_id:
                place = None
            item = {
                "id": event.id,
                "recorded_at": naive_utc(event.recorded_at),
                "place_id": place.id if place else None,
                "place_name": place.name if place else None,
                "place_note": place.note if place else None,
                "summary": event.summary,
                "app_name": event.app_name,
                "title": event.window_title,
                "browser": browser_name(event.app_name),
                "private": event.summary == "Личное приложение",
                "device_id": device.id,
                "device_name": device.name,
                "platform_label": PLATFORM_LABELS.get(device.platform, device.platform),
                "lat": event.latitude,
                "lon": event.longitude,
            }
            if event.kind == "location":
                location_rows.append(item)
            else:
                screen_rows.append(item)

        place_count = await session.scalar(
            select(func.count()).select_from(ActivityPlace).where(ActivityPlace.user_id == user_id)
        )
        device_count = await session.scalar(
            select(func.count()).select_from(ActivityDevice).where(ActivityDevice.user_id == user_id)
        )
        sample = max(1, settings.sample_minutes or 5)
        now_utc = naive_utc()
        pad = timedelta(minutes=sample)
        gap = timedelta(minutes=20)
        per_device: dict[int, list] = {}
        for row in screen_rows:
            per_device.setdefault(row["device_id"], []).append(row)
        screen = []
        for rows in per_device.values():
            screen.extend(build_segments(rows, gap, tz, now_utc, pad))
        screen.sort(key=lambda seg: (seg["start"], seg["device_name"]))
        presence = build_presence(screen, tz)
        location = build_segments(location_rows, timedelta(minutes=90), tz, now_utc, None)
        total_seconds = sum(block["seconds"] for block in presence)
        nearby_seconds = sum(block["seconds"] for block in presence if block["nearby"])
        today = datetime.now(tz).date()
        return {
            "day": day.isoformat(),
            "day_label": fmt_day(day),
            "is_today": day == today,
            "prev_date": (day - timedelta(days=1)).isoformat(),
            "next_date": None if day >= today else (day + timedelta(days=1)).isoformat(),
            "presence": presence,
            "location_segments": location,
            "screen_total": fmt_duration(total_seconds) if total_seconds >= 60 else None,
            "nearby_total": fmt_duration(nearby_seconds) if nearby_seconds >= 60 else None,
            "has_places": bool(place_count),
            "has_devices": bool(device_count),
        }


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
            radius = 150
        else:
            try:
                radius = int(float(raw_radius.replace(",", ".")))
            except ValueError:
                return "Радиус должен быть числом"
            if radius < 30 or radius > 20000:
                return "Радиус от 30 до 20000 метров"
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
        place.name = name
        place.note = note
        place.wifi_ssid = wifi
        place.latitude = lat
        place.longitude = lon
        place.radius_m = radius
        await session.commit()
    return None


async def delete_place(user_id: int, place_id: int) -> bool:
    async with async_session() as session:
        place = await session.get(ActivityPlace, place_id)
        if not place or place.user_id != user_id:
            return False
        result = await session.execute(
            select(ActivityEvent).where(
                ActivityEvent.place_id == place_id,
                ActivityEvent.user_id == user_id,
            )
        )
        for event in result.scalars().all():
            event.place_id = None
        await session.delete(place)
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
                seen_day = naive_utc(device.last_seen_at).replace(tzinfo=timezone.utc).astimezone(tz)
                seen = seen_day.strftime("%d.%m %H:%M")
            devices.append(
                {
                    "id": device.id,
                    "name": device.name,
                    "platform": device.platform,
                    "platform_label": PLATFORM_LABELS.get(device.platform, device.platform),
                    "token": device.token,
                    "last_seen": seen,
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
            select(ActivityEvent).where(
                ActivityEvent.device_id == device_id,
                ActivityEvent.user_id == user_id,
            )
        )
        for event in result.scalars().all():
            remove_screenshot(event.screenshot_path)
            await session.delete(event)
        await session.delete(device)
        await session.commit()
        return True


async def settings_view(user_id: int) -> dict:
    async with async_session() as session:
        settings = await get_or_create_settings(session, user_id)
        await session.commit()
        return {
            "timezone": settings.timezone or DEFAULT_TIMEZONE,
            "sample_minutes": settings.sample_minutes or 5,
            "idle_minutes": settings.idle_minutes or 3,
            "skip_apps": settings.skip_apps or DEFAULT_SKIP_APPS,
        }


async def update_settings(
    user_id: int,
    timezone_name: str,
    sample_minutes: str,
    idle_minutes: str,
    skip_apps: str,
) -> str | None:
    tz_name = (timezone_name or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(tz_name)
    except Exception:
        return "Неизвестная временная зона. Пример: Asia/Almaty"
    try:
        sample = int(sample_minutes)
        idle = int(idle_minutes)
    except ValueError:
        return "Интервалы должны быть числами"
    if not 1 <= sample <= 60:
        return "Интервал записи от 1 до 60 минут"
    if not 1 <= idle <= 60:
        return "Пауза простоя от 1 до 60 минут"
    apps = (skip_apps or "").strip()
    if len(apps) > 4000:
        return "Список приложений слишком длинный"

    async with async_session() as session:
        settings = await get_or_create_settings(session, user_id)
        settings.timezone = tz_name
        settings.sample_minutes = sample
        settings.idle_minutes = idle
        settings.skip_apps = apps or DEFAULT_SKIP_APPS
        await session.commit()
    return None


async def purge_expired_screenshots():
    now = naive_utc()
    async with async_session() as session:
        settings_rows = (await session.execute(select(ActivitySettings))).scalars().all()
        retention = {row.user_id: row.retention_days or 7 for row in settings_rows}
        result = await session.execute(
            select(ActivityEvent).where(ActivityEvent.screenshot_path.is_not(None))
        )
        removed = 0
        for event in result.scalars().all():
            days = retention.get(event.user_id, 7)
            recorded = naive_utc(event.recorded_at) if event.recorded_at else None
            if recorded and now - recorded > timedelta(days=days):
                remove_screenshot(event.screenshot_path)
                event.screenshot_path = None
                removed += 1
        await session.commit()
        if removed:
            logger.info("Removed %s expired activity screenshots", removed)


async def delete_user_activity_data(user_id: int):
    folder = (activity_root() / str(int(user_id))).resolve()
    if folder.parent == activity_root() and folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    async with async_session() as session:
        await session.execute(delete(ActivityEvent).where(ActivityEvent.user_id == user_id))
        await session.execute(delete(ActivityPlace).where(ActivityPlace.user_id == user_id))
        await session.execute(delete(ActivityDevice).where(ActivityDevice.user_id == user_id))
        await session.execute(delete(ActivitySettings).where(ActivitySettings.user_id == user_id))
        await session.commit()
