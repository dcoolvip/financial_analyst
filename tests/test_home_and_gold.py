"""Home values from Redfin pages and gold at today's price - the decisions, without a browser or the internet."""
import importlib.util
import json
from datetime import date
from pathlib import Path

from finance import db, gold

spec = importlib.util.spec_from_file_location("redfin_fetch", Path(__file__).parent.parent / "tools/redfin_fetch.py")
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)


def test_redfin_estimate_is_read_from_the_page_either_way_round():
    assert rf.estimate("3 beds\n$1,596,911\nRedfin Estimate\n2 baths") == 1_596_911
    assert rf.estimate("Redfin Estimate for 1 Main St\n$4,191,090\nEdit home facts") == 4_191_090
    assert rf.estimate("nothing here", '{"avm":{"predictedValue":1600123.5}}') == 1_600_123.5
    assert rf.estimate("List price $999") is None


def test_redfin_value_saved_only_when_believable(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    home = db.get_or_create_account(conn, "Home", "Redfin estimate", "property")
    db.upsert_balance(conn, home, "2026-09-26", 1_600_000, "Redfin estimate history")
    assert "couldn't trust" in rf.save(conn, home, 160_000, date(2026, 10, 28))      # page changed: misread
    assert "couldn't find" in rf.save(conn, home, None, date(2026, 10, 28))
    line = rf.save(conn, home, 1_616_000, date(2026, 10, 28))
    assert line == "Home: value saved (Oct 28), +1.0% since the last value"
    assert conn.execute("SELECT balance, source FROM balances WHERE account_id = ? AND date = '2026-10-28'",
                        (home,)).fetchone() == (1_616_000, "Redfin estimate")


def test_gold_holding_and_price_answers():
    assert gold.holding("Gold (324 g, 22k)") == (324.0, 0.916)
    assert gold.holding("Gold coins") is None
    assert gold.parse_price("stooq.com", b"Symbol,Date,Time,Close\nXAUUSD,2026-10-18,12:00:00,4012.5\n") == 4012.5
    yahoo = json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": 4020.1}}]}}).encode()
    assert gold.parse_price("Yahoo Finance", yahoo) == 4020.1
    assert gold.parse_price("stooq.com", b"Symbol,Date,Time,Close\nXAUUSD,N/D,N/D,N/D\n") is None   # no price today
    assert gold.fetch_price() is None                                       # tests never go on the internet


def test_gold_updates_once_a_month_and_tries_at_most_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    g = db.get_or_create_account(conn, "Gold (324 g, 22k)", "Physical", "precious_metal")
    db.upsert_balance(conn, g, "2026-09-26", 40_934, "Approximate")
    calls = []
    assert gold.monthly_update(conn, date(2026, 10, 18), fetch=lambda: calls.append(1)) == [
        "Couldn't get today's gold price - the gold value stays as it was; trying again tomorrow."]
    assert gold.monthly_update(conn, date(2026, 10, 18), fetch=lambda: calls.append(1)) == []    # not again today
    out = gold.monthly_update(conn, date(2026, 10, 19), fetch=lambda: (4_000.0, "stooq.com"))
    assert out == ["Gold (324 g, 22k): $38,167 at today's gold price"]          # 324 x 0.916 / 31.1035 x 4000
    assert gold.monthly_update(conn, date(2026, 10, 25), fetch=lambda: (9_999.0, "x")) == []       # done this month
    assert len(calls) == 1
