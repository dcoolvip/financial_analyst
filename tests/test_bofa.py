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


def test_net_worth_keeps_history_and_backfills_loans(conn):
    """A brokerage whose history starts later is marked as added (not a gain); loans are extended back."""
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
    assert nw["date"].iloc[0] == pd.Timestamp("2025-01-31")      # all history kept
    mar = nw[nw["date"] == "2025-03-31"].iloc[0]
    assert mar["added"] == "Brokerage"                           # the step is explained on the chart
    assert mar["liabilities"] == pytest.approx((400_000 + 3000) / 1.005)   # reverse amortization
    jan = nw.iloc[0]
    assert jan["liabilities"] > mar["liabilities"]               # loan was bigger earlier, never $0


def test_big_account_added_today_does_not_wipe_history_or_count_as_gain(conn):
    """Regression: a $3M account with only today's balance collapsed the Overview history to one point."""
    chk = db.add_account(conn, "Checking", "Bank of America", "checking")
    for i, m in enumerate(pd.date_range(end=pd.Timestamp.today(), periods=14, freq="ME")):
        db.upsert_balance(conn, chk, m.date(), 10_000 + 1000 * i)
    new = db.add_account(conn, "DS eTrade Brokerage", "E*TRADE", "brokerage")
    db.upsert_balance(conn, new, date.today(), 3_000_000)
    assert len(db.net_worth_series(conn)) > 12
    change, base, left_out = db.net_worth_change(conn, 12)
    assert change == pytest.approx(12_000, abs=1_001)            # checking's growth only, not +$3M
    assert left_out == ["DS eTrade Brokerage"]


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


# --- Wealthfront ------------------------------------------------------------------------

def test_wealthfront_all_time_builds_balance_history():
    p = parse_file(read("wealthfront_cash.csv"), filename="Joint Cash Account - All-time.csv")
    assert (p.kind, p.institution, p.note) == ("deposit", "Wealthfront", "")
    bal = dict(zip(p.balances["date"], p.balances["balance"]))
    assert bal[date(2026, 7, 31)] == 5000 and bal[date(2026, 8, 15)] == 4535
    assert bal[date(2026, 9, 25)] == pytest.approx(5000 + 35 - 500 + 40 - 1000)
    assert p.transactions["fingerprint"].is_unique


def test_wealthfront_partial_range_imports_transactions_only():
    p = parse_file(read("wealthfront_cash.csv"), filename="Joint Cash Account - 2026.csv")
    assert len(p.transactions) == 5 and p.balances.empty and "All-time" in p.note


def test_wealthfront_categories():
    from finance import insights
    p = parse_file(read("wealthfront_cash.csv"), filename="x - All-time.csv")
    cats = {d: insights.categorize(d, a) for d, a in zip(p.transactions["description"], p.transactions["amount"])}
    assert cats["August interest"] == "Income"
    assert cats["Transfer to Joint Investment Account"] == "Transfer"
    assert cats["Bank of America (Account ****1111)"] == "Transfer"
    assert insights.categorize("INTEREST CHARGE ON PURCHASES", -12.0) != "Income"      # card interest isn't income


def test_trade_confirmation_pdf_is_explained():
    from tests.test_statements import _pdf
    from finance.importers.statements import parse_pdf
    s = parse_pdf(_pdf(["Trade Confirmation", "Date: 9/22/2026", "Wealthfront Brokerage LLC",
                        "9/22/2026 9/22/2026 Buy 7.77 $1.0000 $7.77 -- $7.77"]))
    assert s.kind == "unknown" and "trade confirmation" in s.notes[0]


# --- American Express ---------------------------------------------------------------------

def test_amex_csv():
    from finance import insights
    p = parse_file(read("amex_activity.csv"), filename="activity.csv")
    assert (p.kind, p.institution, p.account_hint) == ("credit_card", "American Express", "1111")   # the paying card
    assert len(p.transactions) == 5 and p.transactions["fingerprint"].is_unique      # identical Whole Foods kept twice
    amounts = dict(zip(p.transactions["fingerprint"], p.transactions["amount"]))
    assert amounts["320000000000000001"] == -19.99                                    # purchase -> money out
    assert amounts["320000000000000004"] == 500.00 and amounts["320000000000000005"] == 12.00
    pay = p.transactions.loc[p.transactions["fingerprint"] == "320000000000000004", "description"].iloc[0]
    assert insights.categorize(pay, 500.0) == "Transfer"                              # paying the card isn't income
    assert "(John Sample)" in p.transactions.iloc[1]["description"] and "2 cards" in p.note


