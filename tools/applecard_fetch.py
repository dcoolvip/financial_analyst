"""Monthly Apple Card pull: sign in at card.apple.com, download the newest statement, import it; read the Savings balance.

    ~/.venvs/financial-analyst/bin/python tools/applecard_fetch.py            # the real thing
    ~/.venvs/financial-analyst/bin/python tools/applecard_fetch.py --dry-run  # download + check, import into a copy

The route (recorded): card.apple.com -> Sign In (opens a pop-up) -> Continue with Password -> Sign In -> a code
from your iPhone -> Statements -> "Download statement of <month> (PDF)" (the newest is first) -> the PDF. Then Savings
(card.apple.com/savings) for the balance, saved as today's value of the Apple Cash Savings account (created the first
time). Statement transactions merge with what's here (date + amount), so nothing is doubled.
Plain Chrome. Sign-in: Sign In on the home page and Continue with Password are pressed for you, and Enter once Chrome
has filled the password in; the code from your iPhone is yours (a notification asks).
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

HOME = "https://card.apple.com/"
SAVINGS = "https://card.apple.com/savings"
WAIT_FOR_SIGN_IN_S = 10 * 60
INSTITUTION = "Apple Card"
SAVINGS_NAME = "Apple Cash Savings"
_MONEY = r"-?\$\s?([\d,]+\.\d{2})"


def notify(text: str) -> None:
    print(text, flush=True)
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Apple Card pull\""],
                   check=False, capture_output=True)


def mask_digits(s: str) -> str:
    return re.sub(r"\d", "#", s)


def fetched_path() -> Path:
    return paths.data_dir() / "bank_fetch" / "applecard_fetched.json"


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


def _menu(page, label: str):
    return page.locator(".menu-item-label", has_text=re.compile(rf"^\s*{label}\s*$")).filter(visible=True).first


def _signed_in(page) -> bool:
    return _menu(page, "Statements").count() > 0


def _sign_in_form_open(page) -> bool:
    """Apple's sign-in pop-up is showing (its frame has the account / password boxes or Continue with Password)."""
    for frame in page.frames:
        try:
            if frame.locator("#continue-password, input[type=password], #account_name_text_field").filter(
                    visible=True).count():
                return True
        except Exception:
            pass
    return False


def sign_in(page) -> bool:
    """Press Sign In on the home page (it opens a pop-up); Apple's sign-in sits in a frame there: press
    "Continue with Password" when it shows, Enter once Chrome has filled
    the password in (never a typed one), and leave the verification code to you."""
    page.goto(HOME)
    pressed, told, opened = set(), False, 0.0
    deadline, ask_after = time.time() + WAIT_FOR_SIGN_IN_S, time.time() + 20
    while time.time() < deadline:
        if _signed_in(page):
            return True
        if not _sign_in_form_open(page) and time.time() - opened > 15:      # the home page: Sign In opens the pop-up
            try:
                page.locator("ui-button, button, [role=button], a").filter(
                    has_text=re.compile(r"^\s*Sign In\s*$", re.I)).filter(visible=True).first.click(timeout=3000)
                opened = time.time()
            except Exception:
                pass
        for frame in page.frames:
            try:
                button = frame.locator("#continue-password").filter(visible=True)
                if button.count() and "continue" not in pressed:
                    pressed.add("continue")
                    button.first.click()
                box = frame.locator("input[type=password]").filter(visible=True).first
                if box.count() and "enter" not in pressed and bank_browser.chrome_filled(box):
                    pressed.add("enter")
                    box.press("Enter")
            except Exception:
                pass
        if not told and time.time() > ask_after:
            notify("Apple Card: finish signing in in the Chrome window (the code from your iPhone) - "
                   "the pull does the rest.")
            told = True
        page.wait_for_timeout(1500)
    return False


