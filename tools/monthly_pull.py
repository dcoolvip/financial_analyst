"""The monthly browser pulls, one bank after another (what the schedule and "Update all" run).

    ~/.venvs/financial-analyst/bin/python tools/monthly_pull.py [--dry-run]

Runs tools/<bank>_fetch.py for every bank that's set up (it has been updated or tested at least once).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from finance import bank_pull, paths  # noqa: E402

if __name__ == "__main__":
    try:
        for bank in bank_pull.BANKS:
            if bank_pull.set_up(bank):
                print(f"=== {bank_pull.BANKS[bank]}", flush=True)
                subprocess.run([sys.executable, *bank_pull.command(bank)] + sys.argv[1:], cwd=ROOT)
    finally:
        pid = paths.data_dir() / "bank_fetch" / "bank_pull.pid"
        if pid.exists() and pid.read_text().strip() == str(os.getpid()):
            pid.unlink()
