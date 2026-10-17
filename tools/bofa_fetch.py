"""Monthly Bank of America pull: sign in, download new statements, import them without doubles.

    ~/.venvs/financial-analyst/bin/python tools/bofa_fetch.py            # the real thing
    ~/.venvs/financial-analyst/bin/python tools/bofa_fetch.py --dry-run  # download + check, import into a copy

Signs in with the password Chrome saved for BofA in this profile (it just presses Enter once Chrome fills the
boxes). If Chrome doesn't, it uses a login saved in the macOS Keychain (optional, "FinancialAnalyst BofA"),
else asks you to sign in in the window. If BofA asks for a text code, a notification asks you to finish there. Then it opens Statements & Documents, downloads every
statement it hasn't fetched before, checks each one against its own printed totals, and imports it: balance,
plus transactions that aren't already in the dashboard. A notification says what happened.

Uses the same Chrome profile as tools/bank_recorder.py, so BofA keeps recognizing this browser.
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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from finance import bank_pull, checkpoints, db, paths  # noqa: E402
from finance.importers import apply_statement, statements  # noqa: E402

SIGN_IN = "https://secure.bankofamerica.com/myaccounts/signin/signIn.go"   # BofA's own sign-in page
STATEMENTS = "https://secure.bankofamerica.com/statements-documents/home/"
SIGNED_IN = re.compile(r"\b(log ?out|sign ?out)\b", re.I)
NEEDS_YOU = re.compile(r"(verification code|one-time code|authorization code|text message|verify (it'?s )?you"
                       r"|security code|we sent)", re.I)
WAIT_FOR_SIGN_IN_S = 10 * 60
MAX_PER_RUN = 24
INSTITUTION = "Bank of America"


def notify(text: str) -> None:
    print(text, flush=True)
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Bank of America pull\""],
                   check=False, capture_output=True)


def keychain_login() -> tuple[str, str] | None:
    """(user ID, password) from the Keychain, if saved there - only needed when Chrome doesn't fill them in."""
    return bank_pull.keychain_login()


# --- which statement tiles are new ------------------------------------------------------------------------------

def tile_info(text: str) -> dict | None:
    """'September Statement / Advantage Savings - 1234 / Sep 25, 2026 / ...' -> label + date."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 3 or "statement" not in lines[0].lower():
        return None
    try:
        when = datetime.strptime(lines[2].replace(",", ""), "%b %d %Y").date()
    except ValueError:
        return None
    return {"title": lines[0], "label": lines[1], "date": when}


def fetched_path() -> Path:
    return paths.data_dir() / "bank_fetch" / "bofa_fetched.json"


def already_fetched() -> set[str]:
    try:
        return set(json.loads(fetched_path().read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def tile_key(info: dict) -> str:
    return f"{info['label']}|{info['date'].isoformat()}"


# --- matching a downloaded statement to a dashboard account -----------------------------------------------------

KIND_TYPES = {"deposit": ["checking", "savings"], "credit_card": ["credit_card"], "loan": ["mortgage", "heloc"]}


def match_account(conn, s: statements.Statement) -> int | None:
    accts = db.accounts(conn)
    bofa = accts[(accts["institution"] == INSTITUTION) & accts["type"].isin(KIND_TYPES.get(s.kind, []))]
    if s.last4 and len(hit := bofa[bofa["last4"] == s.last4]) == 1:
        return int(hit["id"].iloc[0])
    if s.kind == "deposit" and s.account_type:                 # checking vs savings
        bofa = bofa[bofa["type"] == s.account_type]
    blank = bofa[bofa["last4"].isna() | (bofa["last4"] == "")]
    return int(blank["id"].iloc[0]) if len(bofa) == 1 and len(blank) == 1 else None


def import_statement(conn, pdf: Path) -> str:
    s = statements.parse_pdf(pdf.read_bytes())
    if s.institution != INSTITUTION or s.kind == "unknown":
        return f"{pdf.name}: not a Bank of America statement I can read - skipped"
    aid = match_account(conn, s)
    if aid is None:
        return f"{pdf.name}: couldn't tell which dashboard account it is (ending {s.last4 or '?'}) - import it in Add data"
    name = db.accounts(conn).set_index("id").loc[aid, "name"]
    r = apply_statement(conn, s, aid, pdf.name)
    if r.get("error"):
        return f"{name}: balance saved; {r['error']}"
    if r.get("on_statement"):
        return (f"{name} ({r['period'][0]:%b %-d} – {r['period'][1]:%b %-d}): {r['on_statement']} transactions, "
                f"{r['already_there']} already there, {r['added']} added"
                + (f", {r['only_in_dashboard']} in the dashboard but not on the statement" if r["only_in_dashboard"] else ""))
    return f"{name}: balance{' and loan details' if s.kind == 'loan' else ''} saved ({s.as_of:%b %-d})"


# --- the browser part -------------------------------------------------------------------------------------------

USER_FIELDS = ["#oid", "#onlineId1", "input[name=onlineId1]", "input[name=oid]", "input[autocomplete=username]"]


def _visible(page, selectors: list[str], timeout_s: float = 30):
    """The first of these that's on screen (BofA keeps hidden copies of its sign-in boxes around)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for sel in selectors:
            loc = page.locator(f"{sel} >> visible=true").first
            if loc.count():
                return loc
        page.wait_for_timeout(500)
    return None


