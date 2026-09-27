#!/usr/bin/env python3
"""Aggregate one local day's screen log into calendar blocks with Codex CLI.

Reads the diary database on the host and writes
data/activity-days/<user_id>/<date>.json. The hourly crontab lives in
scripts/activity-aggregate.cron and is not installed by this script.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = Path(__file__).with_name("activity_day.schema.json")
DB_PATH = ROOT / "data" / "main-project.db"
OUT_ROOT = ROOT / "data" / "activity-days"
MODEL = "gpt-6-sol"
REASONING = "medium"
GAP_MINUTES = 20

WEEKDAYS = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]
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

PROMPT = """Ты собираешь один день личного дневника.

Вход — подробный лог за один местный день. Каждая строка это снимок переднего окна примерно раз в минуту, а не отдельное занятие. Поля: time (местное ЧЧ:ММ), device, app, title (может отсутствовать), place. Геолокации телефона во входе нет.

Верни один JSON по заданной схеме и ничего больше. Не запускай команды и не меняй файлы.

Две дорожки:
- blocks — что делал человек. Это попадёт в сумму «ты за экраном».
- agent — что делал агент на компьютере, пока человек этим не занимался. Если агента не было, верни пустой список. Не придумывай агента.

Агент:
- Заголовок с «Computer Use» — это агент, не человек и не категория «ИИ».
- Короткие переключения Finder, Safari, настроек и других окон между такими снимками — тоже агент: он кликает по экрану. Не делай из них занятия человека.
- Длинный кусок, где подряд идут ChatGPT, пустой заголовок и Computer Use, — один отрезок агента.
- Обычный ChatGPT без Computer Use, когда человек сам сидит в чате, — это человек, категория «ИИ».
- agent может совпадать по времени с blocks: человек играет на PC, а на Mac в это время работает агент. Минуты человека и минуты агента не складываются.

Человек:
- Одна шкала. Минута человека не считается дважды. Если Mac и Windows оба его, оставь занятие, которое длится дольше вокруг этой минуты.
- Соседние снимки одного занятия слей. Дыра больше 20 минут разрывает отрезок и не закрашивается.
- Конец отрезка — начало следующего другого занятия человека. Дальше дыра или конец лога — последняя отметка плюс интервал снимка.
- Если последняя отметка не старше двух интервалов от текущего времени, открытый отрезок заканчивается текущим временем. Если начало и конец совпали, конец на минуту позже.
- Заход в один-два снимка не становится своим блоком и не режет длинное занятие. Запиши его в blips этого блока. Время blip уже внутри блока, второй раз его не добавляй.
- Игры вроде dota2 — «Компьютерные игры», имя Dota 2. В device — имя компьютера из лога.
- Браузер: имя занятия — короткий смысл вкладки или сайт. Категория «Браузер», если это обычный просмотр. Явная работа или учёба — в свою категорию.
- Cursor, Terminal и редактор — «Работа». Имя — приложение или проект из заголовка.
- Finder как отдельное занятие человека — «Файлы», только если он длится несколько минут и это не клики агента.
- Пустой title не выдумывай. Telegram, почта и пароли — только имя приложения.
- place не пиши в название.

