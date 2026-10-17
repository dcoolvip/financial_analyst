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


class FakeBox:
    def __init__(self): self.clicked, self.pressed = False, []
    @property
    def first(self): return self
    def click(self): self.clicked = True
    def press(self, key): self.pressed.append(key)


class FakeBofA:
    """Home page with a Log In link; Chrome has filled the sign-in boxes; Enter signs you in."""
    def __init__(self): self.url, self.login, self.pwd, self.signed_in = None, FakeBox(), FakeBox(), False
    def goto(self, url): self.url = url
    def get_by_role(self, role, name=None):
        login = self.login
        class L:
            first = property(lambda s: login)
            def or_(s, other): return s
        return L()
    def locator(self, sel):
        assert sel == "input[type=password] >> visible=true"
        return self.pwd
    def get_by_text(self, pattern):
        page = self
        class T:
            first = property(lambda s: s)
            def is_visible(s): return page.signed_in and bool(pattern.search("Log Out"))
        return T()
    def wait_for_timeout(self, ms):
        if self.pwd.pressed: self.signed_in = True


def test_sign_in_is_log_in_then_enter_on_chromes_saved_password():
    page = FakeBofA()
    assert bf.sign_in(page) is True
    assert page.url == bf.HOME and page.login.clicked and page.pwd.clicked and page.pwd.pressed == ["Enter"]


def test_a_download_must_be_the_statement_on_the_tile():
    """Regression: every tile's download came back as the mortgage statement (first link on the page)."""
    from finance.importers.statements import Statement
    mortgage = Statement(kind="loan", institution="Bank of America", last4="6784")
    savings_tile = {"title": "September Statement", "label": "Advantage Savings - 9897"}
    card_tile = {"title": "September Statement", "label": "Customized Cash Rewards Visa Signature - 1013"}
    assert "ending 6784, not 9897" in bf.matches_tile(savings_tile, mortgage)
    assert bf.matches_tile({"title": "October Statement", "label": "Mortgage - 6784"}, mortgage) is None
    assert "not credit card" in bf.matches_tile({"title": "x", "label": "Customized Cash Rewards"}, mortgage)
    savings = Statement(kind="deposit", institution="Bank of America", last4="9897")
    assert bf.matches_tile(savings_tile, savings) is None
    assert bf.matches_tile({"title": "September Statement", "label": "DCOOLVIP"},
                           Statement(kind="deposit", last4="0522")) is None   # a nickname: nothing to contradict
    assert bf.matches_tile(card_tile, savings) is not None



def test_no_two_tiles_may_bring_down_the_same_document(tmp_path, monkeypatch):
    """The DCOOLVIP (nickname) tile's PDF was the mortgage too; with no digits to check, the duplicate is what gives
    it away - the Mortgage tile proves the document is its own, so the nickname tile's copy is rejected."""
    from finance.importers.statements import Statement
    mortgage = Statement(kind="loan", institution="Bank of America", last4="6784")
    mortgage.as_of = date(2026, 10, 5)
    monkeypatch.setattr(bf.statements, "parse_pdf", lambda b: mortgage)
    files = {}
    for name in ("2026-09-21_DCOOLVIP.pdf", "2026-10-05_Mortgage_6784.pdf"):
        files[name] = tmp_path / name
        files[name].write_bytes(b"%PDF")
    new = [("DCOOLVIP|2026-09-21", files["2026-09-21_DCOOLVIP.pdf"], {"title": "September Statement", "label": "DCOOLVIP"}),
           ("Mortgage - 6784|2026-10-05", files["2026-10-05_Mortgage_6784.pdf"],
            {"title": "October Statement", "label": "Mortgage - 6784"})]
    verdict = {info["label"]: why for _, _, info, why in bf.check_downloads(new)}
    assert verdict["Mortgage - 6784"] is None
    assert "same document as the Mortgage - #### one" in verdict["DCOOLVIP"]



def test_a_shortened_name_on_the_tile_still_finds_its_download_link():
    """Regression: the card tile reads 'Customized Cash Re...'; its link has the full name, so it was skipped."""
    card = {"title": "September Statement", "label": "Customized Cash Re... - 1013"}     # as on the tile
    link = "Download PDF\nfor September Statement for Customized Cash Rewards Visa Signature - 1013"
    assert bf.link_pattern(card).search(link)
    assert not bf.link_pattern(card).search("Download PDF for September Statement for Customized Cash Rewards - 9999")
    assert bf.link_pattern({"title": "September Statement", "label": "Customized Cash Re..."}).search(link)
    savings = {"title": "September Statement", "label": "Advantage Savings - 9897"}
    assert bf.link_pattern(savings).search("Download PDF for September Statement for Advantage Savings - 9897")
    assert not bf.link_pattern(savings).search("Download PDF for September Statement for DCOOLVIP")
    assert not bf.link_pattern({"title": "September Statement", "label": "Mortgage - 67"}).search(
        "Download PDF for September Statement for Mortgage - 6784")              # a full name must match fully
