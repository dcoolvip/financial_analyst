"""Front-end refresh tests: drive the real app like a user and check that every change shows up
everywhere (other tabs, selectors, totals) in the same session - no manual refresh.

Runs against a throwaway data folder; never touches real data and never calls the AI.
"""
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from finance import db
from tests.test_statements import ETRADE, MORTGAGE, _pdf

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


class Upload:
    def __init__(self, name, data: bytes):
        self.name, self.file_id, self._b = name, f"{name}-{len(data)}", data

    def getvalue(self):
        return self._b


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINANCE_NO_AI", "1")
    uploads = {"files": [], "keys": []}

    def fake_uploader(*_a, key=None, **_k):
        uploads["keys"].append(key)
        return uploads["files"] if key == uploads.get("active", key) else []
    monkeypatch.setattr(st, "file_uploader", fake_uploader)
    conn = db.connect(tmp_path / "finance.db")
    return conn, uploads


def app() -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def hero(at) -> str:
    return next(m.value for m in at.markdown if 'class="hero"' in m.value).split(">")[1].split("<")[0]


def click(at, label):
    [b for b in at.button if b.label == label][0].click().run()
    assert not at.exception, [e.value for e in at.exception]


def select_options(at, label):
    return [s.options for s in at.selectbox if s.label == label]


def table_with(at, column) -> pd.DataFrame:
    return next(d.value for d in at.dataframe if column in d.value.columns)


def accounts_editors(at):
    """The Accounts tab's tables - one per group (Cash, Investments, ..., Loans)."""
    return [d for d in at.dataframe if "new_balance" in d.value.columns]


def accounts_table(at) -> pd.DataFrame:
    return pd.concat([d.value for d in accounts_editors(at)], ignore_index=True)


# --- accounts -------------------------------------------------------------------------

def test_balance_update_refreshes_overview_and_accounts(env):
    conn, _ = env
    a = db.add_account(conn, "Checking", "Bank of America", "checking")
    db.upsert_balance(conn, a, "2026-09-01", 1000)
    at = app()
    assert hero(at) == "$1,000"
    [n for n in at.number_input if n.label == "Balance ($)"][0].set_value(5000)
    click(at, "Save balance")
    assert hero(at) == "$5,000"                                        # Overview
    assert accounts_table(at)["balance"].iloc[0] == 5000                 # Accounts table


def test_new_account_appears_in_every_selector(env):
    conn, _ = env
    db.upsert_balance(conn, db.add_account(conn, "Checking", "Bank of America", "checking"), "2026-09-01", 1000)
    at = app()
    [t for t in at.text_input if t.label == "Name" and not t.value][0].input("Savings")
    click(at, "Add account")
    for label in ("Account", "Account to remove"):
        assert all("Savings" in opts for opts in select_options(at, label)), label
    assert "Savings" in accounts_table(at)["name"].tolist()


def test_accounts_table_is_editable_inline(env):
    conn, _ = env
    db.upsert_balance(conn, db.add_account(conn, "Checking", "Bank of America", "checking"), "2026-09-01", 1000)
    db.upsert_balance(conn, db.add_account(conn, "BofA Auto loan", "Bank of America", "auto_loan"), "2026-09-01", 50_000)
    at = app()
    assert not [b for b in at.button if b.label in ("Save changes", "Save loan details")]   # old forms gone
    accounts_table = next(d for d in at.dataframe if {"name", "balance", "rate_pct"} <= set(d.value.columns))
    assert accounts_table.proto.editing_mode != 0, "Accounts table should be editable (data editor)"
    config = str(accounts_table.proto.columns)
    assert "Account \\u270f" in config or "Account ✏️" in config        # editable columns are marked ✏️


def test_account_edits_diff():
    from finance.editing import account_edits
    before = pd.DataFrame({"id": [1, 2], "name": ["Checking", "BofA Auto loan"], "type": ["checking", "auto_loan"],
                           "Type": ["Checking", "Auto loan"], "rate_pct": [None, 7.0], "payment": [None, 500.0]})
    after = before.copy()
    after.loc[1, ["name", "Type", "rate_pct", "payment"]] = ["BofA Mortgage", "Mortgage", 5.25, None]
    edits = account_edits(before, after, {"Mortgage": "mortgage", "Checking": "checking"}, terms=True)
    assert edits == {2: {"name": "BofA Mortgage", "type": "mortgage", "rate": pytest.approx(0.0525), "payment": None}}
    assert account_edits(before, before.copy(), {}, terms=True) == {}          # nothing changed, nothing saved


