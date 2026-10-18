"""Monthly home values: open each home's Redfin page and save its Redfin Estimate.

    ~/.venvs/financial-analyst/bin/python tools/redfin_fetch.py            # the real thing
    ~/.venvs/financial-analyst/bin/python tools/redfin_fetch.py --dry-run  # read + check, save into a copy

The pages are public (no sign-in). Which page belongs to which home is kept in your data (setting "redfin_homes":
account id -> Redfin link), never in the code. A value is saved for today, unless it's far off the home's last one
(under half or over double) - then it's reported instead, as the page probably changed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from finance import bank_browser, db, paths  # noqa: E402

SOURCE = "Redfin estimate"
_AMOUNT = r"\$\s?([\d,]{5,})"
_PATTERNS = [re.compile(r"Redfin Estimate[^$\n]{0,40}\n?[^$\n]{0,20}" + _AMOUNT, re.I),     # "Redfin Estimate  $1,596,911"
             re.compile(_AMOUNT + r"\s*\n?\s*(?:Redfin Estimate|Est\.)", re.I)]            # "$1,596,911  Redfin Estimate"
_EMBEDDED = re.compile(r'"predictedValue"\s*:\s*([\d.]+)')                               # in the page's own data


def notify(text: str) -> None:
    print(text, flush=True)
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Home values\""],
                   check=False, capture_output=True)


def estimate(text: str, html: str = "") -> float | None:
    """The Redfin Estimate shown on a home's page (its text, else the value in the page's data)."""
    for p in _PATTERNS:
        if m := p.search(text or ""):
            return float(m.group(1).replace(",", ""))
    if m := _EMBEDDED.search(html or ""):
        return float(m.group(1))
    return None


def homes(conn) -> dict[int, str]:
    return {int(k): v for k, v in (db.get_setting(conn, "redfin_homes", {}) or {}).items()}


def save(conn, aid: int, value: float | None, today: date) -> str:
    name = db.accounts(conn).set_index("id").loc[aid, "name"]
    if value is None:
        return f"{name}: couldn't find the Redfin Estimate on its page - nothing saved"
    last = conn.execute("SELECT balance FROM balances WHERE account_id = ? ORDER BY date DESC LIMIT 1", (aid,)).fetchone()
    if last and last[0] and not (0.5 <= value / last[0] <= 2):
        return f"{name}: couldn't trust the value read ({value / last[0]:.1f}x the last one) - nothing saved"
    db.upsert_balance(conn, aid, today, value, SOURCE)
    change = f", {value / last[0] - 1:+.1%} since the last value" if last and last[0] else ""
    return f"{name}: value saved ({today:%b %-d}){change}"


def main(dry_run: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    base = paths.data_dir() / "bank_fetch"
    db_path = paths.data_dir() / "finance.db"
    if dry_run:
        tmp = Path(tempfile.mkdtemp()) / "finance.db"
        shutil.copy(db_path, tmp)
        db_path = tmp
    conn = db.connect(db_path)
    pages = homes(conn)
    if not pages:
        notify("Home values: no Redfin links saved yet - nothing to do.")
        return
    profile = base / "chrome-profile" / "redfin"
    profile.mkdir(parents=True, exist_ok=True)
    profile.chmod(0o700)
    found = {}
    with sync_playwright() as pw:
        ctx = bank_browser.launch(pw, profile, plain=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for aid, url in pages.items():
                try:
                    page.goto(url, timeout=60_000)
                    page.get_by_text(re.compile("Redfin Estimate", re.I)).first.wait_for(timeout=30_000)
                except Exception as e:
                    print(f"Home {aid}: page didn't show an estimate ({e.__class__.__name__})", flush=True)
                found[aid] = estimate(page.locator("body").inner_text(), page.content())
        finally:
            ctx.close()
    lines = [save(conn, aid, value, date.today()) for aid, value in found.items()]
    with (base / "redfin_runs.log").open("a") as log:          # what happened, no amounts
        log.write(f"--- {datetime.now():%Y-%m-%d %H:%M}{' (dry run)' if dry_run else ''}\n" + "\n".join(lines) + "\n")
    print("\n".join(lines))
    notify(("Dry run: " if dry_run else "") + "; ".join(lines)[:220])


if __name__ == "__main__":
    try:
        main(dry_run="--dry-run" in sys.argv)
    finally:                                   # the dashboard shows "running" until this pull's pid file is gone
        pid = paths.data_dir() / "bank_fetch" / "bank_pull.pid"
        if pid.exists() and pid.read_text().strip() == str(os.getpid()):
            pid.unlink()
