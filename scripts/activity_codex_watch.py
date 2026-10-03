#!/usr/bin/env python3
"""Run Codex for a day when the activity page asks.

The page writes data/activity-days/<user>/<date>.refresh. Codex itself
stays on the host: it is not installed in the app container.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DAYS = ROOT / "data" / "activity-days"
AGGREGATE = ROOT / "scripts" / "activity_aggregate.py"


def pending() -> list[Path]:
    if not DAYS.is_dir():
        return []
    return sorted(DAYS.glob("*/*.refresh"))


def run_one(flag: Path) -> None:
    user = flag.parent.name
    day = flag.name.removesuffix(".refresh")
    running = flag.with_suffix(".running")
    if running.exists():
        return
    running.write_text(day, encoding="utf-8")
    flag.unlink(missing_ok=True)
    try:
        subprocess.run(
            [sys.executable, str(AGGREGATE), "--user", user, "--date", day],
            cwd=ROOT,
            check=False,
        )
    finally:
        running.unlink(missing_ok=True)


def main() -> None:
    once = "--once" in sys.argv
    while True:
        for flag in pending():
            run_one(flag)
        if once:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
