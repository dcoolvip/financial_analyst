"""Plaid keys and the which-banks-can-connect check. Never calls Plaid: the HTTP call is replaced."""
import io
import json
import stat
import urllib.error

import pandas as pd

import pytest

from finance import plaid


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    return tmp_path


def test_keys_are_private_to_this_user_and_removable(data):
    assert plaid.load_keys() is None
    plaid.save_keys(" cid123 ", "sekret9876\n")
    assert plaid.load_keys() == {"client_id": "cid123", "secret": "sekret9876", "env": "production"}
    assert stat.S_IMODE((data / "plaid.json").stat().st_mode) == 0o600
    plaid.forget_keys()
    assert plaid.load_keys() is None and not (data / "plaid.json").exists()


def test_check_sorts_oauth_from_direct_and_skips_hand_tracked_values(data, monkeypatch):
    sent = []
    answers = {"Chase": [{"name": "Chase", "oauth": True, "products": ["transactions", "liabilities", "auth"]},
                         {"name": "Austin Bank - Personal", "oauth": False, "products": []}],      # fuzzy noise
               "Golden 1 Credit Union": [{"name": "Golden Plains Credit Union", "oauth": False, "products": []},
                                         {"name": "Golden 1 Credit Union", "oauth": False,
                                          "products": ["transactions"]}],
               "Nowhere Bank": []}

    def fake_post(endpoint, body, keys):
        sent.append((endpoint, body["query"], body["country_codes"]))
        return {"institutions": answers[body["query"]]}
    monkeypatch.setattr(plaid, "_post", fake_post)
    names = plaid.institutions_to_check(["Chase", "Golden 1 Credit Union", "Redfin estimate", "Physical",
                                         "Nowhere Bank", "Chase"])
    rows = plaid.check(names, {"client_id": "c", "secret": "s"})
    assert [q for _, q, _ in sent] == ["Chase", "Golden 1 Credit Union", "Nowhere Bank"]       # once each
    assert all(e == "/institutions/search" and c == ["US"] for e, _, c in sent)
    by = {r["yours"]: r for r in rows}
    assert by["Chase"]["oauth"] is True and by["Chase"]["products"] == ["transactions", "liabilities"]
    assert by["Golden 1 Credit Union"]["oauth"] is False
    assert [r["plaid_name"] for r in rows if r["plaid_name"]] == ["Chase", "Golden 1 Credit Union"]   # no lookalikes
    assert by["Nowhere Bank"]["plaid_name"] is None
    assert plaid.last_check()["rows"] == rows                                       # kept for the screen


def test_plaid_errors_come_back_readable(monkeypatch):
    def refuse(req, timeout):
        body = json.dumps({"error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret provided"})
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body.encode()))
    monkeypatch.setattr(plaid.urllib.request, "urlopen", refuse)
    with pytest.raises(plaid.PlaidError, match="invalid client_id or secret provided"):
        plaid.search("Chase", {"client_id": "c", "secret": "s"})


# --- linking and syncing, against a fake Plaid -------------------------------------------------------------------

from datetime import date, timedelta

from finance import db


class FakePlaid:
    """Answers the Plaid endpoints the dashboard uses, and records every call."""
    def __init__(self):
        self.calls, self.pages = [], []
        self.accounts = [
            {"account_id": "pa_card", "mask": "1974", "type": "credit", "subtype": "credit card", "name": "Card",
             "balances": {"current": 120.5}},
            {"account_id": "pa_cash", "mask": "0001", "type": "depository", "subtype": "savings", "name": "Cash",
             "balances": {"current": 5000.0}},
            {"account_id": "pa_brk", "mask": "2204", "type": "investment", "subtype": "brokerage", "name": "Brokerage",
             "balances": {"current": 900.0}},
            {"account_id": "pa_new", "mask": "7777", "type": "depository", "subtype": "checking", "name": "Mystery",
             "balances": {"current": 1.0}},
        ]
        self.holdings = {"holdings": [{"account_id": "pa_brk", "security_id": "s1", "quantity": 3,
                                       "institution_price": 300.0, "institution_value": 900.0}],
                         "securities": [{"security_id": "s1", "ticker_symbol": "AAPL", "name": "Apple Inc"}]}

    def __call__(self, endpoint, body, keys):
        self.calls.append((endpoint, body))
        if endpoint == "/link/token/create":
            return {"link_token": "link-tok"}
        if endpoint == "/item/public_token/exchange":
            return {"item_id": "item1", "access_token": "access-1"}
        if endpoint == "/accounts/get":
            return {"accounts": self.accounts}
        if endpoint == "/transactions/sync":
            page = self.pages.pop(0) if self.pages else {"added": [], "modified": [], "removed": []}
            return {**page, "next_cursor": f"cur{len(self.calls)}", "has_more": bool(self.pages)}
        if endpoint == "/investments/holdings/get":
            return self.holdings
        if endpoint == "/item/remove":
            return {}
        raise AssertionError(endpoint)


