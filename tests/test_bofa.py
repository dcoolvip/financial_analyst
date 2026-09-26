from datetime import date

import pytest

from finance import db
from finance.importers import UnrecognizedFile, apply, bofa, parse_file
from finance.importers.base import to_money

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_to_money():
    assert to_money('"1,234.56"') == 1234.56
    assert to_money("$-12.00") == -12.0
    assert to_money("(12.00)") == -12.0
    assert to_money("") is None


def test_parse_checking():
    p = bofa.parse(read("bofa_checking.csv"))
    assert p.kind == "deposit"
    assert len(p.transactions) == 5          # beginning-balance row excluded
    assert p.transactions["fingerprint"].is_unique  # duplicate Zelle rows stay distinct
    assert p.as_of == date(2026, 8, 31)
    bal = dict(zip(p.balances["date"], p.balances["balance"]))
    assert bal[date(2026, 8, 3)] == 6600.00  # last running balance of the day
    assert bal[date(2026, 8, 31)] == 4854.50  # from summary block


def test_parse_credit_card():
    p = bofa.parse(read("bofa_credit_card.csv"))
    assert p.kind == "credit_card"
    assert p.transactions["amount"].sum() == pytest.approx(500 - 42.17 - 61.03)
    assert p.balances.empty                  # CC exports carry no balance


def test_parse_holdings():
    p = bofa.parse(read("merrill_holdings.csv"))
    assert p.kind == "holdings"
    assert p.as_of == date(2026, 9, 25)
    assert list(p.holdings["symbol"]) == ["VTI", "AAPL", "CASH"]  # total row dropped
    assert p.holdings["value"].sum() == pytest.approx(51000.33)


def test_unrecognized():
    with pytest.raises(UnrecognizedFile):
        parse_file(b"foo,bar\n1,2\n")


def test_reimport_is_idempotent(conn):
    acct = db.add_account(conn, "BofA Checking", "Bank of America", "checking")
    parsed = parse_file(read("bofa_checking.csv"))
    first = apply(conn, parsed, acct, "a.csv")
    second = apply(conn, parsed, acct, "a.csv")
    assert first["transactions_added"] == 5
    assert second["transactions_added"] == 0
    assert len(db.transactions(conn)) == 5


def test_net_worth(conn):
    chk = db.add_account(conn, "Checking", "Bank of America", "checking")
    inv = db.add_account(conn, "Merrill", "Bank of America", "brokerage")
    mtg = db.add_account(conn, "Mortgage", "Bank of America", "mortgage")
    apply(conn, parse_file(read("bofa_checking.csv")), chk, "c.csv")
    apply(conn, parse_file(read("merrill_holdings.csv")), inv, "m.csv")
    db.upsert_balance(conn, mtg, "2026-08-01", 300_000)
    last = db.net_worth_series(conn).iloc[-1]
    assert last["assets"] == pytest.approx(4854.50 + 51000.33)
    assert last["liabilities"] == pytest.approx(300_000)
    assert last["net_worth"] == pytest.approx(4854.50 + 51000.33 - 300_000)
