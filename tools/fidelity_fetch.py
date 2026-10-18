"""Monthly Fidelity NetBenefits pull: sign in, show last month's 401(k) statement, read it off the page, import it.

    ~/.venvs/financial-analyst/bin/python tools/fidelity_fetch.py            # the real thing
    ~/.venvs/financial-analyst/bin/python tools/fidelity_fetch.py --dry-run  # read + check, import into a copy

The route (recorded): View your statements -> Time period "Monthly" -> Month -> Year -> Get statement -> the statement
shows as a page (#/sod/soddetail); "Download or Print" only prints that page, so there's no file. The pull reads the
statement's text straight off the page (kept privately in the data folder) and imports it with the Fidelity 401(k)
reader. It fills the dashboard account chosen for this login (Connected accounts -> Fidelity -> ⚙️ -> Fills).
Plain Chrome. Sign-in is yours: Fidelity refused sign-ins while the pull read the password box, so the pull leaves the
sign-in page alone and only watches the address; "Remember this device" (ticked in the recording) keeps the code away.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from finance import bank_browser, checkpoints, db, paths  # noqa: E402
from finance.importers import apply_statement, statements  # noqa: E402

LOGIN = "https://nb.fidelity.com/"     # where the recording started
STATEMENT = "https://workplaceservices.fidelity.com/mybenefits/savings#/navigation/dc/OnlineStatement"
WAIT_FOR_SIGN_IN_S = 10 * 60
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def notify(text: str) -> None:
    print(text, flush=True)
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Fidelity pull\""],
                   check=False, capture_output=True)


def last_month(today: date) -> tuple[int, int]:
    return (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)


def fetched_path(login: str) -> Path:
    return paths.data_dir() / "bank_fetch" / f"{login}_fetched.json"


def already_fetched(login: str) -> set[str]:
    try:
        return set(json.loads(fetched_path(login).read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def _signed_in(page) -> bool:
    return "workplaceservices.fidelity.com" in page.url        # inside NetBenefits (only the address is looked at)


def sign_in(page) -> bool:
    """Open NetBenefits and leave the sign-in page completely alone - you sign in (and the text code if asked).
    Fidelity refused sign-ins in this window while the pull was reading the password box (even with the password
    typed correctly and Log In clicked by you); the recorder, which didn't touch it, signed in fine. So until you're
    in, the pull only watches the page address, which runs nothing on the page."""
    page.goto(LOGIN)
    notify("Fidelity: sign in in the Chrome window (and the text code if asked) - the pull does the rest.")
    deadline = time.time() + WAIT_FOR_SIGN_IN_S
    while time.time() < deadline:
        if _signed_in(page):
            return True
        page.wait_for_timeout(2000)
    return False


def show_statement(page, year: int, month: int) -> str:
    """Monthly -> month -> year -> Get statement; returns the statement's text as shown on the page."""
    page.goto(STATEMENT)
    page.wait_for_timeout(5000)
    page.get_by_label("Time period").select_option(label="Monthly")
    page.wait_for_timeout(1000)
    page.get_by_label("Month").select_option(label=MONTHS[month - 1])
    page.get_by_label("Year").select_option(label=str(year))
    page.get_by_role("button", name=re.compile(r"get statement", re.I)).click()
    page.wait_for_url(re.compile(r"soddetail"), timeout=30_000)
    page.wait_for_timeout(4000)
    return page.locator("body").inner_text()


def account_for(conn, login: str) -> int | None:
    return (db.get_setting(conn, "browser_pull_accounts", {}) or {}).get(login)


def holder(conn, text: str) -> str | None:
    """Whose plan this statement is: the one household member (names from the dashboard's household settings, never
    the code) whose name appears on it. None if no one or several do (then there's nothing to check against)."""
    people = [p.get("name", "") for p in (db.get_setting(conn, "household_people", []) or [])]
    found = [n for n in people if n and re.search(rf"\b{re.escape(n.split()[0])}\b", text, re.I)]
    return found[0] if len(found) == 1 else None


def same_holder(conn, login: str, text: str) -> str | None:
    """None if fine; else why not. The first statement fixes whose plan this login is; a different name later means
    someone else signed in to this window - never import that into this login's account."""
    who, known = holder(conn, text), db.get_setting(conn, "browser_pull_holders", {}) or {}
    if not who:
        return None
    if login not in known:
        db.set_setting(conn, "browser_pull_holders", {**known, login: who})
        return None
    return None if known[login] == who else "this statement is for a different person than this login's earlier ones"


def import_text(conn, login: str, text: str, label: str) -> str:
    aid = account_for(conn, login)
    if aid is None:
        return f"{label}: pick which account this login fills (Connected accounts -> Fidelity -> ⚙️ -> Fills)"
    if (why := same_holder(conn, login, text)):
        return f"{label}: not imported - {why}"
    s = statements.parse_text(text)
    name = db.accounts(conn).set_index("id").loc[aid, "name"]
    if s.balance is None or s.as_of is None:
        return f"{label}: couldn't read the balance from the statement page - the reader needs adjusting"
    apply_statement(conn, s, aid, label)
    split = ", pre-tax / Roth split saved" if s.tax_sources else ""
    return f"{name}: value saved ({s.as_of:%b %-d}){split}"


def main(login: str = "fidelity", dry_run: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    base = paths.data_dir() / "bank_fetch"
    profile, out = base / "chrome-profile" / login, base / "downloads" / login
    for d in (profile, out):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    year, month = last_month(date.today())
    key = f"{year}-{month:02d}"
    if key in already_fetched(login) and not dry_run:
        notify(f"Fidelity: {MONTHS[month - 1]} {year} is already in - nothing to do.")
        return
    with sync_playwright() as pw:
        ctx = bank_browser.launch(pw, profile, plain=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if not sign_in(page):
                notify("Couldn't sign in to Fidelity within 10 minutes - nothing read.")
                return
            text = show_statement(page, year, month)
        except Exception as e:
            notify(f"Fidelity pull stopped: {e.__class__.__name__}: {str(e)[:150]} - nothing was imported.")
            raise
        finally:
            ctx.close()
    label = f"NetBenefits_{key}.txt"
    path = out / label
    path.write_text(text)                                     # private copy, for checking the reader
    path.chmod(0o600)
    db_path = paths.data_dir() / "finance.db"
    if dry_run:
        tmp = Path(tempfile.mkdtemp()) / "finance.db"
        shutil.copy(db_path, tmp)
        db_path = tmp
    conn = db.connect(db_path)
    if not dry_run:
        checkpoints.create(conn, f"Before the Fidelity pull ({MONTHS[month - 1]} {year})")
    line = import_text(conn, login, text, label)
    if not dry_run and "value saved" in line:
        fetched_path(login).write_text(json.dumps(sorted(already_fetched(login) | {key}), indent=1))
    with (base / f"{login}_runs.log").open("a") as log:      # what happened, no amounts
        log.write(f"--- {datetime.now():%Y-%m-%d %H:%M}{' (dry run)' if dry_run else ''}\n{line}\n")
    print(line)
    notify(("Dry run: " if dry_run else "") + line[:220])


if __name__ == "__main__":
    try:
        login = sys.argv[sys.argv.index("--login") + 1] if "--login" in sys.argv else "fidelity"
        main(login=login, dry_run="--dry-run" in sys.argv)
    finally:                                   # the dashboard shows "running" until this pull's pid file is gone
        pid = paths.data_dir() / "bank_fetch" / "bank_pull.pid"
        if pid.exists() and pid.read_text().strip() == str(os.getpid()):
            pid.unlink()