def test_account_edits_are_all_or_nothing(env):
    conn, _ = env
    a = db.add_account(conn, "Checking", "Bank of America", "checking")
    b = db.add_account(conn, "Savings", "Bank of America", "savings")
    with pytest.raises(Exception):
        db.apply_account_edits(conn, {a: {"name": "Everyday"}, b: {"name": "Everyday"}})   # duplicate name
    assert sorted(db.accounts(conn)["name"]) == ["Checking", "Savings"]                     # neither applied


def test_table_edits_show_up_on_other_tabs(env):
    """What the table save does (apply_account_edits), then the page: Overview, Future, selectors."""
    conn, _ = env
    db.upsert_balance(conn, db.add_account(conn, "Checking", "Bank of America", "checking"), "2026-09-01", 100_000)
    loan = db.add_account(conn, "BofA Auto loan", "Bank of America", "auto_loan")
    db.upsert_balance(conn, loan, "2026-09-01", 400_000)
    at = app()
    db.apply_account_edits(conn, {loan: {"name": "BofA Mortgage", "type": "mortgage", "rate": 0.055, "payment": 2500.0}})
    at.run()
    loans = table_with(at, "Monthly payment")                                    # Future tab
    assert loans["Loan"].iloc[0] == "BofA Mortgage"
    assert loans["Rate"].iloc[0] == pytest.approx(5.5) and loans["Monthly payment"].iloc[0] == 2500
    assert all("BofA Mortgage" in o for o in select_options(at, "Account"))     # balance form


def test_data_version_changes_when_another_device_writes(env, tmp_path):
    """The live-update watcher compares this value; any write must change it."""
    import os, time
    conn, _ = env
    before = os.stat(tmp_path / "finance.db").st_mtime_ns
    time.sleep(0.01)
    db.add_account(conn, "From the phone", "Bank of America", "checking")
    assert os.stat(tmp_path / "finance.db").st_mtime_ns != before


def test_remove_account_disappears_everywhere(env):
    conn, _ = env
    for name in ("Checking", "Old savings"):
        db.upsert_balance(conn, db.add_account(conn, name, "Bank of America", "checking"), "2026-09-01", 1000)
    at = app()
    assert hero(at) == "$2,000"
    [s for s in at.selectbox if s.label == "Account to remove"][0].set_value("Old savings").run()
    [c for c in at.checkbox if c.label.startswith("Yes, delete")][0].check().run()
    click(at, "Remove")
    assert hero(at) == "$1,000"
    assert all("Old savings" not in o for o in select_options(at, "Account"))


# --- imports --------------------------------------------------------------------------

def test_csv_import_updates_money_tab_and_clears_uploader(env):
    _, uploads = env
    at = app()
    uploads["active"] = uploads["keys"][-1]
    uploads["files"] = [Upload("checking.csv", (FIXTURES / "bofa_checking.csv").read_bytes())]
    at.run()
    [s for s in at.selectbox if s.label == "Import into"][0].set_value("➕ New account…").run()
    [t for t in at.text_input if t.label == "Account name"][0].input("BofA Checking").run()
    click(at, "Import")
    assert hero(at) == "$4,854"                                         # Overview, from the file's balance
    assert any("**5** transactions" in c.value for c in at.caption)     # Money in & out tab
    assert uploads["keys"][-1] != uploads["active"]                     # uploader emptied
    assert any("added 5 transactions" in t.value for t in at.toast)


def test_statement_pdfs_update_overview_investments_and_clear_uploader(env):
    conn, uploads = env
    db.upsert_balance(conn, db.add_account(conn, "Checking", "Bank of America", "checking"), "2025-07-31", 10_000)
    at = app()
    uploads["active"] = uploads["keys"][-1]
    uploads["files"] = [Upload("etrade.pdf", _pdf(ETRADE.strip().splitlines())),
                        Upload("mortgage.pdf", _pdf(MORTGAGE.strip().splitlines()))]
    at.run()
    titles = [m.value for m in at.markdown if m.value in ("**Loans**", "**Investments**")]
    assert titles == ["**Loans**", "**Investments**"]                   # one table per kind
    click(at, "Save statements")
    names = accounts_table(at)["name"].tolist()
    assert len(names) == 3                                              # two new accounts created
    assert any(m.value == "#### Investments" for m in at.markdown)      # Overview investments section
    assert any(m.label == "Unvested RSUs" for m in at.metric)
    assert uploads["keys"][-1] != uploads["active"]                     # uploader emptied
    assert any("Saved 2 statement" in t.value for t in at.toast)


