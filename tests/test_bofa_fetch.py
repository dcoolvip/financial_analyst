"""The BofA pull's decisions, without a browser: which tiles are new, which account a statement is, the report."""
import importlib.util
import subprocess
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from finance import db
from tests.test_statements import BOFA_STMT, _pdf

spec = importlib.util.spec_from_file_location("bofa_fetch", Path(__file__).parent.parent / "tools/bofa_fetch.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)


def test_statement_tiles_are_read_and_remembered():
    info = bf.tile_info("September Statement\nAdvantage Savings - 1234\nSep 25, 2026\nActivating will open this ...")
    assert info == {"title": "September Statement", "label": "Advantage Savings - 1234", "date": date(2026, 9, 25)}
    assert bf.tile_key(info) == "Advantage Savings - 1234|2026-09-25"
    assert bf.tile_info("Tax documents\nsomething") is None


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    return db.connect(tmp_path / "finance.db")


def test_a_statement_finds_its_account_by_digits_else_the_only_one_of_its_kind(conn, tmp_path):
    chk = db.get_or_create_account(conn, "Bofa Checking", "Bank of America", "checking")
    sav = db.get_or_create_account(conn, "Bofa Savings", "Bank of America", "savings")
    db.get_or_create_account(conn, "Chase Checking", "Chase", "checking")
    pdf = tmp_path / "eStmt_2026-09-21.pdf"
    pdf.write_bytes(_pdf(BOFA_STMT.splitlines()))
    line = bf.import_statement(conn, pdf)
    assert line == "Bofa Checking (Aug 22 – Sep 21): 8 transactions, 0 already there, 8 added"
    assert db.accounts(conn).set_index("id").loc[chk, "last4"] == "0522"     # digits saved: next time by digits
    assert bf.import_statement(conn, pdf).endswith("8 transactions, 8 already there, 0 added")
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (sav,)).fetchone()[0] == 0


def test_an_unclear_statement_is_left_for_you_not_guessed(conn, tmp_path):
    db.get_or_create_account(conn, "Checking A", "Bank of America", "checking")
    db.get_or_create_account(conn, "Checking B", "Bank of America", "checking")
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(_pdf(BOFA_STMT.splitlines()))
    assert "couldn't tell which dashboard account" in bf.import_statement(conn, pdf)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_login_comes_from_the_keychain_item(monkeypatch):
    def fake(cmd, **kw):
        if cmd[-1] == "-w":
            return subprocess.CompletedProcess(cmd, 0, "s3cret pass\n", "")
        return subprocess.CompletedProcess(cmd, 0, 'keychain: "x"\n    "acct"<blob>="jane.sample"\n', "")
    monkeypatch.setattr(bf.bank_pull.subprocess, "run", fake)
    assert bf.keychain_login() == ("jane.sample", "s3cret pass")
    monkeypatch.setattr(bf.bank_pull.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 44, "", "not found"))
    with pytest.raises(SystemExit, match="save it in the dashboard"):
        bf.keychain_login()


class FakeBox:
    def __init__(self, value=""):
        self.value, self.pressed = value, []

    def count(self): return 1
    @property
    def first(self): return self
    def input_value(self): return self.value
    def fill(self, v): self.value = v
    def press(self, key): self.pressed.append(key)


class Nothing:
    def count(self): return 0
    @property
    def first(self): return self
    def is_visible(self): return False


class FakeSignInPage:
    """BofA's sign-in page: a hidden copy of the user ID box plus the visible one, the user ID already saved."""
    def __init__(self):
        self.uid, self.pwd, self.signed_in, self.url = FakeBox("*****1234"), FakeBox(), False, None

    def goto(self, url): self.url = url
    def locator(self, sel):
        if sel == "#oid >> visible=true": return self.uid               # only the shown copy counts
        if sel == "input[type=password] >> visible=true": return self.pwd
        return Nothing()
    def get_by_text(self, pattern):
        page = self
        class T:
            first = property(lambda s: s)
            def is_visible(s): return page.signed_in and bool(pattern.search("Log Out"))
        return T()
    def wait_for_timeout(self, ms):
        if self.pwd.pressed: self.signed_in = True                       # Enter was pressed: BofA signs in
    def eval_on_selector_all(self, sel, js): return []


def test_sign_in_uses_the_visible_boxes_keeps_a_saved_user_id_and_presses_enter():
    page = FakeSignInPage()
    assert bf.sign_in(page, "jane.sample", "s3cret") is True
    assert page.url == bf.SIGN_IN
    assert page.uid.value == "*****1234"                                 # saved user ID left alone
    assert page.pwd.value == "s3cret" and page.pwd.pressed == ["Enter"]