def download_latest(page, out: Path, seen: set[str]) -> tuple[str, Path] | None:
    """The newest statement, unless it's already in. Returns (month label, file)."""
    _menu(page, "Statements").click()
    button = page.locator("[aria-label^='Download statement of']").filter(visible=True).first
    button.wait_for(state="visible", timeout=30_000)
    month = re.sub(r"^Download statement of\s*|\s*\(PDF\)\s*$", "", button.get_attribute("aria-label") or "").strip()
    if month in seen:
        return None
    before = len(DOWNLOADS)
    button.click()
    deadline = time.time() + 30
    while time.time() < deadline and len(DOWNLOADS) == before:
        page.wait_for_timeout(500)
        if time.time() > deadline - 25 and len(DOWNLOADS) == before:     # a confirm sheet, if one shows
            try:
                page.locator("[role=dialog] button, [aria-modal=true] button").filter(
                    has_text=re.compile("download", re.I)).first.click(timeout=500)
            except Exception:
                pass
    if len(DOWNLOADS) == before:
        raise TimeoutError("no download came after Download statement")
    dl = DOWNLOADS[before]
    path = out / dl.suggested_filename
    dl.save_as(path)
    return month, path


def savings_balance(text: str) -> float | None:
    """The balance on card.apple.com/savings: the first amount after a "Balance" label."""
    m = re.search(rf"Balance[^$\n]{{0,40}}\n?[^$\n]{{0,40}}{_MONEY}", text or "", re.I)
    return float(m.group(1).replace(",", "")) if m else None


def read_savings(page) -> tuple[float | None, str]:
    page.goto(SAVINGS)
    try:
        page.get_by_text(re.compile("balance", re.I)).first.wait_for(timeout=30_000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    text = page.locator("body").inner_text()
    return savings_balance(text), text


def import_statement(conn, pdf: Path) -> str:
    s = statements.parse_pdf(pdf.read_bytes())
    accts = db.accounts(conn)
    cards = accts[(accts["institution"] == INSTITUTION) & (accts["type"] == "credit_card")]
    if s.institution != INSTITUTION or s.as_of is None or s.balance is None:
        return f"{pdf.name}: couldn't read it as an Apple Card statement - " + "; ".join(s.notes or ["import it in Add data"])
    if len(cards) != 1:
        return f"{pdf.name}: {len(cards)} Apple Cards in the dashboard - import it in Add data to pick one"
    r = apply_statement(conn, s, int(cards["id"].iloc[0]), pdf.name) or {}
    line = f"{cards['name'].iloc[0]}: balance saved ({s.as_of:%b %-d})"
    if r.get("on_statement") is not None:
        line += f", {r['on_statement']} transactions, {r['already_there']} already there, {r['added']} added"
        if r.get("only_in_dashboard"):
            line += f" - {len(r['only_in_dashboard'])} in the dashboard aren't on the statement"
    return line + "".join(f" - {n}" for n in s.notes)


def save_savings(conn, value: float | None, today: date) -> str:
    if value is None:
        return f"{SAVINGS_NAME}: couldn't find the balance on the Savings page - nothing saved"
    aid = db.get_or_create_account(conn, SAVINGS_NAME, INSTITUTION, "savings")
    db.upsert_balance(conn, aid, today, value, "card.apple.com Savings page")
    return f"{SAVINGS_NAME}: balance saved ({today:%b %-d})"


def main(dry_run: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    base = paths.data_dir() / "bank_fetch"
    profile, out = base / "chrome-profile" / "applecard", base / "downloads" / "applecard"
    for d in (profile, out):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    seen = already_fetched()
    got, savings = None, None
    with sync_playwright() as pw:
        ctx = bank_browser.launch(pw, profile, plain=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        watch_downloads(ctx)
        try:
            if not sign_in(page):
                notify("Couldn't sign in to Apple Card within 10 minutes - nothing downloaded.")
                return
            got = download_latest(page, out, seen)
            savings, text = read_savings(page)
            if savings is None:                        # what the page showed, digits hidden - to fix the reader
                print("Savings page shows: " + str([mask_digits(l)[:60] for l in text.splitlines() if l.strip()][:40]),
                      flush=True)
        except Exception as e:
            notify(f"Apple Card pull stopped: {e.__class__.__name__}: {str(e)[:150]} - nothing was imported.")
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
        checkpoints.create(conn, "Before the Apple Card pull")
    lines = [import_statement(conn, got[1]) if got else "Apple Card: no new statement yet",
             save_savings(conn, savings, date.today())]
    if got and not dry_run and "balance saved" in lines[0]:
        fetched_path().write_text(json.dumps(sorted(seen | {got[0]}), indent=1))
    with (base / "applecard_runs.log").open("a") as log:        # what happened, no amounts
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