def test_statement_never_defaults_into_another_institutions_account(env):
    conn, uploads = env
    merrill = db.add_account(conn, "Merrill Brokerage", "Merrill", "brokerage")
    db.update_account_details(conn, merrill, last4="5678")
    db.upsert_balance(conn, merrill, "2025-07-31", 1_000_000)
    at = app()
    uploads["active"] = uploads["keys"][-1]
    uploads["files"] = [Upload("etrade.pdf", _pdf(ETRADE.strip().splitlines()))]
    at.run()
    click(at, "Save statements")
    accts = db.accounts(conn)
    assert len(accts) == 2                                              # E*TRADE got its own account
    assert accts.loc[accts["name"] == "Merrill Brokerage", "balance"].iloc[0] == 1_000_000   # Merrill untouched


def test_csv_preselects_the_right_account_or_asks(env):
    from finance.importers import apply, parse_file
    conn, uploads = env
    db.add_account(conn, "BofA Everyday", "Bank of America", "checking")
    bills = db.add_account(conn, "BofA Bills", "Bank of America", "checking")
    data = (FIXTURES / "bofa_checking.csv").read_bytes()
    at = app()
    uploads["active"] = uploads["keys"][-1]
    uploads["files"] = [Upload("stmt.csv", data)]
    at.run()
    box = [s for s in at.selectbox if s.label == "Import into"][0]
    assert box.value is None                                                # can't tell yet: must choose
    assert [b for b in at.button if b.label == "Import"][0].disabled
    apply(conn, parse_file(data), bills, "earlier.csv")                     # now "Bills" has these transactions
    at.run()
    assert [s for s in at.selectbox if s.label == "Import into"][0].value == "BofA Bills"
    assert any("Matched automatically" in c.value for c in at.caption)


def test_wrong_import_can_be_undone_from_the_ui(env):
    conn, uploads = env
    bofa = db.add_account(conn, "BofA Checking", "Bank of America", "checking")
    at = app()
    assert not any(e.label == "↩️ Undo an import" for e in at.expander)          # nothing to undo yet
    uploads["active"] = uploads["keys"][-1]
    uploads["files"] = [Upload("chase.csv", (FIXTURES / "chase_checking.csv").read_bytes())]
    at.run()
    [s for s in at.selectbox if s.label == "Import into"][0].set_value("BofA Checking").run()   # the mistake
    click(at, "Import")
    assert len(db.transactions(conn)) == 5
    assert at.selectbox(key="cp_pick").value is not None                         # a checkpoint was taken
    assert "Before importing chase.csv into BofA Checking" in at.selectbox(key="cp_pick").format_func(
        at.selectbox(key="cp_pick").value)
    [c for c in at.checkbox if c.label == "Yes, put my data back to this point"][0].check().run()
    click(at, "Restore")
    assert len(db.transactions(conn)) == 0                                      # back to before the import
    assert any("Restored" in t.value for t in at.toast)


def test_trade_confirmation_shows_explanation_not_a_table(env):
    _, uploads = env
    at = app()
    uploads["active"] = uploads["keys"][-1]
    uploads["files"] = [Upload("CONFIRM_2026-09-22.pdf", _pdf(["Trade Confirmation", "Date: 9/22/2026",
                                                               "Wealthfront Brokerage LLC", "Buy 7.77 $1.0000 $7.77"]))]
    at.run()
    assert any("trade confirmation" in i.value for i in at.info)
    assert not [m for m in at.markdown if m.value == "**Not recognized**"]
    assert not [b for b in at.button if b.label == "Save statements"]           # nothing to save


