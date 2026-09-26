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
    assert table_with(at, "balance")["balance"].iloc[0] == 5000        # Accounts table


def test_new_account_appears_in_every_selector(env):
    conn, _ = env
    db.upsert_balance(conn, db.add_account(conn, "Checking", "Bank of America", "checking"), "2026-09-01", 1000)
    at = app()
    [t for t in at.text_input if t.label == "Name" and not t.value][0].input("Savings")
    click(at, "Add account")
    for label in ("Account", "Account to remove"):
        assert all("Savings" in opts for opts in select_options(at, label)), label
    assert "Savings" in table_with(at, "balance")["name"].tolist()


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
    names = table_with(at, "balance")["name"].tolist()
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
