"""E*TRADE's own API: request signing, approval, sandbox safety and syncing. Never calls E*TRADE."""
import json
import stat
import urllib.parse
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from finance import db, etrade


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    return tmp_path


def test_oauth_signature_matches_the_published_example(monkeypatch):
    """Twitter's worked OAuth 1.0a example (same HMAC-SHA1 scheme E*TRADE uses), checked to the byte."""
    monkeypatch.setattr(etrade.secrets, "token_hex", lambda n: "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg")
    monkeypatch.setattr(etrade.time, "time", lambda: 1318622958)
    keys = {"consumer_key": "xvz1evFS4wEEPTGEFPHBog", "consumer_secret": "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw"}
    h = etrade._auth_header("POST", "https://api.twitter.com/1.1/statuses/update.json",
                            {"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
                            keys, "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
                            "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE")
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in h


class FakeETrade:
    def __init__(self):
        self.calls = []
        self.accounts = [{"accountId": "84012204", "accountIdKey": "k_md", "accountDesc": "Brokerage",
                          "accountType": "INDIVIDUAL", "institutionType": "BROKERAGE"},
                         {"accountId": "55550000", "accountIdKey": "k_other", "accountDesc": "Stock Plan",
                          "accountType": "INDIVIDUAL", "institutionType": "BROKERAGE"}]

    def __call__(self, path, keys, token="", token_secret="", query=None, extra=None):
        self.calls.append((path, query, extra))
        if path == "/oauth/request_token":
            return b"oauth_token=req%2Btok&oauth_token_secret=reqsec&oauth_callback_confirmed=true"
        if path == "/oauth/access_token":
            assert extra == {"oauth_verifier": "AB12C"} and token == "req+tok"
            return b"oauth_token=acc&oauth_token_secret=accsec"
        if path in ("/oauth/renew_access_token", "/oauth/revoke_access_token"):
            return b"ok"
        if path == "/v1/accounts/list.json":
            return json.dumps({"AccountListResponse": {"Accounts": {"Account": self.accounts}}}).encode()
        if path.endswith("/balance.json"):
            assert query["instType"] == "BROKERAGE"
            return json.dumps({"BalanceResponse": {"Computed": {"RealTimeValues": {"totalAccountValue": 1234.5}}}}).encode()
        if path.endswith("/portfolio.json"):
            return json.dumps({"PortfolioResponse": {"AccountPortfolio": [{"totalPages": 1, "Position": [
                {"symbolDescription": "AAPL", "quantity": 4, "marketValue": 1000.0, "totalCost": 400.0,
                 "Quick": {"lastTrade": 250.0}, "Product": {"symbol": "AAPL"}}]}]}}).encode()
        raise AssertionError(path)


@pytest.fixture
def fake(monkeypatch):
    f = FakeETrade()
    monkeypatch.setattr(etrade, "_get", f)
    return f


def test_approval_flow_and_private_storage(data, fake):
    etrade.save_keys("ck", "cs", "live")
    url, tok, sec = etrade.start_approval(etrade.load())
    assert url == "https://us.etrade.com/e/t/etws/authorize?key=ck&token=req%2Btok"
    assert fake.calls[0][2] == {"oauth_callback": "oob"}
    c = etrade.finish_approval(" AB12C ", tok, sec, etrade.load())
    assert c["accounts"] == ["k_md", "k_other"] and etrade.is_current(c)
    assert etrade.load()["connections"][0]["token"] == "acc"
    assert stat.S_IMODE((data / "etrade.json").stat().st_mode) == 0o600
    etrade.finish_approval("AB12C", tok, sec, etrade.load())                  # same login approved again
    assert len(etrade.load()["connections"]) == 1                             # replaces, doesn't pile up


def test_access_ends_at_midnight_eastern(data, fake):
    yesterday = (datetime.now(etrade.EASTERN).date() - timedelta(days=1)).isoformat()
    with pytest.raises(etrade.NeedsApproval):
        etrade.accounts({"consumer_key": "ck", "consumer_secret": "cs"}, {"token": "t", "secret": "s",
                                                                          "approved": yesterday})
    assert fake.calls == []                                                    # doesn't even try


def test_sandbox_never_writes_to_the_database(data, fake):
    conn = db.connect(data / "finance.db")
    etrade.save_keys("ck", "cs", "sandbox")
    url, tok, sec = etrade.start_approval(etrade.load())
    c = etrade.finish_approval("AB12C", tok, sec, etrade.load())
    rows = etrade.sandbox_test(etrade.load(), c)
    assert [r["balance"] for r in rows] == [1234.5, 1234.5] and etrade.load_test()["accounts"] == rows
    with pytest.raises(etrade.ETradeError, match="sandbox"):
        etrade.sync(conn, etrade.load())
    assert conn.execute("SELECT COUNT(*) FROM balances").fetchone()[0] == 0


def test_live_sync_fills_the_matching_account_and_asks_about_the_rest(data, fake):
    conn = db.connect(data / "finance.db")
    md = db.get_or_create_account(conn, "MD eTrade Brokerage", "E*TRADE", "brokerage")
    ds = db.get_or_create_account(conn, "DS E*TRADE Brokerage", "E*TRADE", "brokerage")
    db.update_account_details(conn, md, last4="2204")
    db.update_account_details(conn, ds, last4="9203")
    etrade.save_keys("ck", "cs", "live")
    url, tok, sec = etrade.start_approval(etrade.load())
    etrade.finish_approval("AB12C", tok, sec, etrade.load())
    r = etrade.sync(conn, etrade.load())
    assert r["accounts"] == [{"account_id": md, "balance": 1234.5, "positions": 1}]   # 2204 -> MD, not DS
    assert [u["ends"] for u in r["unmatched"]] == ["0000"]
    today = date.today().isoformat()
    assert conn.execute("SELECT account_id, balance FROM balances WHERE date = ?", (today,)).fetchall() == [(md, 1234.5)]
    assert conn.execute("SELECT symbol, value, cost_basis FROM holdings WHERE account_id = ?", (md,)).fetchall() == \
        [("AAPL", 1000.0, 400.0)]
    assert "/oauth/renew_access_token" in [c[0] for c in fake.calls]        # wakes an idle token first
    etrade.link_account(conn, "k_other", None)                                # "don't import this one"
    assert etrade.sync(conn, etrade.load())["unmatched"] == []
    assert not [c for c in fake.calls if c[0].startswith("/v1") and "orders" in c[0]]   # reads only


def test_a_zero_balance_next_to_real_positions_uses_their_value(data, fake, monkeypatch):
    """Seen in E*TRADE's sandbox: totalAccountValue 0 for accounts holding 7 positions. Never record $0 then."""
    conn = db.connect(data / "finance.db")
    md = db.get_or_create_account(conn, "MD eTrade Brokerage", "E*TRADE", "brokerage")
    db.update_account_details(conn, md, last4="2204")
    monkeypatch.setattr(etrade, "balance", lambda *a, **k: 0.0)
    etrade.save_keys("ck", "cs", "live")
    url, tok, sec = etrade.start_approval(etrade.load())
    etrade.finish_approval("AB12C", tok, sec, etrade.load())
    assert etrade.sync(conn, etrade.load())["accounts"][0]["balance"] == 1000.0     # the positions' value


def test_a_slow_answer_is_retried_once(monkeypatch):
    import urllib.error
    tries = []

    class Ok:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b"fine"

    def slow_then_ok(req, timeout):
        tries.append(timeout)
        if len(tries) == 1:
            raise urllib.error.URLError(TimeoutError("timed out"))
        return Ok()
    monkeypatch.setattr(etrade.urllib.request, "urlopen", slow_then_ok)
    assert etrade._get("/v1/accounts/list.json", {"consumer_key": "k", "consumer_secret": "s", "env": "sandbox"}) == b"fine"
    assert tries == [20, 30]


def test_same_stock_in_several_lots_is_one_holding(data, fake, monkeypatch):
    """Regression: E*TRADE lists a stock once per lot (e.g. AAPL bought at different times). The holdings table
    keeps one row per symbol, so two AAPL rows crashed the sync right after the user matched the account."""
    conn = db.connect(data / "finance.db")
    md = db.get_or_create_account(conn, "MD eTrade Brokerage", "E*TRADE", "brokerage")
    db.update_account_details(conn, md, last4="2204")
    lot = lambda q, v, cost: {"symbolDescription": "AAPL", "quantity": q, "marketValue": v, "totalCost": cost,
                              "Quick": {"lastTrade": 250.0}, "Product": {"symbol": "AAPL"}}
    nameless = {"symbolDescription": None, "quantity": 1, "marketValue": 5.0, "Product": {}}

    class Lots(FakeETrade):
        def __call__(self, path, *a, **k):
            if path.endswith("/portfolio.json"):
                return json.dumps({"PortfolioResponse": {"AccountPortfolio": [{"totalPages": 1, "Position": [
                    lot(4, 1000.0, 400.0), lot(6, 1500.0, 900.0), nameless]}]}}).encode()
            return super().__call__(path, *a, **k)
    monkeypatch.setattr(etrade, "_get", Lots())
    etrade.save_keys("ck", "cs", "live")
    url, tok, sec = etrade.start_approval(etrade.load())
    etrade.finish_approval("AB12C", tok, sec, etrade.load())
    r = etrade.sync(conn, etrade.load())
    assert r["errors"] == []
    assert conn.execute("SELECT symbol, quantity, value, cost_basis FROM holdings WHERE account_id = ? ORDER BY symbol",
                        (md,)).fetchall() == [("AAPL", 10, 2500.0, 1300.0), ("Unnamed position", 1, 5.0, None)]


def test_empty_portfolio_reply_means_no_positions_and_never_saves_zero(data, fake, monkeypatch):
    """Regression: E*TRADE answered the 3882 portfolio request with an empty body, which crashed the JSON reader.
    Empty = nothing to list. And a $0 total with no positions is reported, never written over the real balance."""
    conn = db.connect(data / "finance.db")
    md = db.get_or_create_account(conn, "MD eTrade Brokerage", "E*TRADE", "brokerage")
    db.update_account_details(conn, md, last4="2204")
    db.upsert_balance(conn, md, date(2026, 7, 31), 2766780.13, "statement.pdf")

    class Empty(FakeETrade):
        def __call__(self, path, *a, **k):
            if path.endswith("/portfolio.json"):
                return b""
            if path.endswith("/balance.json"):
                return json.dumps({"BalanceResponse": {"Computed": {"RealTimeValues": {"totalAccountValue": 0}}}}).encode()
            return super().__call__(path, *a, **k)
    monkeypatch.setattr(etrade, "_get", Empty())
    etrade.save_keys("ck", "cs", "live")
    url, tok, sec = etrade.start_approval(etrade.load())
    etrade.finish_approval("AB12C", tok, sec, etrade.load())
    r = etrade.sync(conn, etrade.load())
    assert "nothing saved" in r["errors"][0] and "••2204" in r["errors"][0]
    assert conn.execute("SELECT date, balance FROM balances WHERE account_id = ?", (md,)).fetchall() == \
        [("2026-07-31", 2766780.13)]


def test_matched_to_the_wrong_account_never_overwrites_a_much_bigger_balance(data, fake, monkeypatch):
    """Regression: 3882 (a $0.70 account) was matched to MD eTrade Brokerage ($2.77M) and its $0.70 became the
    newest balance, wiping $2.77M off net worth. A sudden halving is now reported, not saved."""
    conn = db.connect(data / "finance.db")
    md = db.get_or_create_account(conn, "MD eTrade Brokerage", "E*TRADE", "brokerage")
    db.upsert_balance(conn, md, date(2026, 7, 31), 2766780.13, "statement.pdf")

    class Tiny(FakeETrade):
        def __call__(self, path, *a, **k):
            if path.endswith("/portfolio.json"):
                return b""
            if path.endswith("/balance.json"):
                return json.dumps({"BalanceResponse": {"Computed": {"RealTimeValues": {"totalAccountValue": 0.7}}}}).encode()
            return super().__call__(path, *a, **k)
    monkeypatch.setattr(etrade, "_get", Tiny())
    etrade.save_keys("ck", "cs", "live")
    url, tok, sec = etrade.start_approval(etrade.load())
    etrade.finish_approval("AB12C", tok, sec, etrade.load())
    etrade.link_account(conn, "k_md", md)                                     # the user's (wrong) pick
    r = etrade.sync(conn, etrade.load())
    assert any("$0.70" in e and "not saved" in e for e in r["errors"])
    assert conn.execute("SELECT date, balance FROM balances WHERE account_id = ?", (md,)).fetchall() == \
        [("2026-07-31", 2766780.13)]
    seen = json.loads(etrade.seen_path().read_text())["accounts"]
    assert [a["ends"] for a in seen] == ["2204", "0000"] and "balance" not in seen[0]    # names only, for checking