def test_amex_reimport_and_account_matching(conn):
    from finance.importers import suggest_csv_account
    other = db.add_account(conn, "Amex Blue", "American Express", "credit_card")
    gold = db.add_account(conn, "Amex Gold", "American Express", "credit_card")
    db.update_account_details(conn, gold, last4="1111")
    parsed = parse_file(read("amex_activity.csv"), filename="activity.csv")
    assert suggest_csv_account(conn, parsed, "activity.csv", db.accounts(conn))[0] == "Amex Gold"   # by card digits
    assert apply(conn, parsed, gold, "activity.csv")["transactions_added"] == 5
    assert apply(conn, parsed, gold, "activity.csv")["transactions_added"] == 0


# --- Barclays ----------------------------------------------------------------------------

def test_barclays_csv_includes_balance():
    from finance import insights
    p = parse_file(read("barclays_card.csv"), filename="CreditCard_20250101_20250925.csv")
    assert (p.kind, p.institution, p.account_hint) == ("credit_card", "Barclays", "4321")
    assert p.balances.values.tolist() == [[date(2025, 9, 26), 245.10]]            # owed, from the header
    assert len(p.transactions) == 4 and p.transactions["fingerprint"].is_unique   # identical GAP buys kept twice
    assert p.transactions["amount"].sum() == pytest.approx(100 - 45.10 * 2 - 200)
    assert insights.categorize("Payment Received", 100.0) == "Transfer"
    assert insights.categorize("GAP OUTLET US 2814", -45.10) == "Shopping"


def test_barclays_import_sets_card_balance(conn):
    card = db.add_account(conn, "Barclays Card", "Barclays", "credit_card")
    apply(conn, parse_file(read("barclays_card.csv")), card, "barclays.csv")
    a = db.accounts(conn).set_index("name").loc["Barclays Card"]
    assert a["balance"] == pytest.approx(245.10) and a["as_of"] == "2025-09-26"


# --- Apple Card --------------------------------------------------------------------------

def test_apple_card_csv():
    from finance import insights
    p = parse_file(read("apple_card.csv"), filename="Apple Card Transactions Jan 01 2025 - Sep 26 2025.csv")
    assert (p.kind, p.institution) == ("credit_card", "Apple Card") and p.balances.empty
    t = p.transactions
    assert len(t) == 6 and t["fingerprint"].is_unique                          # the two $227 charges both kept
    assert t["amount"].tolist() == [-227.0, -227.0, -2.99, 400.0, 12.0, -0.12]   # signs flipped to bank style
    assert t["description"].iloc[2] == "Hulu (John Sample)"                     # clean merchant + who
    assert insights.categorize(t["description"].iloc[3], 400.0) == "Transfer"   # the payment
    assert insights.categorize("APPLECARD GSBANK DES:PAYMENT ID:999", -400.0) == "Transfer"   # bank side
    assert "Shared card" in p.note


def test_apple_card_reimport_is_idempotent(conn):
    card = db.add_account(conn, "Apple Card", "Apple Card", "credit_card")
    parsed = parse_file(read("apple_card.csv"))
    assert apply(conn, parsed, card, "a.csv")["transactions_added"] == 6
    assert apply(conn, parsed, card, "a.csv")["transactions_added"] == 0


def test_mortgage_not_extended_back_before_the_home(conn):
    """Regression: a car's history from 2019 stretched the chart back, the mortgages were extended with it
    but the homes (recorded from Sep 2021) weren't - net worth showed -$2.3M before the homes appeared."""
    car = db.add_account(conn, "Civic", "Honda", "vehicle")
    home = db.add_account(conn, "Home", "Redfin", "property")
    mtg = db.add_account(conn, "Mortgage", "Golden 1", "mortgage")
    for m in pd.date_range("2019-11-30", "2021-12-31", freq="ME"):
        db.upsert_balance(conn, car, m.date(), 20_000)
    for m in ("2021-09-30", "2021-10-31", "2021-11-30", "2021-12-31"):
        db.upsert_balance(conn, home, m, 3_000_000)
    db.upsert_balance(conn, mtg, "2021-12-31", 2_000_000)
    nw = db.net_worth_series(conn)
    before = nw[nw["date"] < "2021-09-30"]
    assert len(before) and (before["liabilities"] == 0).all() and (before["net_worth"] == 20_000).all()
    sep = nw[nw["date"] == "2021-09-30"].iloc[0]
    assert sep["liabilities"] == 2_000_000 and sep["added"] == "Home"


def test_net_worth_chart_shows_one_tooltip_per_point():
    from finance import charts
    nw = pd.DataFrame({"date": pd.to_datetime(["2021-08-31", "2021-09-30"]), "assets": [1.0, 3e6],
                       "liabilities": [0.0, 2e6], "net_worth": [1.0, 1e6], "added": ["", "Home"]})
    fig = charts.net_worth_history(nw, "light")
    assert [t.hoverinfo for t in fig.data[1:]] == ["skip"]            # the "added" marker has no tooltip
    assert list(fig.data[0].customdata[:, 2]) == ["", "<br>Added: Home"]   # the line's tooltip says it
