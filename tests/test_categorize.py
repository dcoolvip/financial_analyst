import pandas as pd
import pytest

from finance import categorize, db, insights
from finance.categorize import merchant_key


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def txns(rows):
    return pd.DataFrame(rows, columns=["description", "amount"]).assign(
        date=pd.Timestamp("2026-08-10"), category=None)


def test_merchant_key_strips_personal_details():
    assert merchant_key("VENMO DES:PAYMENT ID:1053253435426 INDN:Jane Doe CO ID:3264681992 WEB") == "VENMO PAYMENT"
    assert merchant_key("APPLE INC. DES:PAYROLL ID:203239 INDN:Jane Doe") == "APPLE INC. PAYROLL"
    assert merchant_key("CHECKCARD 0828 WHOLE FOODS MKT 10234 SAN JOSE CA 24492156") == "WHOLE FOODS MKT SAN JOSE CA"
    assert merchant_key("Zelle payment to KEVIN LE Conf# ysuuywmip") == "ZELLE PAYMENT TO KEVIN LE"


def test_rule_precedence(conn):
    categorize.set_rule(conn, "WHOLE FOODS MKT", "Dining", source="ai")
    categorize.set_rule(conn, "MERRILL TRANSFER", "Income", source="ai")
    r = categorize.rules(conn)
    assert insights.categorize("WHOLE FOODS MKT", -50, r) == "Dining"          # AI beats keywords
    assert insights.categorize("MERRILL DES:TRANSFER", -500, r) == "Transfer"  # transfer detector beats AI
    categorize.set_rule(conn, "WHOLE FOODS MKT", "Groceries")                  # user choice
    categorize.set_rule(conn, "WHOLE FOODS MKT", "Shopping", source="ai")     # AI can't override it
    assert insights.categorize("WHOLE FOODS MKT", -50, categorize.rules(conn)) == "Groceries"


def test_auto_categorize_batches_by_merchant(conn, monkeypatch):
    calls = []

    def fake(items):
        calls.append(items)
        return {it["id"]: ("Income" if it["direction"] == "in" else "Payments to people") for it in items}

    monkeypatch.setattr(categorize, "_call_model", fake)
    t = txns([("VENMO DES:PAYMENT ID:1 INDN:X", -20), ("VENMO DES:PAYMENT ID:2 INDN:X", -35),
              ("ACME DES:PAYROLL ID:9", 4000), ("Online Banking transfer to SAV 1234", -500)])
    assert categorize.auto_categorize(conn, t, insights.is_transfer) == 2   # 2 merchants, transfer skipped
    assert len(calls) == 1 and {i["merchant"] for i in calls[0]} == {"VENMO PAYMENT", "ACME PAYROLL"}
    assert "INDN" not in str(calls) and "X" not in {i["merchant"] for i in calls[0]}
    assert categorize.auto_categorize(conn, t, insights.is_transfer) == 0   # nothing left to do
