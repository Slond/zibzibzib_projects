#!/usr/bin/env python3
"""Rebuild activity days using deterministic intervals; no external model calls."""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.activity_pipeline import process_day

DB_PATH = Path(os.environ.get("ACTIVITY_DB", ROOT / "data/main-project.db"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", type=int)
    parser.add_argument("--date")
    args = parser.parse_args()
    with sqlite3.connect(DB_PATH) as con:
        jobs = con.execute(
            "SELECT user_id,day FROM activity_days WHERE (? IS NULL OR user_id=?) AND (? IS NULL OR day=?) ORDER BY day DESC",
            (args.user, args.user, args.date, args.date),
        ).fetchall()
    for uid, day in jobs:
        process_day(DB_PATH, uid, day)
    print(f"Processed {len(jobs)} activity days")


if __name__ == "__main__":
    main()