def test_statement_is_reread_after_the_reader_changes(env, monkeypatch):
    """Regression: PDF results were cached by file contents only, so after a reader fix the same file
    kept returning the old (wrong) value - the Wealthfront $0.16."""
    import os
    from finance.importers import statements
    from tests.test_statements import WEALTHFRONT
    _, uploads = env
    pdf = Upload("STATEMENT_2025-08.pdf", _pdf(WEALTHFRONT.strip().splitlines()))
    real = statements.parse_pdf_all

    def old_reader(data):                                    # what the buggy reader produced
        out = real(data)
        for s in out:
            s.balance, s.holdings, s.history = 0.16, [], []
        return out
    at = app()                                               # first load reloads modules; patch after it
    monkeypatch.setattr(statements, "parse_pdf_all", old_reader)
    uploads["active"] = uploads["keys"][-1]
    uploads["files"] = [pdf]
    at.run()
    value = lambda: next(d.value for d in at.dataframe if "balance" in d.value.columns and "extras" in d.value.columns)
    assert value()["balance"].iloc[0] == pytest.approx(0.16)

    monkeypatch.setattr(statements, "parse_pdf_all", real)   # the fix ships: reader code changes on disk
    st_ = os.stat(statements.__file__)
    os.utime(statements.__file__, (st_.st_atime, st_.st_mtime + 5))
    try:
        at.run()
        assert value()["balance"].iloc[0] == pytest.approx(10_550)   # re-read, not served from the cache
    finally:
        os.utime(statements.__file__, (st_.st_atime, st_.st_mtime))


def test_cards_without_balance_are_flagged_and_signs_are_intuitive(env):
    conn, _ = env
    db.upsert_balance(conn, db.add_account(conn, "Checking", "Bank of America", "checking"), "2026-09-01", 10_000)
    card = db.add_account(conn, "Chase Sapphire", "Chase", "credit_card")
    at = app()
    assert any("have no balance yet" in m.value and "Chase Sapphire" in m.value for m in at.markdown)   # Overview
    accounts = accounts_table(at)
    assert accounts.loc[accounts["name"] == "Chase Sapphire", "status"].iloc[0] == "⚠️ needs a balance"

    at.selectbox(key=[s.key for s in at.selectbox if s.label == "Account"][0]).set_value("Chase Sapphire")
    [n for n in at.number_input if n.label == "Balance ($)"][0].set_value(1200.0)           # owe $1,200, as Chase shows it
    click(at, "Save balance")
    assert hero(at) == "$8,800"                                                             # 10,000 - 1,200
    accounts = accounts_table(at)
    assert accounts.loc[accounts["name"] == "Chase Sapphire", "shown_balance"].iloc[0] == 1200    # shown as owed
    assert db.accounts(conn).set_index("name").loc["Chase Sapphire", "balance"] == 1200
    assert not any("have no balance yet" in m.value for m in at.markdown)


def test_overpaid_card_counts_as_money_you_have(env):
    conn, _ = env
    db.upsert_balance(conn, db.add_account(conn, "Checking", "Bank of America", "checking"), "2026-09-01", 10_000)
    db.add_account(conn, "Chase Sapphire", "Chase", "credit_card")
    at = app()
    at.selectbox(key=[s.key for s in at.selectbox if s.label == "Account"][0]).set_value("Chase Sapphire")
    [n for n in at.number_input if n.label == "Balance ($)"][0].set_value(-50.0)            # a $50 credit
    click(at, "Save balance")
    assert hero(at) == "$10,050"


def test_balance_cell_edit_saves_as_today():
    from finance.editing import balance_edits, to_display, to_stored
    assert to_display(1200.0, True) == 1200 and to_display(500.0, False) == 500 and to_display(None, True) is None
    assert to_stored(1200.0, True) == 1200 and to_stored(-50.0, True) == -50                 # as banks show it
    before = pd.DataFrame({"id": [1, 2], "shown_balance": [None, 500.0], "is_liability": [True, False],
                           "new_balance": [None, None]}).astype({"new_balance": "float64"})
    after = before.copy()
    after.loc[0, "new_balance"] = 1234.56
    assert balance_edits(before, after) == {1: pytest.approx(1234.56)}


