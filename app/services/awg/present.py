"""Turn counters into the numbers the dashboard shows."""

from __future__ import annotations

from datetime import datetime


def format_bytes(n: int) -> str:
    value = float(max(0, n))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(value)} Б"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def format_gib(n: int) -> str:
    return f"{max(0, n) / (1024 ** 3):.2f} ГБ"


def format_rate(bps: float) -> str:
    return format_bytes(int(max(0, bps))) + "/с"


def ago(ts: int, now: int) -> str:
    if ts <= 0:
        return "не подключался"
    delta = max(0, now - ts)
    if delta < 60:
        return f"{delta} с назад"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    if delta < 86400:
        return f"{delta // 3600} ч назад"
    return f"{delta // 86400} д назад"


def start_of_local_day(now: int) -> int:
    dt = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp())


def hourly_bars(
    samples: list[tuple[int, int, int]],
    now: int,
) -> list[dict[str, object]]:
    """24 buckets ending at the current hour. samples are (ts, rx, tx)."""
    end_hour = now // 3600
    start_hour = end_hour - 23
    totals = [0] * 24
    for ts, rx, tx in samples:
        hour = ts // 3600
        idx = hour - start_hour
        if 0 <= idx < 24:
            totals[idx] += rx + tx
    peak = max(totals) or 1
    bars: list[dict[str, object]] = []
    for idx, total in enumerate(totals):
        hour = start_hour + idx
        label = datetime.fromtimestamp(hour * 3600).strftime("%H:%M")
        bars.append(
            {
                "label": label,
                "total": total,
                "height": round(total / peak * 72) if total else 0,
                "title": f"{label} — {format_bytes(total)}",
            }
        )
    return bars