def _fields_seen(page) -> str:
    """Which input boxes are on the page (id, name, type, shown or not) - never their contents."""
    return "; ".join(page.eval_on_selector_all("input", """els => els.slice(0, 25).map(e =>
        `${e.id || '-'}/${e.name || '-'}/${e.type}/${e.offsetParent ? 'shown' : 'hidden'}`)"""))


AUTOFILL_WAIT_MS = 2500


def _autofilled(box) -> bool:
    """Chrome filled this box from its saved passwords (checked without reading what's in it)."""
    try:
        return bool(box.evaluate("e => e.matches(':-webkit-autofill') || e.matches(':autofill')"))
    except Exception:
        return False


def sign_in(page, login: tuple[str, str] | None) -> bool:
    """Sign in with Chrome's saved password if it fills the boxes (just press Enter), else the Keychain login,
    else ask you to sign in in the window."""
    page.goto(SIGN_IN)
    if page.get_by_text(SIGNED_IN).first.is_visible():           # still signed in from before
        return True
    uid = _visible(page, USER_FIELDS)
    pwd = _visible(page, ["input[type=password]"], timeout_s=5)
    page.wait_for_timeout(AUTOFILL_WAIT_MS)                      # give Chrome a moment to fill them in
    if not uid or not pwd:
        print("Sign-in boxes not found. Boxes on the page: " + _fields_seen(page), flush=True)
        notify("Couldn't find BofA's sign-in boxes - sign in yourself in the Chrome window; the pull continues.")
    elif _autofilled(pwd):
        pwd.click()                     # Chrome releases a saved password to the page only after a real click
        pwd.press("Enter")
    elif login:
        if not (uid.input_value() or "").strip():                # "Save User ID" may have filled it in
            uid.fill(login[0])
        pwd.fill(login[1])
        pwd.press("Enter")
    else:
        notify("Chrome didn't fill in your BofA password - sign in in the Chrome window; the pull continues.")
    told, deadline = False, time.time() + WAIT_FOR_SIGN_IN_S
    while time.time() < deadline:
        try:
            if page.get_by_text(SIGNED_IN).first.is_visible():
                return True
            if not told and page.get_by_text(NEEDS_YOU).first.is_visible():
                notify("BofA wants a code - finish signing in in the Chrome window.")
                told = True
        except Exception:
            pass
        page.wait_for_timeout(2000)
    return False


def download_new(page, out: Path, seen: set[str]) -> list[tuple[str, Path]]:
    page.goto(STATEMENTS)
    page.wait_for_timeout(4000)
    got = []
    tiles = page.locator("div.front")
    for i in range(tiles.count()):
        if len(got) >= MAX_PER_RUN:
            break
        tile = tiles.nth(i)
        info = tile_info(tile.inner_text())
        if not info or tile_key(info) in seen:
            continue
        tile.click()
        link = page.locator("#downloadPDFLink").first
        link.wait_for(state="visible", timeout=15_000)
        with page.expect_download(timeout=60_000) as dl:
            link.click()
        slug = re.sub(r"[^A-Za-z0-9]+", "_", info["label"]).strip("_")
        path = out / f"{info['date'].isoformat()}_{slug}.pdf"
        dl.value.save_as(path)
        got.append((tile_key(info), path))
        page.keyboard.press("Escape")                        # close the statement panel before the next tile
        page.wait_for_timeout(1000)
    return got


def main(dry_run: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    login = keychain_login()
    base = paths.data_dir() / "bank_fetch"
    profile, out = base / "chrome-profile" / "bofa", base / "downloads" / "bofa"
    for d in (profile, out):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    seen = already_fetched()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False,
                                                    accept_downloads=True, viewport=None)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if not sign_in(page, login):
                notify("Couldn't sign in to BofA within 10 minutes - nothing downloaded.")
                return
            new = download_new(page, out, seen)
        except Exception as e:
            notify(f"BofA pull stopped: {e.__class__.__name__}: {str(e)[:150]} - nothing half-done was imported.")
            raise
        finally:
            ctx.close()
    if not new:
        notify("No new BofA statements yet.")
        return
    db_path = paths.data_dir() / "finance.db"
    if dry_run:
        tmp = Path(tempfile.mkdtemp()) / "finance.db"
        shutil.copy(db_path, tmp)
        db_path = tmp
    conn = db.connect(db_path)
    if not dry_run:
        checkpoints.create(conn, f"Before the BofA pull ({len(new)} statements)")
    lines = [import_statement(conn, path) for _, path in sorted(new, key=lambda x: x[1].name)]
    if not dry_run:
        fetched_path().write_text(json.dumps(sorted(seen | {k for k, _ in new}), indent=1))
    with (base / "bofa_runs.log").open("a") as log:          # what happened, no amounts
        log.write(f"--- {datetime.now():%Y-%m-%d %H:%M}{' (dry run)' if dry_run else ''}\n" + "\n".join(lines) + "\n")
    print("\n".join(lines))
    notify(("Dry run: " if dry_run else "") + f"{len(new)} BofA statement(s) - " + "; ".join(lines)[:200])


if __name__ == "__main__":
    try:
        main(dry_run="--dry-run" in sys.argv)
    finally:                                   # the dashboard shows "running" until this pull's pid file is gone
        pid = paths.data_dir() / "bank_fetch" / "bofa_pull.pid"
        if pid.exists() and pid.read_text().strip() == str(os.getpid()):
            pid.unlink()
