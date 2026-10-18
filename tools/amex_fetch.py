"""Monthly American Express pull: sign in, download recent activity (Excel), import it without doubles.

    ~/.venvs/financial-analyst/bin/python tools/amex_fetch.py            # the real thing
    ~/.venvs/financial-analyst/bin/python tools/amex_fetch.py --dry-run  # download + check, import into a copy

Amex stalls the sign-in of a browser that announces it's script-driven, so Chrome opens without the automation
flag. Sign-in: Enter if Chrome has filled the password in, else a notification asks you to sign in. Then
Statements & Activity -> Download -> (a pop-up) Excel -> its Download (the route recorded in tools/bank_recorder.py),
imported with the Amex reader: Amex's own reference number per transaction means nothing is doubled. If the file starts
after the newest Amex transaction already here, it says so - a gap can't go unnoticed.
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
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from finance import checkpoints, db, paths  # noqa: E402
from finance.importers import amex, apply  # noqa: E402

DASHBOARD = "https://global.americanexpress.com/dashboard"
ACTIVITY = "https://global.americanexpress.com/activity"
SIGNED_IN = re.compile(r"\blog ?out\b", re.I)
WAIT_FOR_SIGN_IN_S = 10 * 60
INSTITUTION = "American Express"
PLAIN = {"ignore_default_args": ["--enable-automation", "--disable-extensions",
                                 "--disable-component-extensions-with-background-pages"],
         "args": ["--disable-blink-features=AutomationControlled", "--window-size=1680,1050", "--window-position=0,0"]}


def notify(text: str) -> None:
    print(text, flush=True)
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Amex pull\""],
                   check=False, capture_output=True)


def mask_digits(s: str) -> str:
    return re.sub(r"\d", "#", s)


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
    return "/dashboard" in page.url or page.get_by_text(SIGNED_IN).first.is_visible()


def sign_in(page) -> bool:
    page.goto(DASHBOARD)
    page.wait_for_timeout(3000)
    if not _signed_in(page):
        pwd = page.locator("input[type=password]").filter(visible=True).first
        try:
            if pwd.count() and pwd.input_value():          # Chrome filled it in: sign in
                pwd.press("Enter")
            else:                                           # never submit an empty password
                notify("Amex: sign in in the Chrome window (pick your saved login) - the pull does the rest.")
        except Exception:
            pass
    deadline = time.time() + WAIT_FOR_SIGN_IN_S
    while time.time() < deadline:
        if _signed_in(page):
            return True
        page.wait_for_timeout(2000)
    return False


def _labels(scope) -> list[str]:
    """What's in the download pop-up (labels only, digits masked) - for setting the date range from facts."""
    out = []
    for el in scope.locator("label, [role=radio], [role=option], button, select option").filter(visible=True).all()[:40]:
        try:
            t = " ".join((el.inner_text(timeout=1000) or "").split())
            if t:
                out.append(mask_digits(t)[:50])
        except Exception:
            pass
    return out


def download_activity(page, out: Path) -> Path | None:
    """Statements & Activity -> Download opens a pop-up (modal): everything after that happens inside it -
    choose Excel, tick the details, press the pop-up's own Download."""
    page.goto(ACTIVITY)
    page.wait_for_timeout(5000)
    try:                                                    # a one-time tour can cover the page
        page.get_by_text("Explore On My Own", exact=True).first.click(timeout=3000)
    except Exception:
        pass
    page.get_by_text("Download", exact=True).filter(visible=True).first.click()
    excel = page.locator("#myca-activity-download-body-selection-options-excel")
    excel.wait_for(state="attached", timeout=15_000)
    modal = page.locator("[role=dialog], [aria-modal=true]").filter(visible=True)
    modal = modal.last if modal.count() else \
        excel.locator("xpath=ancestor::*[.//*[normalize-space(text())='Download']][1]")   # the box with its Download
    modal.locator("#myca-activity-download-body-selection-options-excel").check(force=True)
    for box in modal.locator("input[type=checkbox]").all():          # "include all additional details"
        try:
            label = (box.evaluate("e => (e.closest('label') || e.parentElement).innerText") or "").lower()
            if "additional" in label or "detail" in label:
                box.check(force=True)
        except Exception:
            pass
    print("Download pop-up shows: " + str(_labels(modal)), flush=True)
    before = len(DOWNLOADS)
    button = modal.get_by_role("button", name=re.compile(r"^\s*download\s*$", re.I))
    (button if button.count() else modal.get_by_text("Download", exact=True)).last.click()
    deadline = time.time() + 30
    while time.time() < deadline and len(DOWNLOADS) == before:
        page.wait_for_timeout(500)
    if len(DOWNLOADS) == before:
        raise TimeoutError("no download came after the pop-up's Download")
    dl = DOWNLOADS[before]
    path = out / f"{datetime.now():%Y%m%d}-{dl.suggested_filename}"
    dl.save_as(path)
    return path


