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

PROMPT = """Ты собираешь один день личного дневника в блоки календаря.

Вход — подробный лог за один местный день. Каждая строка это снимок переднего окна примерно раз в минуту, а не отдельное занятие. Поля: time (местное ЧЧ:ММ), device, app, title (может отсутствовать), place (где это было). Геолокации телефона во входе нет: календарь строится только по экрану.

Верни один JSON по заданной схеме и ничего больше. Не запускай команды и не меняй файлы.

Как склеивать время:
- Соседние снимки одного и того же занятия слей в один отрезок.
- Дыра больше 20 минут разрывает отрезок. Не растягивай занятие через такую дыру.
- Конец отрезка — начало следующего другого занятия. Если дальше дыра или это конец лога, конец — последняя отметка этого занятия плюс интервал снимка.
- Если последняя отметка не старше двух интервалов от текущего времени, последний открытый отрезок заканчивается текущим временем.
- Это одна шкала одного человека. Одна минута не считается дважды. Если в одну минуту активны и Mac, и Windows, не суммируй их и не рисуй два блока на одни и те же минуты. Оставь занятие, которое длится дольше вокруг этой минуты.
- Всплеск в один-два снимка не разрывает длинное занятие и сам блоком не становится.
- Finder, System Settings и одинокий Spotify между другим занятием пропускай. Если приложение держится несколько минут подряд, это уже занятие.

Как называть:
- Слева категория, справа конкретные занятия внутри неё.
- Подряд идущие занятия одной категории — один левый блок: от начала первого до конца последнего. Справа занятия остаются отдельными.
- Игры вроде dota2 — категория «Компьютерные игры». Имя занятия человеческое: Dota 2, не dota2.
- Браузер (Safari, Chrome и другие): имя занятия — короткий смысл вкладки или сайт, не имя браузера и не весь сырой заголовок. Категория «Браузер», если это обычный просмотр. Если вкладка явно про работу или учёбу, отнеси отрезок к этой категории.
- Cursor, Terminal и редактор кода — категория «Работа». Имя занятия — приложение или проект, если проект ясно виден в заголовке.
- Если title пустой, не выдумывай его. Для Telegram, почты и менеджеров паролей имя занятия — имя приложения.
- place не пиши в название занятия и не делай из места категорию.
- Не добавляй занятия и категории, которых нет в логе.

Цвета, если имя категории совпало, возьми эти:
- Компьютерные игры #6d4aff
- Работа #1a73e8
- Учёба #0d904f
- Браузер #e8710a
- Музыка #d81b60
- Мессенджеры #00897b
Новой категории дай латинский id и другой цвет #RRGGBB.
Время строго ЧЧ:ММ. Блоки по возрастанию времени. У каждого блока есть хотя бы одно занятие, и занятия лежат внутри блока.
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
