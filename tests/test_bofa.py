from datetime import date

import pandas as pd
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


def test_net_worth_has_no_fake_jumps_when_accounts_start_late(conn):
    """A loan entered today, or a brokerage whose history starts later, must not read as $0 before."""
    chk = db.add_account(conn, "Checking", "Bank of America", "checking")
    brk = db.add_account(conn, "Brokerage", "E*TRADE", "brokerage")
    mtg = db.add_account(conn, "Mortgage", "Bank of America", "mortgage")
    db.update_account_terms(conn, mtg, 0.06, 3000.0)
    for m in ("2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30"):
        db.upsert_balance(conn, chk, m, 10_000)
    for m in ("2025-03-31", "2025-04-30"):
        db.upsert_balance(conn, brk, m, 500_000)                 # history starts in March
    db.upsert_balance(conn, mtg, "2025-04-30", 400_000)          # only a current balance
    nw = db.net_worth_series(conn)
    assert nw["date"].iloc[0] == pd.Timestamp("2025-03-31")      # starts when the brokerage has data
    mar = nw.iloc[0]
    # mortgage one month earlier, by reverse amortization: (B + payment) / (1 + r/12)
    assert mar["liabilities"] == pytest.approx((400_000 + 3000) / 1.005)
    assert mar["assets"] == pytest.approx(510_000)


def test_loan_without_terms_is_carried_back(conn):
    chk = db.add_account(conn, "Checking", "Bank of America", "checking")
    car = db.add_account(conn, "Car loan", "Bank of America", "auto_loan")
    for m in ("2025-01-31", "2025-02-28", "2025-03-31"):
        db.upsert_balance(conn, chk, m, 10_000)
    db.upsert_balance(conn, car, "2025-03-31", 20_000)
    nw = db.net_worth_series(conn)
    assert (nw.loc[nw["date"] <= "2025-03-31", "liabilities"] == 20_000).all()


# --- Chase --------------------------------------------------------------------------

def test_chase_checking():
    p = parse_file(read("chase_checking.csv"))
    assert (p.kind, p.institution) == ("deposit", "Chase")
    assert len(p.transactions) == 5 and p.transactions["fingerprint"].is_unique   # two identical Zelles kept
    bal = dict(zip(p.balances["date"], p.balances["balance"]))
    assert bal[date(2026, 9, 25)] == pytest.approx(5310.55)                      # newest row = day's close
    assert bal[date(2026, 9, 20)] == pytest.approx(2694.75)
    assert p.as_of == date(2026, 9, 25)


def test_chase_card():
    p = parse_file(read("chase_card.csv"))
    assert (p.kind, p.institution) == ("credit_card", "Chase")
    assert p.transactions["amount"].sum() == pytest.approx(1500 - 6.45 * 2 - 42.99 + 12.00)
    assert p.transactions["fingerprint"].is_unique


def test_chase_reimport_is_idempotent(conn):
    acct = db.add_account(conn, "Chase Checking", "Chase", "checking")
    parsed = parse_file(read("chase_checking.csv"))
    assert apply(conn, parsed, acct, "a.csv")["transactions_added"] == 5
    assert apply(conn, parsed, acct, "a.csv")["transactions_added"] == 0


def test_chase_card_payment_is_a_transfer_not_spending():
    from finance import insights
    t = parse_file(read("chase_card.csv")).transactions.assign(category=None)
    t["date"] = pd.to_datetime(t["date"])
    cf = insights.monthly_cash_flow(t)
    assert cf["money_in"].iloc[0] == pytest.approx(12.00)                        # the return, not the payment


def test_chase_checking_card_payment_is_a_transfer():
    from finance import insights
    assert insights.is_transfer("Payment to Chase card ending in 1234 09/22")
    assert insights.is_transfer("Payment Thank You-Mobile")
    assert insights.is_transfer("PAYMENT - THANK YOU")                       # BofA wording still works
    assert not insights.is_transfer("WHOLE FOODS #10234 SAN JOSE CA")


# --- which account does a CSV belong to? -----------------------------------------------

def _two_checking(conn):
    a = db.add_account(conn, "BofA Everyday", "Bank of America", "checking")
    b = db.add_account(conn, "BofA Bills", "Bank of America", "checking")
    return a, b


def test_csv_matched_by_overlapping_transactions(conn):
    from finance.importers import suggest_csv_account
    a, b = _two_checking(conn)
    parsed = parse_file(read("bofa_checking.csv"))
    apply(conn, parsed, b, "august.csv")                                   # an earlier download went to "Bills"
    name, why = suggest_csv_account(conn, parsed, "stmt.csv", db.accounts(conn))
    assert name == "BofA Bills" and "5 of these transactions" in why


def test_csv_ambiguous_is_left_to_the_user(conn):
    from finance.importers import suggest_csv_account
    _two_checking(conn)
    name, _ = suggest_csv_account(conn, parse_file(read("bofa_checking.csv")), "stmt.csv", db.accounts(conn))
    assert name is None                                                     # two candidates, no evidence


def test_chase_csv_matched_by_digits_in_file_name(conn):
    from finance.importers import last4_from_filename, suggest_csv_account
    assert last4_from_filename("Chase4321_Activity_20260926.CSV") == "4321"
    x = db.add_account(conn, "Chase Freedom", "Chase", "credit_card")
    y = db.add_account(conn, "Chase Sapphire", "Chase", "credit_card")
    db.update_account_details(conn, y, last4="4321")
    name, why = suggest_csv_account(conn, parse_file(read("chase_card.csv")), "Chase4321_Activity_20260926.CSV",
                                    db.accounts(conn))
    assert name == "Chase Sapphire" and "4321" in why
