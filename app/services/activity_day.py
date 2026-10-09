"""Pure, deterministic activity intervals. No network, files, or AI calls."""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

STAY_RADIUS_M = 80
GAP_MINUTES = 45
SHORT_STAY_MINUTES = 8
DAY_MINUTES = 1440
DAY_VERSION = 3


@dataclass(frozen=True)
class GeoPoint:
    at: datetime
    lat: float
    lon: float
    accuracy_m: float | None = None


@dataclass(frozen=True)
class ScreenSample:
    at: datetime
    device: str
    app: str
    title: str
    ssid: str | None = None
    device_id: int | str | None = None
    idle_seconds: float | None = None
    actor: str | None = None
    sample_minutes: int | None = None


@dataclass(frozen=True)
class PlaceSignature:
    name: str
    lat: float | None
    lon: float | None
    ssid: str | None
    created_at: datetime
    corrected_at: datetime | None = None
    radius_m: float = STAY_RADIUS_M


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


def km_from_home(meters: float) -> str:
    tenths = (Decimal(str(meters)) / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    kilometers = tenths / Decimal(10)
    return f"{kilometers:.1f} км от дома"


@dataclass
class _Stay:
    start: int
    end: int
    lat: float
    lon: float
    ssid: str | None = None
    name: str = ""


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
    origin = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    if not (
        _as_utc(origin) - timedelta(minutes=GAP_MINUTES * 2)
        <= _as_utc(value)
        < _as_utc(origin + timedelta(days=1))
    ):
        return None
    return int((_as_utc(value) - _as_utc(origin)).total_seconds() // 60)


def _iso(day: date, tz: ZoneInfo, minute: int) -> str:
    base = datetime(day.year, day.month, day.day, tzinfo=tz)
    return (_as_utc(base) + timedelta(minutes=minute)).astimezone(tz).isoformat()


def _cluster_points(
    points: list[tuple[int, float, float]],
) -> list[tuple[int, int, float, float, bool]]:
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


def _gap_kind(
    origin_lat: float, origin_lon: float, lat: float, lon: float, gap: int
) -> str:
    distance = haversine_m(origin_lat, origin_lon, lat, lon)
    if distance <= STAY_RADIUS_M:
        return "unknown" if gap > GAP_MINUTES else "stay"
    if gap <= GAP_MINUTES:
        return "travel"
    return "unknown"


def _body_pieces(
    points: list[tuple[int, float, float]], day_minutes=DAY_MINUTES
) -> list[tuple[int, int, str, float | None, float | None]]:
    if not points:
        return [(0, day_minutes, "unknown", None, None)]
    clusters = _cluster_points(points)
    pieces: list[tuple[int, int, str, float | None, float | None]] = []
    cursor = 0
    for index, (start, end, lat, lon, singleton) in enumerate(clusters):
        if start > cursor:
            if index == 0:
                kind = "unknown"
            else:
                prev_start, prev_end, prev_lat, prev_lon, prev_single = clusters[
                    index - 1
                ]
                origin = prev_start if prev_single else prev_end
                gap = start - origin
                kind = _gap_kind(prev_lat, prev_lon, lat, lon, gap)
                if kind == "stay":
                    kind = "unknown"
            pieces.append((cursor, start, kind, None, None))
        if singleton:
            if index + 1 == len(clusters):
                pieces.append((start, day_minutes, "unknown", None, None))
                cursor = day_minutes
            else:
                cursor = start
            continue
        stay_end = (
            min(day_minutes, end + GAP_MINUTES)
            if index + 1 == len(clusters)
            else end + 1
        )
        pieces.append((start, stay_end, "stay", lat, lon))
        cursor = stay_end
    if cursor < day_minutes:
        pieces.append((cursor, day_minutes, "unknown", None, None))
    return _merge_kinds(pieces)


def _merge_kinds(pieces):
    merged = []
    for start, end, kind, lat, lon in pieces:
        if start >= end:
            continue
        if (
            merged
            and merged[-1][2] == kind
            and kind != "stay"
            and merged[-1][1] == start
        ):
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


def _nearest_home(
    lat: float, lon: float, signatures: list[PlaceSignature]
) -> PlaceSignature | None:
    homes = []
    for signature in signatures:
        if signature.lat is None or signature.lon is None:
            continue
        if signature.name.casefold() != "дом":
            continue
        homes.append(signature)
    if not homes:
        return None
    homes.sort(
        key=lambda item: (
            round(haversine_m(lat, lon, item.lat, item.lon)),
            _as_utc(item.created_at),
        )
    )
    best = homes[0]
    best_distance = haversine_m(lat, lon, best.lat, best.lon)
    tied = [
        item
        for item in homes
        if abs(haversine_m(lat, lon, item.lat, item.lon) - best_distance) < 0.5
    ]
    tied.sort(key=lambda item: _as_utc(item.created_at))
    return tied[0]


def _draft_name(
    lat: float, lon: float, ssid: str | None, signatures: list[PlaceSignature]
) -> str:
    home = _nearest_home(lat, lon, signatures)
    if home is None:
        return "без имени"
    return km_from_home(haversine_m(lat, lon, home.lat, home.lon))


def _lookup_stay(stays: list[_Stay], minute: int) -> _Stay | None:
    for stay in stays:
        if stay.start <= minute < stay.end:
            return stay
    return None


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


def _block_box(row: dict, origin=None, day_minutes=DAY_MINUTES) -> dict | None:
    try:
        begin, minutes = _span_minutes(row["start"], row["end"])
        duration = (
            _as_utc(datetime.fromisoformat(row["end"]))
            - _as_utc(datetime.fromisoformat(row["start"]))
        ).total_seconds()
        if origin is not None:
            begin = (
                _as_utc(datetime.fromisoformat(row["start"])) - origin
            ).total_seconds() / 60
            minutes = duration / 60
    except (KeyError, TypeError, ValueError):
        return None
    title = (row.get("name") or row.get("title") or row.get("app") or "").strip()
    return {
        "top": begin / day_minutes * 100,
        "height": minutes / day_minutes * 100,
        "kind": row.get("kind") or "",
        "name": title,
        "color": _hex_color(row.get("color")),
        "start": row.get("start") or "",
        "device": row.get("device") or "",
        "seconds": max(duration, 0),
        "is_stay": row.get("kind") == "stay",
    }


def _dispute_line(row: dict) -> str:
    try:
        start = datetime.fromisoformat(row["start"])
        end = datetime.fromisoformat(row["end"])
    except (KeyError, TypeError, ValueError):
        return ""
    end_label = (
        "24:00" if end.date() != start.date() else f"{end.hour:02d}:{end.minute:02d}"
    )
    clock = f"{start.hour:02d}:{start.minute:02d}–{end_label}"
    names = []
    for item in row.get("screens") or []:
        name = item.get("device") or ""
        if name and name not in names:
            names.append(name)
    if len(names) <= 1:
        who = names[0] if names else "устройства"
    elif len(names) == 2:
        who = f"{names[0]} и {names[1]}"
    else:
        who = ", ".join(names[:-1]) + " и " + names[-1]
    winner = row.get("winner") or ""
    return f"{clock} — {who} бодрствовали. Экран отдан {winner}."


def _hex_color(value) -> str:
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return ""
    if any(char not in "0123456789abcdefABCDEF" for char in value[1:]):
        return ""
    return value


def _match_signature(lat, lon, ssid, signatures, at):
    matches = []
    for s in signatures:
        if s.lat is None or s.lon is None or _as_utc(s.created_at) > _as_utc(at):
            continue
        if ssid and s.ssid and ssid != s.ssid:
            continue
        distance = haversine_m(lat, lon, s.lat, s.lon)
        if distance <= s.radius_m:
            matches.append((distance, -_as_utc(s.created_at).timestamp(), s))
    return min(matches, key=lambda x: (round(x[0], 1), x[1]))[2] if matches else None


def _union_seconds(rows):
    spans = sorted(
        (
            _as_utc(datetime.fromisoformat(r["start"])),
            _as_utc(datetime.fromisoformat(r["end"])),
        )
        for r in rows
    )
    total = 0.0
    end = None
    for start, stop in spans:
        if end is not None:
            start = max(start, end)
        if stop > start:
            total += (stop - start).total_seconds()
        end = max(end, stop) if end else stop
    return total


def _collapse(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row.get("device_id", row.get("device", "")), []).append(row)
    result = []
    for items in groups.values():
        joined = []
        for row in sorted(
            items, key=lambda r: _as_utc(datetime.fromisoformat(r["start"]))
        ):
            if (
                joined
                and joined[-1]["end"] == row["start"]
                and all(
                    joined[-1].get(k) == row.get(k)
                    for k in ("app", "title", "state", "reason")
                )
            ):
                joined[-1]["end"] = row["end"]
            else:
                joined.append(dict(row))
        result.extend(joined)
    return sorted(
        result,
        key=lambda r: (
            _as_utc(datetime.fromisoformat(r["start"])),
            str(r.get("device_id", "")),
        ),
    )


def merge_day(
    day,
    tz,
    points,
    screens,
    signatures=None,
    sample_minutes=1,
    idle_minutes=3,
    now=None,
):
    """Sweep interval boundaries once. Screen durations do not depend on title stability."""
    signatures = list(signatures or [])
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    start = _as_utc(start_local)
    end = _as_utc(start_local + timedelta(days=1))
    limit = min(end, _as_utc(now)) if now is not None else end
    located = []
    poor_points = 0
    for p in sorted(points, key=lambda p: _as_utc(p.at)):
        minute = _local_minutes(p.at, tz, day)
        if minute is None:
            continue
        if p.accuracy_m is not None and (
            not math.isfinite(p.accuracy_m)
            or p.accuracy_m > STAY_RADIUS_M
            or p.accuracy_m < 0
        ):
            poor_points += 1
            continue
        if not located or minute != located[-1][0]:
            located.append((minute, p.lat, p.lon))
    day_minutes = int((end - start).total_seconds() / 60)
    pieces = _shorten_stays(_body_pieces(located, day_minutes))
    body = []
    for begin, stop, kind, lat, lon in pieces:
        a = max(start, _as_utc(datetime.fromisoformat(_iso(day, tz, begin))))
        b = min(_as_utc(datetime.fromisoformat(_iso(day, tz, stop))), limit)
        if b <= a:
            continue
        ssids = {s.ssid for s in screens if s.ssid and a <= _as_utc(s.at) < b}
        ssid = next(iter(ssids)) if len(ssids) == 1 else None
        label_times = {
            _as_utc(s.created_at)
            for s in signatures
            if lat is not None
            and s.lat is not None
            and s.lon is not None
            and a < _as_utc(s.created_at) < b
            and haversine_m(lat, lon, s.lat, s.lon) <= s.radius_m
        }
        edges = [a, *sorted(label_times), b]
        for left, right in zip(edges, edges[1:]):
            match = (
                _match_signature(lat, lon, ssid, signatures, left)
                if lat is not None
                else None
            )
            name = (
                match.name
                if match
                else (
                    _draft_name(lat, lon, ssid, signatures)
                    if lat is not None
                    else ("Нет свежей геолокации" if kind == "unknown" else "Дорога")
                )
            )
            body.append(
                dict(
                    start=left.astimezone(tz).isoformat(),
                    end=right.astimezone(tz).isoformat(),
                    kind=kind,
                    name=name,
                    center=None if lat is None else dict(lat=lat, lon=lon),
                    ssid=ssid,
                )
            )

    by_device = {}
    for s in sorted(screens, key=lambda s: _as_utc(s.at)):
        identity = s.device_id if s.device_id is not None else s.device
        by_device.setdefault(identity, []).append(s)
    spans = []
    for identity, samples in by_device.items():
        run_start = None
        previous = None
        previous_at = start
        for index, s in enumerate(samples):
            at = _as_utc(s.at)
            interval = timedelta(
                minutes=max(1, min(60, s.sample_minutes or sample_minutes))
            )
            following = (
                _as_utc(samples[index + 1].at) if index + 1 < len(samples) else None
            )
            stop = (
                following
                if following is not None and following - at <= interval * 2
                else at + interval
            )
            stop = min(stop, limit)
            at = max(at, start)
            if stop <= at:
                continue
            same = (
                previous is not None
                and (previous.app, previous.title) == (s.app, s.title)
                and at - previous_at <= interval * 2
            )
            run_start = run_start if same else at
            previous = s
            previous_at = at
            is_agent = s.actor == "agent" or "computer use" in s.title.casefold()
            state = (
                "agent"
                if is_agent
                else (
                    "idle"
                    if s.idle_seconds is not None
                    and s.idle_seconds >= idle_minutes * 60
                    else "observed"
                )
            )
            spans.append(
                dict(
                    start=at,
                    end=stop,
                    sample=s,
                    id=identity,
                    run_start=run_start,
                    state=state,
                )
            )
    # Active sets are updated only when an interval starts or ends, not once per minute per block.
    boundaries = {}
    for index, s in enumerate(spans):
        boundaries.setdefault(s["start"], []).append((True, index))
        boundaries.setdefault(s["end"], []).append((False, index))
    for row in body:
        boundaries.setdefault(_as_utc(datetime.fromisoformat(row["start"])), [])
        boundaries.setdefault(_as_utc(datetime.fromisoformat(row["end"])), [])
    moments = sorted(boundaries)
    active = {}
    body_index = 0
    human = []
    agents = []
    uncertain = []
    disputes = []
    for index, moment in enumerate(moments[:-1]):
        for add, i in boundaries[moment]:
            if add:
                active[i] = spans[i]
            else:
                active.pop(i, None)
        stop = moments[index + 1]
        if stop <= moment:
            continue
        while (
            body_index < len(body)
            and _as_utc(datetime.fromisoformat(body[body_index]["end"])) <= moment
        ):
            body_index += 1
        place = body[body_index] if body_index < len(body) else None
        candidates = []
        for s in active.values():
            sample = s["sample"]
            reason = ""
            if s["state"] == "agent":
                target = agents
                reason = "Признак автоматизации в заголовке или клиенте"
            elif s["state"] == "idle":
                target = uncertain
                reason = "Нет ввода; чтение и просмотр неотличимы от простоя"
            else:
                center = (place or {}).get("center") or {}
                known = [
                    p
                    for p in signatures
                    if sample.ssid
                    and p.ssid == sample.ssid
                    and _as_utc(p.created_at) <= moment
                ]
                remote = (
                    known
                    and center
                    and all(
                        p.lat is not None
                        and p.lon is not None
                        and haversine_m(center["lat"], center["lon"], p.lat, p.lon)
                        > p.radius_m
                        for p in known
                    )
                )
                if remote:
                    target = uncertain
                    reason = "Устройство наблюдается в другом месте"
                else:
                    candidates.append(s)
                    continue
            target.append(
                dict(
                    start=moment.astimezone(tz).isoformat(),
                    end=stop.astimezone(tz).isoformat(),
                    device=sample.device,
                    device_id=s["id"],
                    app=sample.app,
                    title=sample.title or sample.app,
                    state=s["state"],
                    reason=reason,
                )
            )
        if candidates:
            winner = min(candidates, key=lambda s: (s["run_start"], str(s["id"])))
            for s in candidates:
                sample = s["sample"]
                chosen = s is winner
                (human if chosen else uncertain).append(
                    dict(
                        start=moment.astimezone(tz).isoformat(),
                        end=stop.astimezone(tz).isoformat(),
                        device=sample.device,
                        device_id=s["id"],
                        app=sample.app,
                        title=sample.title or sample.app,
                        state="observed" if chosen else "conflict",
                        reason=""
                        if chosen
                        else "Одновременно наблюдаются несколько устройств",
                    )
                )
            if len(candidates) > 1:
                dispute = dict(
                    start=moment.astimezone(tz).isoformat(),
                    end=stop.astimezone(tz).isoformat(),
                    winner=winner["sample"].device,
                    winner_id=winner["id"],
                    screens=sorted(
                        [
                            dict(device=s["sample"].device, device_id=s["id"])
                            for s in candidates
                        ],
                        key=lambda s: str(s["device_id"]),
                    ),
                )
                if (
                    disputes
                    and disputes[-1]["end"] == dispute["start"]
                    and all(
                        disputes[-1][k] == dispute[k] for k in ("winner_id", "screens")
                    )
                ):
                    disputes[-1]["end"] = dispute["end"]
                else:
                    disputes.append(dispute)
    return dict(
        version=DAY_VERSION,
        source="deterministic",
        body=body,
        screen=_collapse(human),
        agent=_collapse(agents),
        uncertain=_collapse(uncertain),
        disputes=disputes,
        poor_location_points=poor_points,
    )


def present_day(document, selected_start=None):
    origin = None
    day_minutes = DAY_MINUTES
    tz = None
    if document.get("day") and document.get("timezone"):
        tz = ZoneInfo(document["timezone"])
        day = date.fromisoformat(document["day"])
        local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        origin = _as_utc(local)
        day_minutes = int(
            (_as_utc(local + timedelta(days=1)) - origin).total_seconds() / 60
        )

    def boxes(key):
        return [
            box
            for row in document.get(key, [])
            if (box := _block_box(row, origin, day_minutes)) is not None
        ]

    body = boxes("body")
    screens = boxes("screen")
    agents = boxes("agent")
    uncertain = boxes("uncertain")
    for box in body:
        box["selected"] = box["is_stay"] and box["start"] == selected_start
    selected = next((b for b in body if b["selected"]), None)
    hours = [
        dict(
            label="24:00"
            if m == day_minutes
            else (
                (origin + timedelta(minutes=m)).astimezone(tz).strftime("%H:%M")
                if origin
                else f"{m // 60:02d}:{m % 60:02d}"
            ),
            top=m / day_minutes * 100,
            last=m == day_minutes,
            major=m % 60 == 0,
        )
        for m in range(0, day_minutes + 1, 15)
    ]
    columns = [
        dict(title="тело", kind="", blocks=body),
        dict(title="экран", kind="screen", blocks=screens),
        dict(title="агент", kind="agent", blocks=agents),
    ]
    if uncertain:
        columns.append(
            dict(title="неопределённость", kind="uncertain", blocks=uncertain)
        )
    return dict(
        day_minutes=day_minutes,
        body_blocks=body,
        screen_blocks=screens,
        agent_blocks=agents,
        columns=columns,
        hours=hours,
        disputes=[
            line for r in document.get("disputes", []) if (line := _dispute_line(r))
        ],
        screen_sum=_clock_label(_union_seconds(document.get("screen", []))),
        agent_sum=_clock_label(_union_seconds(document.get("agent", []))),
        selected_start=selected["start"] if selected else "",
        selected_name=selected["name"] if selected else "",
        day_error=bool(document.get("error")),
        poor_location_points=document.get("poor_location_points", 0),
    )
