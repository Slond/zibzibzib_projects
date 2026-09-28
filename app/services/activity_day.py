"""Build one layered activity day from raw points and window samples.

The merger is deterministic. It does not call Codex and it ignores
``activity_events.place_id``. Naive datetimes are UTC.
"""

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

STAY_RADIUS_M = 80
GAP_MINUTES = 45
SHORT_STAY_MINUTES = 8
DAY_MINUTES = 24 * 60


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def km_from_home(meters: float) -> str:
    tenths = (Decimal(str(meters)) / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    kilometers = tenths / Decimal(10)
    return f"{kilometers:.1f} км от дома"


@dataclass(frozen=True)
class GeoPoint:
    at: datetime
    lat: float
    lon: float


@dataclass(frozen=True)
class ScreenSample:
    at: datetime
    device: str
    app: str
    title: str
    ssid: str | None = None


@dataclass(frozen=True)
class PlaceSignature:
    name: str
    lat: float | None
    lon: float | None
    ssid: str | None
    created_at: datetime
    corrected_at: datetime | None = None


@dataclass
class _Stay:
    start: int
    end: int
    lat: float
    lon: float
    ssid: str | None = None
    name: str = ""


@dataclass
class _Block:
    device: str
    app: str
    title: str
    ssid: str | None
    start: datetime
    end: datetime
    samples: tuple


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _local_minutes(value: datetime, tz: ZoneInfo, day: date) -> int | None:
    local = _as_utc(value).astimezone(tz)
    if local.date() != day:
        return None
    return local.hour * 60 + local.minute


def _iso(day: date, tz: ZoneInfo, minute: int) -> str:
    base = datetime(day.year, day.month, day.day, tzinfo=tz)
    return (base + timedelta(minutes=minute)).isoformat()


def _signature_stamp(signature: PlaceSignature) -> datetime:
    return signature.corrected_at or signature.created_at


def applicable_signatures(signatures: list[PlaceSignature], day: date, tz: ZoneInfo) -> list[PlaceSignature]:
    kept = []
    for signature in signatures:
        stamp = _as_utc(_signature_stamp(signature)).astimezone(tz)
        if stamp.date() <= day:
            kept.append(signature)
    return kept


def _cluster_points(points: list[tuple[int, float, float]]) -> list[tuple[int, int, float, float, bool]]:
    clusters = []
    index = 0
    count = len(points)
    while index < count:
        start_minute, lat, lon = points[index]
        last = index
        probe = index + 1
        while probe < count:
            gap = points[probe][0] - points[last][0]
            distance = haversine_m(lat, lon, points[probe][1], points[probe][2])
            if distance <= STAY_RADIUS_M and 0 < gap <= GAP_MINUTES:
                last = probe
                probe += 1
                continue
            break
        end_minute = points[last][0]
        clusters.append((start_minute, end_minute, lat, lon, last == index))
        index = last + 1
    return clusters


def _gap_kind(origin_lat: float, origin_lon: float, lat: float, lon: float, gap: int) -> str:
    distance = haversine_m(origin_lat, origin_lon, lat, lon)
    if distance <= STAY_RADIUS_M:
        return "unknown" if gap > GAP_MINUTES else "stay"
    if gap <= GAP_MINUTES:
        return "travel"
    return "unknown"


def _body_pieces(points: list[tuple[int, float, float]]) -> list[tuple[int, int, str, float | None, float | None]]:
    if not points:
        return [(0, DAY_MINUTES, "unknown", None, None)]
    clusters = _cluster_points(points)
    pieces: list[tuple[int, int, str, float | None, float | None]] = []
    cursor = 0
    for index, (start, end, lat, lon, singleton) in enumerate(clusters):
        if start > cursor:
            if index == 0:
                kind = "unknown"
            else:
                prev_start, prev_end, prev_lat, prev_lon, prev_single = clusters[index - 1]
                origin = prev_start if prev_single else prev_end
                gap = start - origin
                kind = _gap_kind(prev_lat, prev_lon, lat, lon, gap)
                if kind == "stay":
                    kind = "unknown"
            pieces.append((cursor, start, kind, None, None))
        if singleton:
            if index + 1 == len(clusters):
                pieces.append((start, DAY_MINUTES, "unknown", None, None))
                cursor = DAY_MINUTES
            else:
                cursor = start
            continue
        stay_end = DAY_MINUTES if index + 1 == len(clusters) else end + 1
        pieces.append((start, stay_end, "stay", lat, lon))
        cursor = stay_end
    if cursor < DAY_MINUTES:
        pieces.append((cursor, DAY_MINUTES, "unknown", None, None))
    return _merge_kinds(pieces)


def _merge_kinds(pieces):
    merged = []
    for start, end, kind, lat, lon in pieces:
        if start >= end:
            continue
        if merged and merged[-1][2] == kind and kind != "stay" and merged[-1][1] == start:
            prev = merged[-1]
            merged[-1] = (prev[0], end, kind, None, None)
            continue
        merged.append((start, end, kind, lat, lon))
    return merged


def _shorten_stays(pieces):
    adjusted = []
    for index, piece in enumerate(pieces):
        start, end, kind, lat, lon = piece
        if kind != "stay" or end - start >= SHORT_STAY_MINUTES:
            adjusted.append(piece)
            continue
        previous = pieces[index - 1][2] if index else None
        following = pieces[index + 1][2] if index + 1 < len(pieces) else None
        if previous == "travel" and following == "travel":
            adjusted.append((start, end, "travel", None, None))
        elif previous in (None, "unknown") or following in (None, "unknown"):
            adjusted.append((start, end, "unknown", None, None))
        else:
            adjusted.append((start, end, "stay", lat, lon))
    return _merge_kinds(adjusted)


def _blocks(samples: list[ScreenSample], tz: ZoneInfo, day: date, sample_minutes: int) -> list[_Block]:
    fresh = timedelta(minutes=2 * sample_minutes)
    ordered = sorted(samples, key=lambda item: _as_utc(item.at))
    by_device: dict[str, list[ScreenSample]] = {}
    for sample in ordered:
        if _local_minutes(sample.at, tz, day) is None:
            continue
        by_device.setdefault(sample.device, []).append(sample)
    blocks = []
    for device, rows in by_device.items():
        current: list[ScreenSample] = []
        for sample in rows:
            if not current:
                current = [sample]
                continue
            previous = current[-1]
            same = (previous.app or "") == (sample.app or "") and (previous.title or "") == (sample.title or "")
            gap = _as_utc(sample.at) - _as_utc(previous.at)
            if same and gap < fresh:
                current.append(sample)
                continue
            blocks.append(_close_block(device, current))
            current = [sample]
        if current:
            blocks.append(_close_block(device, current))
    return blocks


def _close_block(device: str, samples: list[ScreenSample]) -> _Block:
    first, last = samples[0], samples[-1]
    ssids = []
    for sample in samples:
        ssid = _blank(sample.ssid)
        if ssid and ssid not in ssids:
            ssids.append(ssid)
    return _Block(
        device=device,
        app=first.app or "",
        title=first.title or "",
        ssid=ssids[0] if len(ssids) == 1 else None,
        start=_as_utc(first.at),
        end=_as_utc(last.at),
        samples=tuple(_as_utc(sample.at) for sample in samples),
    )


def _covers(block: _Block, minute_start: datetime, fresh: timedelta) -> bool:
    window_end = minute_start + timedelta(minutes=1)
    for sample in block.samples:
        if minute_start <= sample < window_end:
            return True
        if sample < minute_start and minute_start - sample < fresh:
            return True
    return False


def _excluded(block: _Block, lat: float | None, lon: float | None, signatures: list[PlaceSignature]) -> bool:
    ssid = _blank(block.ssid)
    if ssid is None or lat is None or lon is None:
        return False
    matched = [item for item in signatures if _blank(item.ssid) == ssid]
    if not matched:
        return False
    for item in matched:
        if item.lat is None or item.lon is None:
            continue
        if haversine_m(lat, lon, item.lat, item.lon) <= STAY_RADIUS_M:
            return False
    return True


def _computer_use(title: str) -> bool:
    return "Computer Use" in title


def _winner(blocks: list[_Block], minute_start: datetime) -> _Block:
    pool = [block for block in blocks if not _computer_use(block.title)]
    if not pool:
        pool = blocks

    def key(block: _Block):
        length = (minute_start - block.start).total_seconds()
        return (-length, block.start.timestamp(), block.device)

    return min(pool, key=key)


def _nearest_home(lat: float, lon: float, signatures: list[PlaceSignature]) -> PlaceSignature | None:
    homes = []
    for signature in signatures:
        if signature.lat is None or signature.lon is None:
            continue
        if signature.name.casefold() != "дом":
            continue
        homes.append(signature)
    if not homes:
        return None
    homes.sort(key=lambda item: (round(haversine_m(lat, lon, item.lat, item.lon)), _as_utc(item.created_at)))
    best = homes[0]
    best_distance = haversine_m(lat, lon, best.lat, best.lon)
    tied = [
        item
        for item in homes
        if abs(haversine_m(lat, lon, item.lat, item.lon) - best_distance) < 0.5
    ]
    tied.sort(key=lambda item: _as_utc(item.created_at))
    return tied[0]


def _match_signature(lat: float, lon: float, ssid: str | None, signatures: list[PlaceSignature]) -> PlaceSignature | None:
    found = []
    for signature in signatures:
        if signature.lat is None or signature.lon is None:
            continue
        if haversine_m(lat, lon, signature.lat, signature.lon) > STAY_RADIUS_M:
            continue
        stay_ssid = _blank(ssid)
        place_ssid = _blank(signature.ssid)
        if stay_ssid and place_ssid and stay_ssid != place_ssid:
            continue
        found.append(signature)
    if not found:
        return None
    found.sort(key=lambda item: (haversine_m(lat, lon, item.lat, item.lon), _as_utc(item.created_at)))
    return found[0]


def _draft_name(lat: float, lon: float, ssid: str | None, signatures: list[PlaceSignature]) -> str:
    if _blank(ssid):
        return _blank(ssid)
    home = _nearest_home(lat, lon, signatures)
    if home is None:
        return "без имени"
    return km_from_home(haversine_m(lat, lon, home.lat, home.lon))


def _stay_ssid(stay: _Stay, minute_blocks: list[list[_Block]], signatures: list[PlaceSignature]) -> str | None:
    seen = []
    for minute in range(stay.start, stay.end):
        for block in minute_blocks[minute]:
            if _excluded(block, stay.lat, stay.lon, signatures):
                continue
            ssid = _blank(block.ssid)
            if ssid not in seen:
                seen.append(ssid)
    present = [item for item in seen if item]
    if len(present) == 1 and seen == present:
        return present[0]
    if len(present) > 1:
        return None
    return None


def _name_stays(pieces, minute_blocks, signatures: list[PlaceSignature]) -> list[_Stay]:
    stays = []
    for start, end, kind, lat, lon in pieces:
        if kind != "stay":
            continue
        stay = _Stay(start, end, lat, lon)
        if end - start < SHORT_STAY_MINUTES:
            stays.append(stay)
            continue
        stay.ssid = _stay_ssid(stay, minute_blocks, signatures)
        signature = _match_signature(lat, lon, stay.ssid, signatures)
        if signature is not None:
            stay.name = signature.name
        else:
            stay.name = _draft_name(lat, lon, stay.ssid, signatures)
        stays.append(stay)
    return stays


def _lookup_stay(stays: list[_Stay], minute: int) -> _Stay | None:
    for stay in stays:
        if stay.start <= minute < stay.end:
            return stay
    return None


def _collapse(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    merged = [dict(rows[0])]
    for row in rows[1:]:
        current = merged[-1]
        same = row["start"] == current["end"] and all(
            row[key] == current[key] for key in row if key not in ("start", "end")
        )
        if same:
            current["end"] = row["end"]
            continue
        merged.append(dict(row))
    return merged


def merge_day(
    day: date,
    tz: ZoneInfo,
    points: list[GeoPoint],
    screens: list[ScreenSample],
    signatures: list[PlaceSignature] | None = None,
    sample_minutes: int = 1,
) -> dict:
    signatures = applicable_signatures(list(signatures or []), day, tz)
    located = []
    for point in sorted(points, key=lambda item: _as_utc(item.at)):
        minute = _local_minutes(point.at, tz, day)
        if minute is None:
            continue
        if located and located[-1][0] == minute:
            continue
        located.append((minute, point.lat, point.lon))
    pieces = _shorten_stays(_body_pieces(located))
    fresh = timedelta(minutes=2 * max(1, sample_minutes))
    blocks = _blocks(screens, tz, day, max(1, sample_minutes))
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    minute_blocks: list[list[_Block]] = []
    for minute in range(DAY_MINUTES):
        moment = day_start + timedelta(minutes=minute)
        minute_blocks.append([block for block in blocks if _covers(block, _as_utc(moment), fresh)])
    stays = _name_stays(pieces, minute_blocks, signatures)
    body = []
    screen_rows = []
    agent_rows = []
    dispute_rows = []
    for start, end, kind, lat, lon in pieces:
        stay = _lookup_stay(stays, start) if kind == "stay" else None
        body.append(
            {
                "start": _iso(day, tz, start),
                "end": _iso(day, tz, end),
                "kind": kind,
                "name": stay.name if stay else "",
                "center": None if lat is None else {"lat": lat, "lon": lon},
                "ssid": stay.ssid if stay else None,
            }
        )
    for minute, active in enumerate(minute_blocks):
        stay = _lookup_stay(stays, minute)
        lat = stay.lat if stay else None
        lon = stay.lon if stay else None
        kept = [block for block in active if not _excluded(block, lat, lon, signatures)]
        rejected = [block for block in active if block not in kept]
        person = None
        dispute = False
        if len(kept) == 1:
            person = kept[0]
        elif len(kept) > 1:
            dispute = True
            person = _winner(kept, _as_utc(day_start + timedelta(minutes=minute)))
        agents = [block for block in active if block is not person]
        start = _iso(day, tz, minute)
        end = _iso(day, tz, minute + 1)
        if person is not None:
            screen_rows.append(
                {
                    "start": start,
                    "end": end,
                    "app": person.app,
                    "title": person.title,
                    "device": person.device,
                }
            )
        for block in agents:
            agent_rows.append(
                {
                    "start": start,
                    "end": end,
                    "app": block.app,
                    "title": block.title,
                    "device": block.device,
                }
            )
        if dispute and person is not None:
            dispute_rows.append(
                {
                    "start": start,
                    "end": end,
                    "screens": sorted(
                        (
                            {"device": block.device, "app": block.app, "title": block.title}
                            for block in kept
                        ),
                        key=lambda item: item["device"],
                    ),
                    "winner": person.device,
                }
            )
    return {
        "body": body,
        "screen": _collapse(screen_rows),
        "agent": _collapse_named(agent_rows),
        "disputes": _collapse(dispute_rows),
    }


def _collapse_named(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["device"], []).append(row)
    merged = []
    for device in sorted(grouped):
        merged.extend(_collapse(grouped[device]))
    return merged


def reapply_signatures(
    document: dict,
    day: date,
    tz: ZoneInfo,
    signatures: list[PlaceSignature],
) -> bool:
    """Rename stays from signatures. Leave geometry where it is."""
    applicable = applicable_signatures(signatures, day, tz)
    changed = False
    for segment in document.get("body") or []:
        if segment.get("kind") != "stay":
            continue
        center = segment.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
        if lat is None or lon is None:
            continue
        try:
            start = datetime.fromisoformat(segment["start"])
            end = datetime.fromisoformat(segment["end"])
        except (KeyError, TypeError, ValueError):
            continue
        minutes = int((end - start).total_seconds() // 60)
        if minutes < SHORT_STAY_MINUTES:
            if segment.get("name"):
                segment["name"] = ""
                changed = True
            continue
        match = _match_signature(lat, lon, segment.get("ssid"), applicable)
        if match and segment.get("name") != match.name:
            segment["name"] = match.name
            changed = True
    return changed


def _span_minutes(start_iso: str, end_iso: str) -> tuple[int, int]:
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    begin = start.hour * 60 + start.minute
    if end.date() != start.date():
        finish = DAY_MINUTES
    else:
        finish = end.hour * 60 + end.minute
    if finish < begin:
        finish = DAY_MINUTES
    return begin, max(finish - begin, 0)


def _clock_label(seconds: float) -> str:
    total = int(seconds // 60)
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def _block_box(row: dict) -> dict | None:
    try:
        begin, minutes = _span_minutes(row["start"], row["end"])
        duration = (
            datetime.fromisoformat(row["end"]) - datetime.fromisoformat(row["start"])
        ).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None
    title = (row.get("name") or row.get("title") or row.get("app") or "").strip()
    return {
        "top": begin / DAY_MINUTES * 100,
        "height": minutes / DAY_MINUTES * 100,
        "kind": row.get("kind") or "",
        "name": title,
        "start": row.get("start") or "",
        "device": row.get("device") or "",
        "seconds": max(duration, 0),
        "is_stay": row.get("kind") == "stay",
    }


def _dispute_line(row: dict) -> str:
    try:
        start = datetime.fromisoformat(row["start"])
    except (KeyError, TypeError, ValueError):
        return ""
    clock = f"{start.hour:02d}:{start.minute:02d}"
    names = [item.get("device") or "" for item in row.get("screens") or []]
    names = [name for name in names if name]
    if len(names) <= 1:
        who = names[0] if names else "устройства"
    elif len(names) == 2:
        who = f"{names[0]} и {names[1]}"
    else:
        who = ", ".join(names[:-1]) + " и " + names[-1]
    winner = row.get("winner") or ""
    return f"{clock} — {who} бодрствовали. Экран отдан {winner}."


def present_day(document: dict, selected_start: str | None = None) -> dict:
    """Turn a layered day into vertical columns. Morning is the smaller top percent."""
    body = []
    screens = []
    agents = []
    screen_seconds = 0.0
    agent_seconds = 0.0
    for row in document.get("body") or []:
        box = _block_box(row)
        if box is None:
            continue
        box["selected"] = bool(selected_start) and box["is_stay"] and box["start"] == selected_start
        body.append(box)
    for row in document.get("screen") or []:
        box = _block_box(row)
        if box is None:
            continue
        screen_seconds += box["seconds"]
        screens.append(box)
    for row in document.get("agent") or []:
        box = _block_box(row)
        if box is None:
            continue
        agent_seconds += box["seconds"]
        agents.append(box)
    selected = next((item for item in body if item.get("selected")), None)
    hours = [
        {"label": f"{hour:02d}", "top": hour / 24 * 100, "last": False}
        for hour in (0, 6, 12, 18)
    ]
    hours.append({"label": "24", "top": 100, "last": True})
    disputes = [line for line in (_dispute_line(row) for row in document.get("disputes") or []) if line]
    return {
        "body_blocks": body,
        "screen_blocks": screens,
        "agent_blocks": agents,
        "columns": [
            {"title": "тело", "kind": "", "blocks": body},
            {"title": "экран", "kind": "screen", "blocks": screens},
            {"title": "агент", "kind": "agent", "blocks": agents},
        ],
        "hours": hours,
        "disputes": disputes,
        "screen_sum": _clock_label(screen_seconds),
        "agent_sum": _clock_label(agent_seconds),
        "selected_start": selected["start"] if selected else "",
        "selected_name": selected["name"] if selected else "",
        "day_error": document.get("error") == "raw",
    }


def is_old_codex(document: dict) -> bool:
    return "categories" in document or "blocks" in document


def is_layered(document: dict) -> bool:
    return isinstance(document, dict) and "body" in document and not is_old_codex(document)


def day_path(root: Path, user_id: int, day: date) -> Path:
    return root / str(user_id) / f"{day.isoformat()}.json"


def _write_json(path: Path, document: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False)
    try:
        json.dump(document, handle, ensure_ascii=False)
        handle.close()
        os.replace(handle.name, path)
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None


def read_day(
    root: Path,
    user_id: int,
    day: date,
    today: date,
    tz: ZoneInfo,
    points: list[GeoPoint],
    screens: list[ScreenSample],
    signatures: list[PlaceSignature],
    raw_latest: datetime | None,
    sample_minutes: int = 1,
    raw_failed: bool = False,
) -> dict:
    path = day_path(root, user_id, day)
    branch = "rebuild"
    existing = _read_json(path) if path.exists() else None
    if path.exists() and existing is None:
        branch = "rebuild"
    elif existing is not None and is_old_codex(existing):
        branch = "rebuild"
    elif existing is not None and is_layered(existing) and day < today:
        branch = "hit"
    elif existing is not None and is_layered(existing) and day == today:
        file_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        newer = raw_latest is not None and _as_utc(raw_latest) > file_time
        branch = "rebuild" if newer else "hit"
    elif not points and not screens:
        branch = "empty"
    if raw_failed:
        logger.info("activity read_day user=%s date=%s branch=error", user_id, day.isoformat())
        return {"error": "raw", "body": [], "screen": [], "agent": [], "disputes": []}
    if branch == "empty":
        logger.info("activity read_day user=%s date=%s branch=empty", user_id, day.isoformat())
        return {"body": [], "screen": [], "agent": [], "disputes": []}
    if branch == "hit" and existing is not None:
        if reapply_signatures(existing, day, tz, signatures):
            _write_json(path, existing)
        logger.info("activity read_day user=%s date=%s branch=hit", user_id, day.isoformat())
        return existing
    if not points and not screens:
        logger.info("activity read_day user=%s date=%s branch=empty", user_id, day.isoformat())
        return {"body": [], "screen": [], "agent": [], "disputes": []}
    document = merge_day(day, tz, points, screens, signatures, sample_minutes)
    _write_json(path, document)
    logger.info("activity read_day user=%s date=%s branch=rebuild", user_id, day.isoformat())
    return document
