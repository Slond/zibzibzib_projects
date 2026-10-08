#!/usr/bin/env python3
"""Process refresh requests on the host; one worker owns the queue at a time."""
from __future__ import annotations
import fcntl
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DAYS = Path(os.environ.get("ACTIVITY_BREAKDOWN_DIR", ROOT / "data" / "activity-days"))
AGGREGATE = ROOT / "scripts" / "activity_aggregate.py"


def pending() -> list[Path]:
    return sorted(p for p in DAYS.glob("*/*.refresh") if p.stat().st_mtime <= time.time())


def run_one(flag: Path) -> None:
    user = flag.parent.name
    day = flag.name.removesuffix(".refresh")
    try:
        if int(user) <= 0 or date.fromisoformat(day).isoformat() != day:
            raise ValueError()
    except ValueError:
        flag.unlink(missing_ok=True)
        return
    if (DAYS / f".deleted-user-{user}").exists():
        flag.unlink(missing_ok=True)
        return
    running = flag.with_suffix(".running")
    flag.replace(running)
    try:
        result = subprocess.run([sys.executable, str(AGGREGATE), "--user", user, "--date", day], cwd=ROOT, timeout=960, check=False)
        if result.returncode:
            # Keep the request for retry, without a tight failure loop.
            if not flag.exists():
                flag.touch()
                retry = time.time() + 300
                os.utime(flag, (retry, retry))
            print(f"aggregation failed for user {user}, date {day}; retry in five minutes", file=sys.stderr, flush=True)
    except subprocess.TimeoutExpired:
        flag.touch()
        retry = time.time() + 300
        os.utime(flag, (retry, retry))
    finally:
        running.unlink(missing_ok=True)


def main() -> None:
    DAYS.mkdir(parents=True, exist_ok=True)
    with (DAYS / ".worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # We own the process lock: flags from an interrupted worker are stale.
        for running in DAYS.glob("*/*.running"):
            refresh = running.with_suffix(".refresh")
            if not refresh.exists():
                running.replace(refresh)
            else:
                running.unlink(missing_ok=True)
        once = "--once" in sys.argv
        while True:
            for flag in pending():
                run_one(flag)
            if once:
                return
            time.sleep(2)

if __name__ == "__main__":
    main()