def txn(tid, acct, amount, on, name="SHOP", pending=False):
    return {"transaction_id": tid, "account_id": acct, "amount": amount, "date": on, "name": name,
            "pending": pending, "personal_finance_category": {"primary": "GENERAL_MERCHANDISE"}}


@pytest.fixture
def linked(data, monkeypatch):
    fake = FakePlaid()
    monkeypatch.setattr(plaid, "_post", fake)
    conn = db.connect(data / "finance.db")
    card = db.get_or_create_account(conn, "Old Navy Card", "Barclays", "credit_card")
    cash = db.get_or_create_account(conn, "Barclays Savings", "Barclays", "savings")
    brk = db.get_or_create_account(conn, "Barclays Invest", "Barclays", "brokerage")
    db.update_account_details(conn, card, last4="1974")
    db.update_account_details(conn, brk, last4="2204")
    old = pd.DataFrame([{"date": date(2026, 8, 1), "description": "GAP OUTLET", "amount": -81.43, "fingerprint": "csv1"}])
    db.insert_transactions(conn, card, old, "card.csv")                      # imported from a CSV earlier
    plaid.save_keys("cid", "sec")
    plaid.add_item("public-tok", "Barclays - Cards", plaid.load_keys())
    return conn, fake, {"card": card, "cash": cash, "brk": brk}


def test_link_token_asks_only_for_read_products(data, monkeypatch):
    fake = FakePlaid()
    monkeypatch.setattr(plaid, "_post", fake)
    assert plaid.link_token({"client_id": "c", "secret": "s"}) == "link-tok"
    body = fake.calls[0][1]
    assert body["products"] == ["transactions"] and body["optional_products"] == ["investments"]
    assert not {"auth", "transfer", "identity", "payment_initiation"} & set(body["products"] + body["optional_products"])


def test_linking_keeps_the_access_token_private_with_the_keys(linked, data):
    keys = plaid.load_keys()
    assert keys["client_id"] == "cid" and keys["items"][0]["access_token"] == "access-1"
    assert stat.S_IMODE((data / "plaid.json").stat().st_mode) == 0o600
    plaid.save_keys("cid2", "sec2")                                           # new keys don't drop linked banks
    assert plaid.load_keys()["items"][0]["item_id"] == "item1"


def test_sync_reads_balances_transactions_and_holdings_into_the_right_accounts(linked):
    conn, fake, ids = linked
    fake.pages = [{"added": [txn("t_old", "pa_card", 81.43, "2026-08-01", "GAP OUTLET US"),   # already in the CSV
                             txn("t1", "pa_card", 25.00, "2026-09-03"),                     # a purchase
                             txn("t2", "pa_card", -100.00, "2026-09-04", "PAYMENT"),         # a payment
                             txn("t3", "pa_card", 9.99, "2026-09-05", pending=True)],        # not final yet
                   "modified": [], "removed": []},
                  {"added": [txn("t4", "pa_cash", -50.0, "2026-09-06", "INTEREST")], "modified": [], "removed": []}]
    r = plaid.sync(conn, plaid.load_keys())
    rows = conn.execute("SELECT account_id, date, description, amount, source FROM transactions "
                        "WHERE source = 'Plaid' ORDER BY date").fetchall()
    assert rows == [(ids["card"], "2026-09-03", "SHOP", -25.0, "Plaid"),          # money out is negative here
                    (ids["card"], "2026-09-04", "PAYMENT", 100.0, "Plaid"),
                    (ids["cash"], "2026-09-06", "INTEREST", 50.0, "Plaid")]
    today = date.today().isoformat()
    bal = dict(conn.execute("SELECT account_id, balance FROM balances WHERE date = ?", (today,)).fetchall())
    assert bal == {ids["card"]: 120.5, ids["cash"]: 5000.0, ids["brk"]: 900.0}
    assert conn.execute("SELECT symbol, quantity, value FROM holdings WHERE account_id = ?",
                        (ids["brk"],)).fetchall() == [("AAPL", 3, 900.0)]
    assert [u["plaid_account_id"] for u in r["unmatched"]] == ["pa_new"]       # no account like it: ask
    assert plaid.load_keys()["items"][0]["cursor"].startswith("cur")         # next sync asks only for what's new
    first, second = [c[1]["cursor"] for c in fake.calls if c[0] == "/transactions/sync"]
    assert first == "" and second.startswith("cur")                            # page 2 continues from page 1


