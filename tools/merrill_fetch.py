"""Monthly Merrill pull: sign in, fetch new statements, import them (balance + holdings).

    ~/.venvs/financial-analyst/bin/python tools/merrill_fetch.py            # the real thing
    ~/.venvs/financial-analyst/bin/python tools/merrill_fetch.py --dry-run  # download + check, import into a copy

Merrill opens each statement in its own window (olui2.fs.ml.com/Records/StatementsReport.aspx/STMT_…): a web page
wrapping the PDF in a small embedded frame without download buttons. So: click the statement on Statements &
Documents and keep a copy of the PDF as that window loads it (the link is single-use, so it can't be fetched again;
no viewer buttons needed), check it's a PDF, and import it with the Merrill statement reader.
Plain Chrome (no automation signals), as for Amex. Sign-in: Merrill passes through an interim page first; whenever a
page shows the password Chrome filled in, Enter is pressed; otherwise a notification asks you to sign in.
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
from finance import bank_browser, checkpoints, db, paths  # noqa: E402
from finance.importers import apply_statement, statements  # noqa: E402

HOME = "https://www.merrilledge.com/"
DOCUMENTS = "https://olui2.fs.ml.com/Records/Overview.aspx"
SIGNED_IN = re.compile(r"\b(log ?out|sign ?out)\b", re.I)
WAIT_FOR_SIGN_IN_S = 10 * 60
RECENT_DAYS = 45
INSTITUTION = "Merrill"
STATEMENT = re.compile(r"CMA|Edge|IRA|Brokerage|Account", re.I)     # account statements, not "Equity Cost Basis"
NOT_STATEMENT = re.compile(r"cost basis|confirm|tax|notice|disclosure|more ", re.I)
_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def notify(text: str) -> None:
    print(text, flush=True)
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Merrill pull\""],
                   check=False, capture_output=True)


def mask_digits(s: str) -> str:
    return re.sub(r"\d", "#", s)


def fetched_path() -> Path:
    return paths.data_dir() / "bank_fetch" / "merrill_fetched.json"


def already_fetched() -> set[str]:
    try:
        return set(json.loads(fetched_path().read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def row_info(row_text: str, link_text: str) -> dict | None:
    """'09/30/2026  CMA Edge' -> a statement worth fetching (an account statement, with its date)."""
    m = _DATE.search(row_text or "")
    name = " ".join((link_text or "").split())
    if not m or not STATEMENT.search(name) or NOT_STATEMENT.search(name):
        return None
    return {"date": date(int(m.group(3)), int(m.group(1)), int(m.group(2))), "name": name}


def wanted(info: dict, seen: set[str], today: date) -> bool:
    return today - info["date"] <= timedelta(days=RECENT_DAYS) and key(info) not in seen


def key(info: dict) -> str:
    return f"{info['name']}|{info['date'].isoformat()}"


def _signed_in(page) -> bool:
    if page.locator("input[type=password]").filter(visible=True).count():
        return False                                     # a sign-in form is still on screen
    return "olui2.fs.ml.com" in page.url or page.get_by_text(SIGNED_IN).first.is_visible()


def _press_if_filled(page, pressed: set[str]) -> bool:
    """If this page shows a password Chrome has filled in, press Enter - once per page (Merrill passes you through
    an interim sign-in page first, then the real one with the saved login filled)."""
    try:
        pwd = page.locator("input[type=password]").filter(visible=True).first
        here = page.url.split("?")[0]
        if here not in pressed and pwd.count() and pwd.input_value():
            pressed.add(here)
            pwd.press("Enter")
            return True
    except Exception:
        pass
    return False


def sign_in(page) -> bool:
    # Start at merrilledge.com: that's where Chrome has the Merrill login saved (the olui2 sign-in page that the
    # statements page redirects to has none). Statements & Documents comes after, in download_new.
    page.goto(HOME)
    pressed, told = set(), False
    deadline, ask_after = time.time() + WAIT_FOR_SIGN_IN_S, time.time() + 15
    while time.time() < deadline:
        if _signed_in(page):
            return True
        if not _press_if_filled(page, pressed) and not told and time.time() > ask_after:
            notify("Merrill: sign in in the Chrome window (pick your saved login) - the pull does the rest.")
            told = True                                  # never submits an empty password
        page.wait_for_timeout(1500)
    return False


CAUGHT: list = []        # PDFs caught on their way into a statement window


def catch_pdfs(ctx) -> None:
    """Statement links are single-use (…&g=<one-time code>): asking again gives an error page. So keep a copy of any
    PDF as the window loads it, and let the window show it as usual."""
    def handle(route):
        resp = route.fetch()
        body = resp.body()
        if body.startswith(b"%PDF"):
            CAUGHT.append(body)
        route.fulfill(response=resp, body=body)
    ctx.route("**/Records/**", handle)


def wait_for_pdf(page, before: int, timeout_s: float = 30) -> bytes:
    deadline = time.time() + timeout_s
    while time.time() < deadline and len(CAUGHT) == before:
        page.wait_for_timeout(500)
    if len(CAUGHT) == before:
        raise ValueError("no PDF came into the statement window")
    return CAUGHT[before]


def goto_settled(page, url: str, tries: int = 3) -> None:
    """Go to url, letting Merrill finish its own redirects first: right after sign-in it sends you on to the accounts
    overview, and going elsewhere at that moment gets cut off ("interrupted by another navigation")."""
    for attempt in range(tries):
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        try:
            page.goto(url)
            return
        except Exception as e:
            if "interrupted by another navigation" not in str(e) or attempt == tries - 1:
                raise
            page.wait_for_timeout(3000)


def download_new(page, out: Path, seen: set[str]) -> list[tuple[str, Path]]:
    goto_settled(page, DOCUMENTS)
    page.wait_for_timeout(4000)
    today, got = date.today(), []
    rows = page.locator("tr").filter(has_text=_DATE)
    tried = set()
    for i in range(rows.count()):
        row = rows.nth(i)
        text = row.inner_text()
        if len(_DATE.findall(text)) != 1:                # a row wrapping a whole box of statements, not one statement
            continue
        link = row.locator("a").first
        if not link.count():
            continue
        info = row_info(text, link.inner_text())
        if not info or not wanted(info, seen | tried, today):
            continue
        tried.add(key(info))
        try:
            before = len(CAUGHT)
            with page.expect_popup(timeout=20_000) as pop:
                link.click()
            win = pop.value
            body = wait_for_pdf(page, before)
            path = out / f"ML_{info['date']:%m%d%Y}_{re.sub(r'[^A-Za-z0-9]+', '', info['name'])}.pdf"
            path.write_bytes(body)
            got.append((key(info), path))
            win.close()
        except Exception as e:
            print(f"Couldn't fetch {mask_digits(info['name'])} {info['date']}: {e}", flush=True)
    if not got:
        seen_rows = [mask_digits(" ".join(t.split()))[:60] for t in rows.all_inner_texts()][:15]
        print(f"Statement rows seen: {seen_rows}", flush=True)
    return got


def import_statement(conn, pdf: Path) -> str:
    s = statements.parse_pdf(pdf.read_bytes())
    accts = db.accounts(conn)
    mine = accts[accts["institution"].str.startswith(INSTITUTION) & accts["type"].isin(["brokerage", "retirement"])]
    hit = mine[mine["last4"] == s.last4] if s.last4 else mine.iloc[0:0]
    if len(hit) != 1:
        hit = mine if len(mine) == 1 else mine.iloc[0:0]
    if not s.institution.startswith(INSTITUTION) or len(hit) != 1:
        return f"{pdf.name}: couldn't tell which dashboard account it is (ending {s.last4 or '?'}) - import it in Add data"
    aid, name = int(hit["id"].iloc[0]), hit["name"].iloc[0]
    apply_statement(conn, s, aid, pdf.name)
    extras = f", {len(s.holdings)} positions" if s.holdings else ""
    return f"{name}: value saved ({s.as_of:%b %-d}){extras}"


def main(dry_run: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    base = paths.data_dir() / "bank_fetch"
    profile, out = base / "chrome-profile" / "merrill", base / "downloads" / "merrill"
    for d in (profile, out):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    seen = already_fetched()
    with sync_playwright() as pw:
        ctx = bank_browser.launch(pw, profile, plain=True)
        catch_pdfs(ctx)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if not sign_in(page):
                notify("Couldn't sign in to Merrill within 10 minutes - nothing downloaded.")
                return
            new = download_new(page, out, seen)
        except Exception as e:
            notify(f"Merrill pull stopped: {e.__class__.__name__}: {str(e)[:150]} - nothing was imported.")
            raise
        finally:
            ctx.close()
    if not new:
        notify("No new Merrill statements yet.")
        return
    db_path = paths.data_dir() / "finance.db"
    if dry_run:
        tmp = Path(tempfile.mkdtemp()) / "finance.db"
        shutil.copy(db_path, tmp)
        db_path = tmp
    conn = db.connect(db_path)
    if not dry_run:
        checkpoints.create(conn, f"Before the Merrill pull ({len(new)} statements)")
    lines = [import_statement(conn, path) for _, path in sorted(new, key=lambda x: x[1].name)]
    if not dry_run:
        fetched_path().write_text(json.dumps(sorted(seen | {k for k, _ in new}), indent=1))
    with (base / "merrill_runs.log").open("a") as log:        # what happened, no amounts
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
