#!/usr/bin/env python3
"""Own the durable SQLite activity queue. Filename kept for service compatibility."""

import fcntl
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.activity_pipeline import process_day

DB = Path(os.environ.get("ACTIVITY_DB", ROOT / "data/main-project.db"))


def run_once():
    with sqlite3.connect(DB, timeout=30) as con:
        jobs = con.execute(
            "SELECT user_id,day,revision FROM activity_days WHERE (revision>processed_revision OR next_run_at IS NOT NULL) AND (next_run_at IS NULL OR next_run_at<=?) AND attempts<5 ORDER BY day DESC LIMIT 20",
            (str(datetime.now(timezone.utc).replace(tzinfo=None)),),
        ).fetchall()
    for uid, day, revision in jobs:
        try:
            process_day(DB, uid, day)
        except Exception as exc:
            with sqlite3.connect(DB, timeout=30) as con:
                con.execute(
                    "UPDATE activity_days SET attempts=attempts+1,error=?,next_run_at=? WHERE user_id=? AND day=? AND revision=?",
                    (
                        type(exc).__name__,
                        str(
                            datetime.now(timezone.utc).replace(tzinfo=None)
                            + timedelta(seconds=60)
                        ),
                        uid,
                        day,
                        revision,
                    ),
                )
            print(
                f"activity job failed user={uid} day={day}: {type(exc).__name__}",
                flush=True,
            )


def main():
    with (DB.parent / ".activity-worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            try:
                run_once()
            except sqlite3.OperationalError:
                print("activity database unavailable; will retry", flush=True)
            if "--once" in sys.argv:
                return
            time.sleep(2)


if __name__ == "__main__":
    main()
