"""The automatic Bank of America pull, as the dashboard drives it: starting a pull (it runs
tools/bofa_fetch.py in the background - Chrome opens on this Mac), its status and results, and the monthly
schedule (launchd, via tools/bofa_schedule.sh).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import paths

LABEL = "com.financialanalyst.bofa-monthly"
SCHEDULE_DAY, SCHEDULE_HOUR = 28, 9


def _dir() -> Path:
    d = paths.data_dir() / "bank_fetch"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- a pull running in the background ---------------------------------------------------------------------------

def _pid_file() -> Path:
    return _dir() / "bofa_pull.pid"


def run_log() -> Path:
    return _dir() / "bofa_pull_output.log"


def running() -> bool:
    try:
        pid = int(_pid_file().read_text())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def start(dry_run: bool = False) -> None:
    if running():
        return
    script = paths.ROOT / "tools" / "bofa_fetch.py"
    with run_log().open("w") as out:
        p = subprocess.Popen([sys.executable, str(script)] + (["--dry-run"] if dry_run else []),
                             cwd=paths.ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    _pid_file().write_text(str(p.pid))


def output() -> str:
    """What the running (or last) pull printed - status lines, no amounts."""
    try:
        return run_log().read_text()[-3000:]
    except FileNotFoundError:
        return ""


def last_results() -> tuple[str, list[str]] | None:
    """The newest entry of bofa_runs.log: (when, one line per statement)."""
    try:
        text = (_dir() / "bofa_runs.log").read_text()
    except FileNotFoundError:
        return None
    runs = [r for r in text.split("--- ") if r.strip()]
    if not runs:
        return None
    head, *lines = runs[-1].strip().splitlines()
    return head, lines


# --- the monthly schedule ---------------------------------------------------------------------------------------

def plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def scheduled() -> bool:
    return plist().exists()


def set_schedule(on: bool) -> str:
    r = subprocess.run(["zsh", str(paths.ROOT / "tools" / "bofa_schedule.sh"), "install" if on else "remove"],
                       cwd=paths.ROOT, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "Couldn't change the schedule")
    return r.stdout.strip()


def next_run(now: float | None = None) -> time.struct_time:
    t = time.localtime(now or time.time())
    year, month = t.tm_year, t.tm_mon
    if (t.tm_mday, t.tm_hour) >= (SCHEDULE_DAY, SCHEDULE_HOUR):
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return time.strptime(f"{year}-{month:02d}-{SCHEDULE_DAY} {SCHEDULE_HOUR}", "%Y-%m-%d %H")
