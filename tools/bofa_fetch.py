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

HOME = "https://www.bankofamerica.com/"     # no sign-in boxes here: its "Log In" opens the sign-in page
SIGN_IN = ("https://secure.bankofamerica.com/myaccounts/signin/signIn.go?returnSiteIndicator=GAIMW&langPref=en-us"
           "&request_locale=en-us&capturemode=N&newuser=false&bcIP=F")   # where "Log In" leads (as you saw it)
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

AUTOFILL_WAIT_MS = 2500


def _visible_password(page, timeout_s: float = 30):
    """Whatever password box is on screen - in the page or any frame inside it (no guessing at BofA's names)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for frame in page.frames:
            try:
                box = frame.locator("input[type=password] >> visible=true").first
                if box.count():
                    return frame, box
            except Exception:
                pass
        page.wait_for_timeout(500)
    return None, None


def _user_box(frame, pwd):
    """The visible text box in the same form as the password box (the user ID)."""
    form = pwd.locator("xpath=ancestor::form[1]")
    scope = form if form.count() else frame
    box = scope.locator("input[type=text] >> visible=true, input[type=email] >> visible=true").first
    return box if box.count() else None


def _fields_seen(page) -> str:
    """Every input box in the page and its frames (frame, id, name, type, shown or not) - never their contents."""
    seen = []
    for i, frame in enumerate(page.frames):
        try:
            seen += [f"f{i}:" + x for x in frame.locator("input").evaluate_all("""els => els.slice(0, 30).map(e =>
                `${e.id || '-'}/${e.name || '-'}/${e.type}/${e.offsetParent ? 'shown' : 'hidden'}`)""")]
        except Exception:
            pass
    return "; ".join(seen)


def _autofilled(box) -> bool:
    """Chrome filled this box from its saved passwords (checked without reading what's in it)."""
    try:
        return bool(box.evaluate("e => e.matches(':-webkit-autofill') || e.matches(':autofill')"))
    except Exception:
        return False


QUICK_MS = 5000


def _submit(frame, pwd, login) -> None:
    """Chrome's saved password first: click into the box (that's when Chrome hands it to the page) and press
    Enter if it's filled. Else type the Keychain login. Else ask you. Every step gives up quickly, never stalls."""
    pwd.click(timeout=QUICK_MS)
    pwd.page.wait_for_timeout(700)
    if (pwd.input_value(timeout=QUICK_MS) or "") or _autofilled(pwd):
        pwd.press("Enter", timeout=QUICK_MS)
    elif login:
        uid = _user_box(frame, pwd)
        if uid is not None and not (uid.input_value(timeout=QUICK_MS) or "").strip():   # "Save User ID" may fill it
            uid.fill(login[0], timeout=QUICK_MS)
        pwd.fill(login[1], timeout=QUICK_MS)
        pwd.press("Enter", timeout=QUICK_MS)
    else:
        notify("Chrome didn't fill in your BofA password - sign in in the Chrome window; the pull continues.")


def sign_in(page, login: tuple[str, str] | None) -> bool:
    """Sign in on BofA's home page: with Chrome's saved password if it fills the boxes (click + Enter), else the
    Keychain login, else ask you to sign in in the window."""
    page.goto(HOME)
    if page.get_by_text(SIGNED_IN).first.is_visible():           # still signed in from before
        return True
    try:                                                         # BofA's "Log In" opens its sign-in page
        page.get_by_role("link", name=re.compile(r"^\s*log ?in\s*$", re.I)).or_(
            page.get_by_role("button", name=re.compile(r"^\s*log ?in\s*$", re.I))).first.click(timeout=10_000)
    except Exception:
        page.goto(SIGN_IN)
    frame, pwd = _visible_password(page, timeout_s=15)
    if not pwd:                                                  # the link moved: go to the sign-in page itself
        page.goto(SIGN_IN)
        frame, pwd = _visible_password(page, timeout_s=15)
    page.wait_for_timeout(AUTOFILL_WAIT_MS)                      # give Chrome a moment to fill them in
    if not pwd:
        print(f"No visible password box at {page.url.split('?')[0]}. Boxes seen: {_fields_seen(page)}", flush=True)
        notify("Couldn't find BofA's sign-in boxes - sign in yourself in the Chrome window; the pull continues.")
    else:
        try:
            _submit(frame, pwd, login)
        except Exception as e:          # you signed in yourself, or the page moved on: just wait for "signed in"
            print(f"Sign-in step didn't finish ({e.__class__.__name__}) - waiting for you to be signed in.", flush=True)
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


def mask_digits(s: str) -> str:
    return re.sub(r"\d", "#", s)


LABEL_KINDS = [(r"mortgage|home loan|heloc", "loan"), (r"cash rewards|card|visa|mastercard|travel rewards", "credit_card"),
               (r"savings|checking|banking", "deposit")]


def matches_tile(info: dict, s: statements.Statement) -> str | None:
    """Is this PDF really the statement on the tile we clicked? None if yes, else why not."""
    digits = re.findall(r"(\d{4})\s*$", info["label"])
    if digits and s.last4 and s.last4 != digits[0]:
        return f"it's for an account ending {s.last4}, not {digits[0]}"
    for pattern, kind in LABEL_KINDS:
        if re.search(pattern, info["label"], re.I) and s.kind != kind:
            return f"it reads as a {s.kind.replace('_', ' ')} statement, not {kind.replace('_', ' ')}"
    return None


def check_downloads(new: list) -> list[tuple]:
    """Each download with the reason it must not be imported (None = fine). Tiles with digits or a kind in their
    name are checked against the PDF; and no two tiles may bring down the same document (a nickname tile like
    "DCOOLVIP" says nothing about its account, so that's the check that covers it)."""
    parsed = [(key, path, info, statements.parse_pdf(path.read_bytes())) for key, path, info in new]
    out, claimed = [], {}
    for key, path, info, s in sorted(parsed, key=lambda x: bool(matches_tile(x[2], x[3])) or not
                                     re.search(r"\d{4}\s*$", x[2]["label"])):     # tiles that prove themselves first
        why = matches_tile(info, s)
        doc = (s.kind, s.last4, s.as_of)
        if not why and doc in claimed:
            why = f"it's the same document as the {mask_digits(claimed[doc])} one"
        if not why:
            claimed[doc] = info["label"]
        out.append((key, path, info, why))
    return sorted(out, key=lambda x: x[1].name)


def download_new(page, out: Path, seen: set[str]) -> list[tuple[str, Path, dict]]:
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
        # this statement's own link: "Download PDF for September Statement for Advantage Savings - 1234"
        link = page.locator("#downloadPDFLink").filter(has_text=re.compile(
            rf"for\s+{re.escape(info['title'])}\s+for\s+{re.escape(info['label'])}", re.I)).first
        try:
            link.wait_for(state="visible", timeout=15_000)
        except Exception:
            print(f"No download link for {info['title']} - {mask_digits(info['label'])} - skipped", flush=True)
            page.keyboard.press("Escape")
            continue
        with page.expect_download(timeout=60_000) as dl:
            link.click()
        slug = re.sub(r"[^A-Za-z0-9]+", "_", info["label"]).strip("_")
        path = out / f"{info['date'].isoformat()}_{slug}.pdf"
        dl.value.save_as(path)
        got.append((tile_key(info), path, info))
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
    lines, done = [], set()
    for key, path, info, why in check_downloads(new):
        if why:                       # the wrong document came down: never import it; try again next time
            lines.append(f"{info['title']} - {mask_digits(info['label'])}: downloaded the wrong document ({why}) - "
                         "not imported")
            continue
        lines.append(import_statement(conn, path))
        done.add(key)
    if not dry_run:
        fetched_path().write_text(json.dumps(sorted(seen | done), indent=1))
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
