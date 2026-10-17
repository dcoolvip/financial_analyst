"""Monthly Chase pull: sign in, download new statements, import them without doubles.

    ~/.venvs/financial-analyst/bin/python tools/chase_fetch.py            # the real thing
    ~/.venvs/financial-analyst/bin/python tools/chase_fetch.py --dry-run  # download + check, import into a copy

Signs in with the password Chrome saved for Chase in this profile (click into the password box, Enter); if Chase
asks for a code, type it in the window. Opens Statements & documents and downloads every statement from the last
45 days it hasn't fetched before - bank accounts via "save document" -> Save as PDF -> Submit, cards via
"Saves document" -> Submit (the route recorded in tools/bank_recorder.py). Chase names each file after the
account's last 4 digits, which picks the dashboard account. Each statement proves itself (running balance or the
card summary) before anything is imported.
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
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from finance import checkpoints, db, paths  # noqa: E402
from finance.importers import apply_statement, chase_pdf, statements  # noqa: E402

DASHBOARD = "https://secure.chase.com/web/auth/dashboard"
DOCUMENTS = DASHBOARD + "#/dashboard/documents/myDocs/index;mode=documents"
SIGNED_IN = re.compile(r"\bsign ?out\b", re.I)
WAIT_FOR_SIGN_IN_S = 10 * 60
RECENT_DAYS = 45
MAX_PER_RUN = 12
INSTITUTION = "Chase"
_DATE = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")


def notify(text: str) -> None:
    print(text, flush=True)
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Chase pull\""],
                   check=False, capture_output=True)


def mask_digits(s: str) -> str:
    return re.sub(r"\d", "#", s)


def fetched_path() -> Path:
    return paths.data_dir() / "bank_fetch" / "chase_fetched.json"


def already_fetched() -> set[str]:
    try:
        return set(json.loads(fetched_path().read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def link_date(text: str) -> date | None:
    """'Oct 06, 2026 Statement SAPPHIRE RESERVE (...2349) Saves document' -> Oct 6, 2026."""
    m = _DATE.search(text or "")
    return datetime.strptime(" ".join(m.groups()), "%b %d %Y").date() if m else None


def wanted(text: str, seen: set[str], today: date) -> bool:
    """A statement link from the last RECENT_DAYS that hasn't been fetched yet."""
    when = link_date(text)
    return bool(when and today - when <= timedelta(days=RECENT_DAYS) and " ".join(text.split()) not in seen)


# --- matching and importing -------------------------------------------------------------------------------------

def match_account(conn, last4: str | None) -> int | None:
    accts = db.accounts(conn)
    hit = accts[(accts["institution"] == INSTITUTION) & (accts["last4"] == last4)]
    return int(hit["id"].iloc[0]) if last4 and len(hit) == 1 else None


def import_statement(conn, pdf: Path) -> str:
    if "statements" not in pdf.name:
        return f"{mask_digits(pdf.name)}: not a statement - skipped"
    s = statements.parse_pdf(pdf.read_bytes())
    s.last4 = chase_pdf.last4_from_filename(pdf.name) or s.last4
    aid = match_account(conn, s.last4)
    if s.institution != INSTITUTION or aid is None:
        return f"{mask_digits(pdf.name)}: couldn't tell which dashboard account it is (ending {s.last4 or '?'}) - " \
               "import it in Add data"
    name = db.accounts(conn).set_index("id").loc[aid, "name"]
    r = apply_statement(conn, s, aid, pdf.name)
    if r.get("error"):
        return f"{name}: balance saved; {r['error']}"
    if r.get("on_statement"):
        return (f"{name} ({r['period'][0]:%b %-d} – {r['period'][1]:%b %-d}): {r['on_statement']} transactions, "
                f"{r['already_there']} already there, {r['added']} added"
                + (f", {r['only_in_dashboard']} in the dashboard but not on the statement" if r["only_in_dashboard"] else ""))
    return f"{name}: balance saved ({s.as_of:%b %-d}); no transactions this period"


# --- the browser part -------------------------------------------------------------------------------------------

def _password_box(page, timeout_s: float = 30):
    """Chase's sign-in sits in an embedded frame: the first visible password box in any frame."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.get_by_text(SIGNED_IN).first.is_visible():
            return None
        for frame in page.frames:
            try:
                box = frame.locator("input[type=password] >> visible=true").first
                if box.count():
                    return box
            except Exception:
                pass
        page.wait_for_timeout(500)
    return None


def sign_in(page) -> bool:
    """Open Chase -> click the password box Chrome filled in -> Enter. Then wait until you're signed in."""
    page.goto(DASHBOARD)
    pwd = _password_box(page)
    if pwd is not None:
        try:
            pwd.click()
            page.wait_for_timeout(1000)
            pwd.press("Enter")
        except Exception:
            pass                          # you signed in yourself / the page moved on: just wait below
    deadline = time.time() + WAIT_FOR_SIGN_IN_S
    while time.time() < deadline:
        if page.get_by_text(SIGNED_IN).first.is_visible():
            return True
        page.wait_for_timeout(2000)
    return False


def _save(page, click, out: Path) -> Path | None:
    """Click, confirm Chase's Submit, and save the download under Chase's own file name."""
    with page.expect_download(timeout=30_000) as dl:
        click()
        submit = page.locator("input[type=submit], button:has-text('Submit')").filter(visible=True).first
        submit.wait_for(state="visible", timeout=10_000)
        submit.click()
    path = out / dl.value.suggested_filename
    dl.value.save_as(path)
    page.keyboard.press("Escape")
    page.wait_for_timeout(1000)
    return path