def import_csv(conn, path: Path) -> str:
    parsed = amex.parse(path.read_bytes(), filename=path.name)      # it's an Amex file: no guessing
    accts = db.accounts(conn)
    cards = accts[(accts["institution"] == INSTITUTION) & (accts["type"] == "credit_card")]
    if len(cards) != 1:
        return f"{path.name}: {len(cards)} Amex cards in the dashboard - import it in Add data to pick one"
    aid = int(cards["id"].iloc[0])
    newest = conn.execute("SELECT MAX(date) FROM transactions WHERE account_id = ?", (aid,)).fetchone()[0]
    r = apply(conn, parsed, aid, path.name)
    span = (parsed.transactions["date"].min(), parsed.transactions["date"].max()) if len(parsed.transactions) else None
    line = (f"{cards['name'].iloc[0]}" + (f" ({span[0]:%b %-d} – {span[1]:%b %-d})" if span else "")
            + f": {len(parsed.transactions)} transactions, {r['transactions_skipped']} already there, "
              f"{r['transactions_added']} added")
    if newest and span and span[0] > datetime.strptime(newest, "%Y-%m-%d").date() + timedelta(days=1):
        line += (f" - ⚠️ gap: nothing here between {newest} and {span[0]:%Y-%m-%d}; download that stretch in Amex "
                 "and import it in Add data")
    return line


def main(dry_run: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    base = paths.data_dir() / "bank_fetch"
    profile, out = base / "chrome-profile" / "amex", base / "downloads" / "amex"
    for d in (profile, out):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False,
                                                    accept_downloads=True, viewport=None, **PLAIN)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        watch_downloads(ctx)
        try:
            if not sign_in(page):
                notify("Couldn't sign in to Amex within 10 minutes - nothing downloaded.")
                return
            path = download_activity(page, out)
        except Exception as e:
            notify(f"Amex pull stopped: {e.__class__.__name__}: {str(e)[:150]} - nothing was imported.")
            raise
        finally:
            ctx.close()
    db_path = paths.data_dir() / "finance.db"
    if dry_run:
        tmp = Path(tempfile.mkdtemp()) / "finance.db"
        shutil.copy(db_path, tmp)
        db_path = tmp
    conn = db.connect(db_path)
    if not dry_run:
        checkpoints.create(conn, "Before the Amex pull")
    line = import_csv(conn, path)
    with (base / "amex_runs.log").open("a") as log:          # what happened, no amounts
        log.write(f"--- {datetime.now():%Y-%m-%d %H:%M}{' (dry run)' if dry_run else ''}\n{line}\n")
    print(line)
    notify(("Dry run: " if dry_run else "") + line[:220])


if __name__ == "__main__":
    try:
        main(dry_run="--dry-run" in sys.argv)
    finally:                                   # the dashboard shows "running" until this pull's pid file is gone
        pid = paths.data_dir() / "bank_fetch" / "bank_pull.pid"
        if pid.exists() and pid.read_text().strip() == str(os.getpid()):
            pid.unlink()