def test_balances_colored_and_hand_edits_recorded(env):
    conn, _ = env
    db.upsert_balance(conn, db.add_account(conn, "Checking", "Bank of America", "checking"), "2026-09-01", 10_000)
    db.upsert_balance(conn, db.add_account(conn, "Mortgage", "Golden 1 Credit Union", "mortgage"), "2026-09-01", 400_000)
    card = db.add_account(conn, "Chase Sapphire", "Chase", "credit_card")
    at = app()
    at.selectbox(key=[s.key for s in at.selectbox if s.label == "Account"][0]).set_value("Chase Sapphire")
    [n for n in at.number_input if n.label == "Balance ($)"][0].set_value(6337.08)          # as Chase shows it
    click(at, "Save balance")
    assert db.accounts(conn).set_index("name").loc["Chase Sapphire", "balance"] == pytest.approx(6337.08)
    styles = "".join(str(d.proto) for d in accounts_editors(at))
    assert "d03b3b" in styles and "006300" in styles                   # debts red, what you have green
    accounts = accounts_editors(at)[0]
    # Streamlit only draws Styler colors on NON-editable columns - the colored Balance column must be read-only
    import json
    config = json.loads(accounts.proto.columns)
    assert config["shown_balance"].get("disabled") is True, "Balance must be read-only or its colors are not drawn"
    assert not config["new_balance"].get("disabled")                    # editing happens in New balance
    log = db.recent_edits(conn)
    assert log.iloc[0]["account"] == "Chase Sapphire" and log.iloc[0]["new"] == "6337.08"
    assert any(e.label == "🕘 Recent changes" for e in at.expander)


def test_multi_account_pdf_never_piles_into_one_existing_account(env):
    """Three Robinhood accounts + an existing manual 'Robinhood Trading' (no account number): each must get
    its own new account, not all three saved into the manual one."""
    from tests.test_statements import ROBINHOOD_PAGE
    conn, uploads = env
    manual = db.add_account(conn, "Robinhood Trading", "Robinhood", "brokerage")
    db.upsert_balance(conn, manual, "2025-08-31", 5_000)
    pages = "".join(ROBINHOOD_PAGE.format(page=i, label="Individual", acct=a, open="1,000.00", close="1,400.00",
                                          spy_pct=100, extra="", cash="0.00")
                    for i, a in ((1, "111114688"), (4, "222222941"), (7, "333334782")))
    at = app()
    uploads["active"] = uploads["keys"][-1]
    uploads["files"] = [Upload("robinhood.pdf", _pdf(pages.splitlines()))]
    at.run()
    table = next(d.value for d in at.dataframe if "extras" in d.value.columns)
    assert len(table) == 3 and set(table["account"]) == {"➕ New account"}           # one row per account
    click(at, "Save statements")
    accts = db.accounts(conn).set_index("name")
    assert accts.loc["Robinhood Trading", "balance"] == 5_000                          # manual one untouched
    assert len(accts) == 4                                                              # 3 new + the manual one
    assert set(accts["last4"].dropna()) == {"4688", "2941", "4782"}


def test_csv_from_a_new_bank_defaults_to_new_account(env):
    conn, uploads = env
    db.add_account(conn, "Chase Sapphire", "Chase", "credit_card")                      # a card, but not Amex
    at = app()
    uploads["active"] = uploads["keys"][-1]
    uploads["files"] = [Upload("activity.csv", (FIXTURES / "amex_activity.csv").read_bytes())]
    at.run()
    assert [s for s in at.selectbox if s.label == "Import into"][0].value == "➕ New account…"


def test_valuables_in_accounts_and_future(env, monkeypatch):
    monkeypatch.setenv("POKEMON_DB", "/nonexistent")                       # tests never read the real dashboard
    conn, _ = env
    db.upsert_balance(conn, db.add_account(conn, "Checking", "Bank of America", "checking"), "2026-09-01", 10_000)
    house = db.add_account(conn, "House", "Manual", "property")
    gold = db.add_account(conn, "Gold coins", "Manual", "precious_metal")
    cards = db.add_account(conn, "Pokemon collection", "Pokemon dashboard", "collectible")
    db.upsert_balance(conn, house, "2026-09-01", 1_000_000)
    db.upsert_balance(conn, gold, "2026-09-01", 10_000)
    db.upsert_balance(conn, cards, "2025-07-31", 16_000)
    db.upsert_balance(conn, cards, "2026-07-31", 20_000)
    at = app()
    accounts = accounts_table(at)
    assert accounts.set_index("name").loc["Pokemon collection", "trend"] == "+25.0%/yr"    # its own history
    assert any("Property & valuables" == g for g in accounts["Group"])
    future = table_with(at, "Growth / yr").set_index("Asset")
    assert future.loc["Gold coins", "Growth / yr"] == pytest.approx(4.0)                    # long-run: no history
    # 1 year of history at +25%/yr earns 1/(1+5) weight; the rest is the long-run +3% for collectibles
    assert future.loc["Pokemon collection", "Growth / yr"] == pytest.approx((25 + 5 * 3) / 6, abs=0.1)
    assert future.loc["Pokemon collection", "Based on"].startswith("your history +25.0%/yr")
    db.apply_account_edits(conn, {cards: {"rate": 0.10}})                                  # set your own rate
    at.run()
    future = table_with(at, "Growth / yr").set_index("Asset")
    assert future.loc["Pokemon collection", "Growth / yr"] == pytest.approx(10.0)
    assert future.loc["Pokemon collection", "Based on"] == "your rate"
    assert any(s.label == "See an asset's history and outlook" for s in at.selectbox)


