"""The automatic bank pulls (Bank of America, Chase), as the dashboard drives them: starting one (it runs
tools/<bank>_fetch.py in the background - Chrome opens on this Mac), status and results, and the monthly schedule
(launchd, via tools/bofa_schedule.sh, running tools/monthly_pull.py: every set-up bank in turn).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import paths

LABEL = "com.financialanalyst.bofa-monthly"     # (kept: it's the job macOS already knows; it now runs every bank)
BANKS = {"bofa": "Bank of America", "chase": "Chase", "amex": "American Express"}   # tools/<key>_fetch.py
SCHEDULE_DAY, SCHEDULE_HOUR = 28, 9


def _dir() -> Path:
    d = paths.data_dir() / "bank_fetch"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- a pull running in the background ---------------------------------------------------------------------------

def _pid_file() -> Path:
    return _dir() / "bank_pull.pid"


def run_log() -> Path:
    return _dir() / "bofa_pull_output.log"


def running() -> bool:
    try:
        pid = int(_pid_file().read_text())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def start(dry_run: bool = False, bank: str = "bofa") -> None:
    """Start a pull in the background: one bank (tools/<bank>_fetch.py), or bank="all" for every set-up bank."""
    if os.environ.get("FINANCE_NO_BANK_PULL"):
        raise RuntimeError("Bank pulls are switched off here (tests)")
    if running():
        return
    script = paths.ROOT / "tools" / ("monthly_pull.py" if bank == "all" else f"{bank}_fetch.py")
    with run_log().open("w") as out:
        p = subprocess.Popen([sys.executable, str(script)] + (["--dry-run"] if dry_run else []),
                             cwd=paths.ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    _pid_file().write_text(str(p.pid))


def start_recording(bank: str, label: str = "statements") -> None:
    """Open the recorder for a bank (tools/bank_recorder.py): you sign in and click to one statement's download;
    it keeps the labels of what you clicked (digits masked), so the automatic pull can be written from facts."""
    if os.environ.get("FINANCE_NO_BANK_PULL"):
        raise RuntimeError("Bank pulls are switched off here (tests)")
    if running():
        return
    script = paths.ROOT / "tools" / "bank_recorder.py"
    with run_log().open("w") as out:
        p = subprocess.Popen([sys.executable, str(script), bank, label], cwd=paths.ROOT, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True)
    _pid_file().write_text(str(p.pid))


def recording(bank: str, label: str = "statements") -> dict | None:
    try:
        import json
        return json.loads((_dir() / f"{bank}_{label}_recording.json").read_text())
    except (FileNotFoundError, ValueError):
        return None


def output() -> str:
    """What the running (or last) pull printed - status lines, no amounts."""
    try:
        return run_log().read_text()[-3000:]
    except FileNotFoundError:
        return ""


def last_results(real_only: bool = False, bank: str = "bofa") -> tuple[str, list[str]] | None:
    """The newest entry of <bank>_runs.log: (when, one line per statement). real_only skips test runs."""
    try:
        text = (_dir() / f"{bank}_runs.log").read_text()
    except FileNotFoundError:
        return None
    runs = [r for r in text.split("--- ") if r.strip() and not (real_only and "(dry run)" in r.splitlines()[0])]
    if not runs:
        return None
    head, *lines = runs[-1].strip().splitlines()
    return head, lines


def set_up(bank: str = "bofa") -> bool:
    """The bank is connected: it has been updated (or tested) at least once (BofA: or the schedule is on)."""
    return ((_dir() / f"{bank}_runs.log").exists() or (_dir() / f"{bank}_fetched.json").exists()
            or (bank == "bofa" and scheduled()))


# --- the monthly schedule ---------------------------------------------------------------------------------------

def plist() -> Path:
    agents = os.environ.get("FINANCE_LAUNCH_AGENTS") or Path.home() / "Library" / "LaunchAgents"   # tests: their own
    return Path(agents) / f"{LABEL}.plist"


def scheduled() -> bool:
    return plist().exists()


def schedule_is_current() -> bool:
    """The schedule runs the all-banks monthly pull (older ones ran only Bank of America's)."""
    try:
        return "monthly_pull.py" in plist().read_text()
    except FileNotFoundError:
        return True


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