Цвета, если имя совпало:
- Компьютерные игры #6d4aff
- Работа #1a73e8
- Учёба #0d904f
- Браузер #e8710a
- Музыка #d81b60
- Мессенджеры #00897b
- ИИ #7c3aed
- Файлы #8e24aa
Новой категории — латинский id и другой цвет #RRGGBB.
Время строго ЧЧ:ММ. Блоки человека по возрастанию времени. У блока есть хотя бы одно занятие внутри его границ. blips тоже внутри границ. Заметка агента — короткая, по-русски.
"""


def codex_bin() -> str:
    found = shutil.which("codex")
    if found:
        return found
    for candidate in ("/usr/local/bin/codex", "/root/.local/bin/codex"):
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("codex not found")


def parse_utc(value: str) -> datetime:
    raw = value.strip().replace("Z", "")
    if "." in raw:
        raw = raw.split(".", 1)[0]
    parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=timezone.utc)


def hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def load_users(con: sqlite3.Connection, only_user: int | None) -> list[sqlite3.Row]:
    if only_user is not None:
        rows = list(
            con.execute(
                "select user_id, timezone, sample_minutes from activity_settings where user_id = ?",
                (only_user,),
            )
        )
    else:
        rows = list(con.execute("select user_id, timezone, sample_minutes from activity_settings"))
    if not rows:
        raise SystemExit("no activity settings")
    return rows


def screen_log(con: sqlite3.Connection, user_id: int, start: datetime, end: datetime, tz: ZoneInfo) -> list[dict]:
    rows = con.execute(
        """
        select e.recorded_at, e.app_name, e.window_title, d.name, d.platform, p.name
        from activity_events e
        join activity_devices d on d.id = e.device_id and d.user_id = e.user_id
        left join activity_places p on p.id = e.place_id and p.user_id = e.user_id
        where e.user_id = ? and e.kind = 'screen'
          and e.recorded_at >= ? and e.recorded_at < ?
        order by e.recorded_at
        """,
        (user_id, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
    )
    items = []
    for recorded_at, app_name, title, device, platform, place in rows:
        local = parse_utc(recorded_at).astimezone(tz)
        app = (app_name or "").strip()
        window = (title or "").strip()
        if not app and not window:
            continue
        item = {
            "time": hhmm(local),
            "device": device,
            "platform": platform,
            "app": app or None,
        }
        if window:
            item["title"] = window[:180]
        if place:
            item["place"] = place
        items.append(item)
    return items


def extract_json(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no json object in codex reply")
    return json.loads(raw[start : end + 1])


def clock(total: int) -> str:
    if total >= 24 * 60:
        return "23:59"
    return f"{total // 60:02d}:{total % 60:02d}"


def _stretch_pair(item: dict) -> None:
    if minutes(item["end"]) <= minutes(item["start"]):
        item["end"] = clock(minutes(item["start"]) + 1)


def stretch_open_points(payload: dict) -> None:
    """A sample in the current minute has no duration yet. Give it one minute so it stays visible."""
    payload.setdefault("agent", [])
    for block in payload["blocks"]:
        block.setdefault("blips", [])
        _stretch_pair(block)
        for item in block["items"]:
            _stretch_pair(item)
        for blip in block["blips"]:
            _stretch_pair(blip)
        ends = [minutes(item["end"]) for item in block["items"]]
        ends.extend(minutes(blip["end"]) for blip in block["blips"])
        if ends and max(ends) > minutes(block["end"]):
            block["end"] = clock(max(ends))
    for span in payload["agent"]:
        _stretch_pair(span)


def validate(payload: dict) -> None:
    categories = {item["id"] for item in payload["categories"]}
    if len(categories) != len(payload["categories"]):
        raise ValueError("duplicate category id")
    previous = -1
    for block in payload["blocks"]:
        if block["category"] not in categories:
            raise ValueError(f"unknown category {block['category']}")
        start = minutes(block["start"])
        end = minutes(block["end"])
        if end <= start:
            raise ValueError(f"block {block['start']}–{block['end']} is empty")
        if start < previous:
            raise ValueError("blocks are not in order")
        previous = start
        for item in block["items"]:
            item_start = minutes(item["start"])
            item_end = minutes(item["end"])
            if item_end <= item_start:
                raise ValueError(f"item {item['name']} is empty")
            if item_start < start or item_end > end:
                raise ValueError(f"item {item['name']} sticks out of its category")
        for blip in block.get("blips") or []:
            blip_start = minutes(blip["start"])
            blip_end = minutes(blip["end"])
            if blip_end <= blip_start:
                raise ValueError(f"blip {blip['name']} is empty")
            if blip_start < start or blip_end > end:
                raise ValueError(f"blip {blip['name']} sticks out of its block")
    for span in payload.get("agent") or []:
        if minutes(span["end"]) <= minutes(span["start"]):
            raise ValueError(f"agent {span['start']}–{span['end']} is empty")
        if not str(span.get("note") or "").strip():
            raise ValueError("agent note is empty")


def run_codex(prompt: str, work: Path) -> str:
    out = work / "last.md"
    log = work / "codex.log"
    cmd = [
        codex_bin(),
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "-m",
        MODEL,
        "-c",
        f'model_reasoning_effort="{REASONING}"',
        "--output-schema",
        str(SCHEMA),
        "-o",
        str(out),
        "-C",
        "/tmp",
        "-",
    ]
    completed = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        timeout=900,
        check=False,
    )
    log.write_text(completed.stderr + "\n" + completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise SystemExit(f"codex exited {completed.returncode}, see {log}")
    if not out.is_file():
        raise SystemExit(f"codex wrote no message, see {log}")
    return out.read_text(encoding="utf-8")


def aggregate_user(con: sqlite3.Connection, row: sqlite3.Row, day: date | None) -> Path | None:
    tz = ZoneInfo(row["timezone"] or "Asia/Almaty")
    now = datetime.now(tz)
    target = day or now.date()
    start = datetime.combine(target, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    sample = max(1, int(row["sample_minutes"] or 1))
    events = screen_log(
        con,
        row["user_id"],
        start.astimezone(timezone.utc),
        end.astimezone(timezone.utc),
        tz,
    )
    work = OUT_ROOT / str(row["user_id"])
    work.mkdir(parents=True, exist_ok=True)
    if not events:
        print(f"user {row['user_id']} {target.isoformat()}: no screen log")
        return None
    source = {
        "date": target.isoformat(),
        "timezone": str(tz),
        "now": hhmm(now) if target == now.date() else None,
        "sample_minutes": sample,
        "gap_minutes": GAP_MINUTES,
        "events": events,
    }
    (work / f"{target.isoformat()}.log.json").write_text(
        json.dumps(source, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    prompt = PROMPT + "\nВход:\n" + json.dumps(source, ensure_ascii=False)
    reply = run_codex(prompt, work)
    try:
        parsed = extract_json(reply)
        stretch_open_points(parsed)
        validate(parsed)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        (work / f"{target.isoformat()}.raw.txt").write_text(reply, encoding="utf-8")
        raise SystemExit(f"bad codex json: {exc}") from exc
    is_today = target == now.date()
    result = {
        "date": target.isoformat(),
        "timezone": str(tz),
        "weekday": WEEKDAYS[target.weekday()],
        "day_label": f"{target.day} {MONTHS[target.month - 1]}",
        "now_label": hhmm(now) if is_today else None,
        "now_minutes": now.hour * 60 + now.minute if is_today else None,
        "categories": parsed["categories"],
        "blocks": parsed["blocks"],
        "agent": parsed.get("agent") or [],
        "model": MODEL,
        "reasoning": REASONING,
        "events": len(events),
    }
    dest = work / f"{target.isoformat()}.json"
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {dest}")
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate one activity day with Codex CLI")
    parser.add_argument("--date", help="Local day YYYY-MM-DD. Default: today in the user timezone")
    parser.add_argument("--user", type=int, help="Only this user id")
    args = parser.parse_args()
    day = date.fromisoformat(args.date) if args.date else None
    if not DB_PATH.is_file():
        raise SystemExit(f"database not found: {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    wrote = False
    for row in load_users(con, args.user):
        if aggregate_user(con, row, day):
            wrote = True
    if not wrote:
        raise SystemExit("nothing to aggregate")


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired:
        sys.exit("codex timed out")
