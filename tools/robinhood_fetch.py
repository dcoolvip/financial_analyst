"""Monthly Robinhood pull: sign in, download the latest monthly statement, import every account in it.

    ~/.venvs/financial-analyst/bin/python tools/robinhood_fetch.py            # the real thing
    ~/.venvs/financial-analyst/bin/python tools/robinhood_fetch.py --dry-run  # download + check, import into a copy

The route (recorded): Account -> Reports and statements -> the "Individual" statements tab -> Download PDF (the newest
is listed first). One statement covers every account under the login; the Robinhood reader splits it and each
account is matched by its last 4 digits (printed masked: "Account #:*****4688"). Pop-ups ("Okay", "Continue") are
dismissed if they show. Plain Chrome. Sign-in: Enter only if Chrome filled the password in, else a notification.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from finance import bank_browser, checkpoints, db, paths  # noqa: E402
from finance.importers import apply_statement, statements  # noqa: E402

LOGIN = "https://robinhood.com/login"
STATEMENTS = "https://robinhood.com/account/reports-statements/individual"
WAIT_FOR_SIGN_IN_S = 10 * 60
INSTITUTION = "Robinhood"


def notify(text: str) -> None:
    print(text, flush=True)
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Robinhood pull\""],
                   check=False, capture_output=True)


def fetched_path() -> Path:
    return paths.data_dir() / "bank_fetch" / "robinhood_fetched.json"


def already_fetched() -> set[str]:
    try:
        return set(json.loads(fetched_path().read_text()))
    except (FileNotFoundError, ValueError):
        return set()


DOWNLOADS: list = []


def watch_downloads(ctx) -> None:
    def got(download):
        DOWNLOADS.append(download)

    def hook(p):
        p.on("download", got)
    for p in ctx.pages:
        hook(p)
    ctx.on("page", hook)


def _signed_in(page) -> bool:
    return "robinhood.com" in page.url and "/login" not in page.url and \
        not page.locator("input[type=password]").filter(visible=True).count()


def sign_in(page) -> bool:
    page.goto(LOGIN)
    pressed, told = set(), False
    deadline, ask_after = time.time() + WAIT_FOR_SIGN_IN_S, time.time() + 15
    while time.time() < deadline:
        if _signed_in(page):
            return True
        if not bank_browser.press_if_chrome_filled(page, pressed) and not told and time.time() > ask_after:
            notify("Robinhood: sign in in the Chrome window (and approve on your phone if asked) - the pull does the rest.")
            told = True
        page.wait_for_timeout(1500)
    return False


def dismiss_popups(page) -> None:
    for label in ("Okay", "Continue", "Got it", "Not now"):
        try:
            page.get_by_role("button", name=label, exact=True).filter(visible=True).first.click(timeout=1500)
        except Exception:
            pass


def download_latest(page, out: Path) -> Path:
    page.goto(STATEMENTS)
    page.wait_for_timeout(4000)
    dismiss_popups(page)
    link = page.get_by_text("Download PDF", exact=True).filter(visible=True).first      # the newest is first
    link.wait_for(state="visible", timeout=30_000)
    before = len(DOWNLOADS)
    link.click()
    deadline = time.time() + 30
    while time.time() < deadline and len(DOWNLOADS) == before:
        page.wait_for_timeout(500)
    if len(DOWNLOADS) == before:
        raise TimeoutError("no download came after Download PDF")
    dl = DOWNLOADS[before]
    path = out / f"{datetime.now():%Y%m%d}-{dl.suggested_filename}"
    dl.save_as(path)
    return path


def import_statement(conn, pdf: Path) -> tuple[list[str], str | None]:
    """Each account in the statement into the dashboard account with the same last 4 digits. Returns the report
    lines and the statement date (None if it couldn't be read)."""
    parts = statements.parse_pdf_all(pdf.read_bytes())
    accts = db.accounts(conn)
    mine = accts[accts["institution"] == INSTITUTION]
    lines, when = [], None
    for s in parts:
        if s.institution != INSTITUTION or not s.as_of:
            lines.append(f"{pdf.name}: a part that isn't a readable Robinhood statement - skipped")
            continue
        when = s.as_of.isoformat()
        hit = mine[mine["last4"] == s.last4] if s.last4 else mine.iloc[0:0]
        if len(hit) != 1:
            lines.append(f"Robinhood account ending {s.last4 or '?'}: no dashboard account with those digits - "
                         "import it in Add data")
            continue
        apply_statement(conn, s, int(hit["id"].iloc[0]), pdf.name)
        lines.append(f"{hit['name'].iloc[0]}: value saved ({s.as_of:%b %-d}), {len(s.holdings)} positions")
    return lines, when


def main(dry_run: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    base = paths.data_dir() / "bank_fetch"
    profile, out = base / "chrome-profile" / "robinhood", base / "downloads" / "robinhood"
    for d in (profile, out):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    with sync_playwright() as pw:
        ctx = bank_browser.launch(pw, profile, plain=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        watch_downloads(ctx)
        try:
            if not sign_in(page):
                notify("Couldn't sign in to Robinhood within 10 minutes - nothing downloaded.")
                return
            pdf = download_latest(page, out)
        except Exception as e:
            notify(f"Robinhood pull stopped: {e.__class__.__name__}: {str(e)[:150]} - nothing was imported.")
            raise
        finally:
            ctx.close()
    db_path = paths.data_dir() / "finance.db"
    if dry_run:
        tmp = Path(tempfile.mkdtemp()) / "finance.db"
        shutil.copy(db_path, tmp)
        db_path = tmp
    conn = db.connect(db_path)
    seen = already_fetched()
    probe = statements.parse_pdf_all(pdf.read_bytes())
    when = next((s.as_of.isoformat() for s in probe if s.as_of), None)
    if when and when in seen and not dry_run:
        notify(f"Robinhood: the {when} statement is already in - nothing to do.")
        return
    if not dry_run:
        checkpoints.create(conn, "Before the Robinhood pull")
    lines, when = import_statement(conn, pdf)
    if not dry_run and when:
        fetched_path().write_text(json.dumps(sorted(seen | {when}), indent=1))
    with (base / "robinhood_runs.log").open("a") as log:       # what happened, no amounts
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