def test_sync_again_is_safe_and_follows_changes_and_removals(linked):
    conn, fake, ids = linked
    fake.pages = [{"added": [txn("t1", "pa_card", 25.00, "2026-09-03")], "modified": [], "removed": []}]
    plaid.sync(conn, plaid.load_keys())
    fake.pages = [{"added": [txn("t1", "pa_card", 25.00, "2026-09-03")],            # sent twice: still one row
                   "modified": [txn("t1", "pa_card", 30.00, "2026-09-03", "SHOP FINAL")], "removed": []}]
    plaid.sync(conn, plaid.load_keys())
    assert conn.execute("SELECT description, amount FROM transactions WHERE source = 'Plaid'").fetchall() == \
        [("SHOP FINAL", -30.0)]
    fake.pages = [{"added": [], "modified": [], "removed": [{"transaction_id": "t1"}]}]
    plaid.sync(conn, plaid.load_keys())
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'Plaid'").fetchone()[0] == 0


def test_matching_an_account_later_rereads_its_history(linked):
    conn, fake, ids = linked
    fake.pages = [{"added": [txn("n1", "pa_new", 5.0, "2026-09-10")], "modified": [], "removed": []}]
    plaid.sync(conn, plaid.load_keys())                                       # pa_new unmatched: skipped
    chk = db.get_or_create_account(conn, "Barclays Checking", "Barclays", "checking")
    plaid.link_account(conn, "pa_new", chk, plaid.load_keys())
    assert plaid.load_keys()["items"][0]["cursor"] == ""
    fake.pages = [{"added": [txn("n1", "pa_new", 5.0, "2026-09-10")], "modified": [], "removed": []}]
    plaid.sync(conn, plaid.load_keys())
    assert conn.execute("SELECT amount FROM transactions WHERE account_id = ?", (chk,)).fetchall() == [(-5.0,)]
    plaid.link_account(conn, "pa_new", None)                                   # "don't import this one"
    assert plaid.sync(conn, plaid.load_keys())["unmatched"] == []


def test_match_account_needs_same_bank_kind_and_digits():
    ours = pd.DataFrame([
        {"id": 1, "institution": "E*TRADE", "type": "brokerage", "last4": "2204"},
        {"id": 2, "institution": "E*TRADE", "type": "brokerage", "last4": "9203"},
        {"id": 3, "institution": "Wealthfront", "type": "savings", "last4": None},
        {"id": 4, "institution": "Wealthfront", "type": "brokerage", "last4": "1667"}])
    m = lambda a, inst: plaid.match_account(a, inst, ours)
    assert m({"type": "investment", "mask": "9203"}, "E*TRADE from Morgan Stanley") == 2
    assert m({"type": "investment", "mask": "5555"}, "E*TRADE from Morgan Stanley") is None   # two there: ask
    assert m({"type": "depository", "subtype": "savings", "mask": "0042"}, "Wealthfront") == 3   # only one, no digits yet
    assert m({"type": "depository", "subtype": "checking", "mask": "0042"}, "Wealthfront") is None  # not a savings
    assert m({"type": "investment", "mask": "2204"}, "Wealthfront") is None    # right digits, wrong bank


def test_unlink_ends_the_connection_at_plaid(linked):
    conn, fake, _ = linked
    plaid.remove_item("item1", plaid.load_keys())
    assert fake.calls[-1] == ("/item/remove", {"access_token": "access-1"})
    assert plaid.load_keys()["items"] == []