def test_accounts_in_sections_with_plain_column_names_and_details(env, monkeypatch):
    monkeypatch.setenv("POKEMON_DB", "/nonexistent")
    import json
    conn, _ = env
    chk = db.add_account(conn, "Checking", "Bank of America", "checking")
    car = db.add_account(conn, "Civic", "Honda", "vehicle")
    loan = db.add_account(conn, "Car loan", "Hyundai Motor Finance", "auto_loan")
    db.update_account_terms(conn, loan, 0.06, 500.0)
    db.update_account_details(conn, loan, last4="6365")
    today = pd.Timestamp.today().normalize()
    for m in range(14, -1, -1):
        d = (today - pd.DateOffset(months=m)).date()
        db.upsert_balance(conn, chk, d, 10_000 + 100 * (14 - m))
        db.upsert_balance(conn, loan, d, 20_000 - 400 * (14 - m), "statement.pdf")
        db.add_balance_if_missing(conn, car, d, 30_000 - 300 * (14 - m), "Estimated (depreciation from purchase price)")
    db.upsert_balance(conn, car, today.date(), 25_000)                     # the value you typed today
    at = app()
    heads = [m.value for m in at.markdown]
    assert any(h.startswith("### What you own") for h in heads) and any(h.startswith("### What you owe") for h in heads)
    editors = accounts_editors(at)
    assert len(editors) == 3                                               # Cash, Property & valuables, Loans
    headers = {v.get("label") for e in editors for v in json.loads(e.proto.columns).values()}
    assert "New balance ✏️" not in headers
    assert {"Type new balance ✏️", "Type new value ✏️", "Type amount owed ✏️", "Future uses", "Paid off by",
            "Monthly payment ✏️", "Interest % ✏️", "Past year"} <= headers
    loans = accounts_table(at).set_index("name")
    assert loans.loc["Car loan", "bank"] == "Hyundai Motor Finance ···6365"
    assert loans.loc["Car loan", "year"] == pytest.approx(400 * 12, abs=400)   # paid down = + (month-end grid)
    assert loans.loc["Car loan", "payoff"]

    # details: every value on record, where it came from
    at.selectbox(key="acct_detail").set_value("Civic").run()
    values = next(d.value for d in at.dataframe if "how" in d.value.columns)
    assert len(values) == 15
    assert values.sort_values("date")["how"].iloc[-1] == "Typed in"
    assert values["how"].str.startswith("Estimated").sum() == 14
    assert any(m.label == "Values on record" and m.value == "15 (14 est.)" for m in at.metric)
    assert any("Future tab:" in m.value for m in at.markdown)
    at.selectbox(key="acct_detail").set_value("Car loan").run()
    assert any("payments left" in m.value for m in at.markdown)
    assert next(d.value for d in at.dataframe if "how" in d.value.columns)["how"].iloc[0] == "Imported from statement.pdf"

    # Overview: what moved net worth this past year
    assert any(m.value.startswith("#### What moved your net worth") for m in at.markdown)
    movers = next(d.value for d in at.dataframe if "then" in d.value.columns)
    assert set(movers["name"]) == {"Checking", "Civic", "Car loan"}


def test_net_worth_range_and_own_owe_view(env):
    conn, _ = env
    a = db.add_account(conn, "Checking", "Bank of America", "checking")
    for d in pd.date_range(end=pd.Timestamp.today(), periods=60, freq="ME"):
        db.upsert_balance(conn, a, d.date(), 1000)
    at = app()
    at.segmented_control(key="nw_range").set_value("1Y").run()
    at.segmented_control(key="nw_view").set_value("Own & owe").run()
    assert not at.exception
    spec = at.get("plotly_chart")[0].proto.spec
    assert '"What you own"' in spec and '"What you owe"' in spec
