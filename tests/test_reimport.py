"""Importing the same file again must never double anything - for every supported format."""
import pytest

from finance import db
from finance.importers import apply, apply_statement, parse_file
from finance.importers.statements import parse_text
from tests.test_bofa import read
from tests.test_statements import CARD, CHECKING, ETRADE, MERRILL_CMA, MORTGAGE, WEALTHFRONT

CSV_FILES = [  # (fixture, file name the bank uses, account type)
    ("bofa_checking.csv", "stmt.csv", "checking"),
    ("bofa_credit_card.csv", "August2026_1013.csv", "credit_card"),
    ("chase_checking.csv", "Chase1234_Activity_20260926.CSV", "checking"),
    ("chase_card.csv", "Chase4321_Activity_20260926.CSV", "credit_card"),
    ("wealthfront_cash.csv", "Joint Cash Account - All-time.csv", "savings"),
    ("amex_activity.csv", "activity.csv", "credit_card"),
    ("barclays_card.csv", "CreditCard_20250101_20250925.csv", "credit_card"),
    ("apple_card.csv", "Apple Card Transactions Jan 01 2025 - Sep 26 2025.csv", "credit_card"),
    ("merrill_holdings.csv", "holdings.csv", "brokerage"),
]


def snapshot(conn):
    q = lambda sql: conn.execute(sql).fetchall()   # noqa: E731
    return (q("SELECT account_id, date, description, amount FROM transactions ORDER BY 1, 2, 3, 4"),
            q("SELECT account_id, date, balance FROM balances ORDER BY 1, 2"),
            q("SELECT account_id, as_of, symbol, value FROM holdings ORDER BY 1, 2, 3"),
            q("SELECT id, rate, payment FROM accounts ORDER BY 1"))


@pytest.mark.parametrize("fixture,filename,kind", CSV_FILES)
def test_csv_imported_twice_changes_nothing(tmp_path, fixture, filename, kind):
    conn = db.connect(tmp_path / "t.db")
    acct = db.add_account(conn, "Account", "Bank", kind)
    content = read(fixture)
    first = apply(conn, parse_file(content, filename=filename), acct, filename)
    after_first = snapshot(conn)
    second = apply(conn, parse_file(content, filename=filename), acct, filename)
    assert second["transactions_added"] == 0
    assert snapshot(conn) == after_first, f"{fixture}: second import changed the data"
    assert first["transactions_added"] == len(parse_file(content, filename=filename).transactions)


@pytest.mark.parametrize("text", [MORTGAGE, CHECKING, CARD, MERRILL_CMA, ETRADE, WEALTHFRONT],
                         ids=["mortgage", "bank", "card", "merrill", "etrade", "wealthfront"])
def test_statement_saved_three_times_changes_nothing(tmp_path, text):
    conn = db.connect(tmp_path / "t.db")
    s = parse_text(text)
    acct = db.add_account(conn, "Account", s.institution or "Bank", s.account_type or "checking")
    apply_statement(conn, s, acct, "statement.pdf")
    after_first = snapshot(conn)
    for _ in range(2):
        apply_statement(conn, parse_text(text), acct, "statement.pdf")
    assert snapshot(conn) == after_first
    # and net worth counts the account once, at its latest value - never a sum of imports
    assert db.accounts(conn)["balance"].iloc[0] == pytest.approx(abs(s.balance))