def _shown_buttons(page) -> str:
    """What could be clicked on screen right then (labels only, digits masked) - for fixing, not guessing."""
    try:
        labels = page.locator("button, input[type=submit], [role=menuitem], [role=button]").filter(
            visible=True).evaluate_all("els => els.slice(0, 40).map(e => (e.innerText || e.value || "").trim())")
        return "On screen: " + str([mask_digits(" ".join(l.split()))[:40] for l in labels if l][:25])
    except Exception:
        return ""


def download_new(page, out: Path, seen: set[str]) -> list[tuple[str, Path]]:
    page.goto(DOCUMENTS)
    page.wait_for_timeout(5000)
    if not page.locator("[id^='button-documentsAccordion'], [id$='downloadDocumentDropdown-icon']").count():
        try:                                                 # the address moved: use the menu
            page.get_by_role("link", name=re.compile(r"statements? (&|and) documents", re.I)).first.click()
            page.wait_for_timeout(5000)
        except Exception:
            pass
    for b in page.locator("button[id^='button-documentsAccordion']").all():   # open every account's section
        try:
            if b.get_attribute("aria-expanded") == "false":
                b.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass
    today, got = date.today(), []
    # Chase lists every link twice (a hidden copy for another layout): only the ones on screen
    bank_rows = page.locator("[id$='downloadDocumentDropdown-icon']").filter(visible=True)   # "Sep 16, 2026 Opens menu…"
    card_rows = page.locator("[id$='requestThisDocumentLink-download']").filter(visible=True)  # "Oct 06, 2026 Statement …"
    tried = set()
    for i in range(bank_rows.count()):
        row = bank_rows.nth(i)
        text = " ".join((row.inner_text() or row.get_attribute("aria-label") or "").split())
        if len(got) >= MAX_PER_RUN or not wanted(text, seen | tried, today):
            continue
        tried.add(text)
        def click(row=row):
            row.click()
            page.locator("[id$='downloadPDFOption']").filter(visible=True).first.click(timeout=10_000)
        try:
            got.append((text, _save(page, click, out)))
        except Exception as e:
            print(f"Couldn't download {mask_digits(text)}: {e.__class__.__name__}. {_shown_buttons(page)}", flush=True)
            page.keyboard.press("Escape")
    for i in range(card_rows.count()):
        row = card_rows.nth(i)
        text = " ".join((row.inner_text() or row.get_attribute("aria-label") or "").split())
        if len(got) >= MAX_PER_RUN or "statement" not in text.lower() or not wanted(text, seen | tried, today):
            continue
        tried.add(text)
        try:
            got.append((text, _save(page, row.click, out)))
        except Exception as e:
            print(f"Couldn't download {mask_digits(text)}: {e.__class__.__name__}. {_shown_buttons(page)}", flush=True)
            page.keyboard.press("Escape")
    if not got:
        rows = [mask_digits(" ".join(t.split()))[:80] for t in (bank_rows.all_inner_texts() + card_rows.all_inner_texts())]
        print(f"Statement links seen: {rows[:20]}", flush=True)
    return got


def main(dry_run: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    base = paths.data_dir() / "bank_fetch"
    profile, out = base / "chrome-profile" / "chase", base / "downloads" / "chase"
    for d in (profile, out):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    seen = already_fetched()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False,
                                                    accept_downloads=True, viewport=None)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if not sign_in(page):
                notify("Couldn't sign in to Chase within 10 minutes - nothing downloaded.")
                return
            new = download_new(page, out, seen)
        except Exception as e:
            notify(f"Chase pull stopped: {e.__class__.__name__}: {str(e)[:150]} - nothing half-done was imported.")
            raise
        finally:
            ctx.close()
    if not new:
        notify("No new Chase statements yet.")
        return
    db_path = paths.data_dir() / "finance.db"
    if dry_run:
        tmp = Path(tempfile.mkdtemp()) / "finance.db"
        shutil.copy(db_path, tmp)
        db_path = tmp
    conn = db.connect(db_path)
    if not dry_run:
        checkpoints.create(conn, f"Before the Chase pull ({len(new)} statements)")
    lines = [import_statement(conn, path) for _, path in sorted(new, key=lambda x: x[1].name)]
    if not dry_run:
        fetched_path().write_text(json.dumps(sorted(seen | {k for k, _ in new}), indent=1))
    with (base / "chase_runs.log").open("a") as log:         # what happened, no amounts
        log.write(f"--- {datetime.now():%Y-%m-%d %H:%M}{' (dry run)' if dry_run else ''}\n" + "\n".join(lines) + "\n")
    print("\n".join(lines))
    notify(("Dry run: " if dry_run else "") + f"{len(new)} Chase statement(s) - " + "; ".join(lines)[:200])


if __name__ == "__main__":
    try:
        main(dry_run="--dry-run" in sys.argv)
    finally:                                   # the dashboard shows "running" until this pull's pid file is gone
        pid = paths.data_dir() / "bank_fetch" / "bank_pull.pid"
        if pid.exists() and pid.read_text().strip() == str(os.getpid()):
            pid.unlink()
