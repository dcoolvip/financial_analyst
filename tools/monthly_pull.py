"""The monthly browser pulls, one bank after another (what the schedule and "Update all" run).

    ~/.venvs/financial-analyst/bin/python tools/monthly_pull.py [--dry-run]     # every set-up bank still to do
    ~/.venvs/financial-analyst/bin/python tools/monthly_pull.py --bank chase    # one bank (the Update button)
    ~/.venvs/financial-analyst/bin/python tools/monthly_pull.py --bank chase --force   # ... even what it already has

Runs tools/<bank>_fetch.py for every bank that's set up, then the monthly steps without a browser (the Pokemon
collection). What finishes is remembered for this month's round (from the 28th): run again after a failure and only
the banks that failed or didn't run go again - none is signed in to twice without need. Test runs (--dry-run) run
everything and remember nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from finance import bank_pull, paths  # noqa: E402

FAILED = {1: "stopped with an error", 2: "couldn't sign in"}


def run_bank(bank: str, dry_run: bool, force: bool = False) -> bool:
    print(f"=== {bank_pull.BANKS[bank]}" + (" (forced)" if force else ""), flush=True)
    extra = (["--dry-run"] if dry_run else []) + (["--force"] if force else [])
    rc = subprocess.run([sys.executable, *bank_pull.command(bank)] + extra, cwd=ROOT).returncode
    if not dry_run:
        bank_pull.mark_round(bank, rc == 0, FAILED.get(rc, f"stopped (code {rc})"))
    return rc == 0


def run_extra(step: str, dry_run: bool, force: bool = False) -> bool:
    print(f"=== {bank_pull.EXTRA_STEPS[step]}", flush=True)
    if dry_run:
        return True
    try:
        if step == "pokemon":
            from finance import db, pokemon
            r = pokemon.sync(db.connect(paths.data_dir() / "finance.db"))
            print(f"Pokemon collection: value saved ({r['as_of']:%b %-d}), {r['cards']} cards", flush=True)
        bank_pull.mark_round(step, True)
        return True
    except Exception as e:
        print(f"{bank_pull.EXTRA_STEPS[step]}: couldn't update ({e.__class__.__name__}: {e})", flush=True)
        bank_pull.mark_round(step, False, str(e)[:150])
        return False


def main(argv: list[str]) -> None:
    dry_run = "--dry-run" in argv
    if "--bank" in argv:
        bank = argv[argv.index("--bank") + 1]
        (run_extra if bank in bank_pull.EXTRA_STEPS else run_bank)(bank, dry_run, "--force" in argv)
        return
    todo = ([b for b in bank_pull.BANKS if bank_pull.set_up(b)] + [s for s in bank_pull.EXTRA_STEPS
                                                                   if bank_pull.extra_available(s)]
            if dry_run else bank_pull.left_this_round())
    skipped = [bank_pull.BANKS.get(b, b) for b in bank_pull.round_state()["done"]] if not dry_run else []
    if skipped:
        print("Already done this month (not run again): " + ", ".join(skipped), flush=True)
    for step in todo:
        (run_extra if step in bank_pull.EXTRA_STEPS else run_bank)(step, dry_run)
    if not dry_run:
        state = bank_pull.round_state()
        print(f"This month: {len(state['done'])} done" + (f", still to do: " + ", ".join(
            bank_pull.BANKS.get(b) or bank_pull.EXTRA_STEPS[b] for b in state["failed"]) if state["failed"] else ""),
              flush=True)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    finally:
        pid = paths.data_dir() / "bank_fetch" / "bank_pull.pid"
        if pid.exists() and pid.read_text().strip() == str(os.getpid()):
            pid.unlink()
