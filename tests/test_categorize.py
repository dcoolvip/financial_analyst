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


def test_merchants_the_model_skips_are_asked_again(tmp_path, monkeypatch):
    from finance import categorize, db, insights
    conn = db.connect(tmp_path / "t.db")
    txns = pd.DataFrame({"description": ["NETFLIX", "SAFEWAY", "SHELL OIL"], "amount": [-15.0, -80.0, -40.0]})
    calls = []

    def fake(items):
        calls.append([i["merchant"] for i in items])
        return {i["id"]: "Other" for i in items[:1]} if len(calls) == 1 else {i["id"]: "Groceries" for i in items}
    monkeypatch.setattr(categorize, "_call_model", fake)
    assert categorize.auto_categorize(conn, txns, insights.is_transfer) == 3
    assert len(calls) == 2 and len(calls[1]) == 2                     # only the skipped two, asked once more
    assert categorize.uncategorized_merchants(conn, txns, insights.is_transfer).empty


def test_merchant_key_keeps_short_numbers_in_names():
    assert merchant_key("99 RANCH #1779") == "99 RANCH"
    assert merchant_key("K1 Speed") == "K1 SPEED" and merchant_key("Us Mobile") == "US MOBILE"
    assert merchant_key("DOORDASH*07/25-2 ORDER") == "DOORDASH ORDER"
    assert merchant_key("SQ *CITY OF FREMONT") == "SQ CITY OF FREMONT"
    assert merchant_key("PAYPAL *SCCLD 4085966293 CA") == "PAYPAL SCCLD CA"          # phone number gone
    assert merchant_key("7-ELEVEN 12345") == "7-ELEVEN"
    assert merchant_key("Apple Caffe HS01") == merchant_key("APPLE CAFFE IL04") == "APPLE CAFFE"   # building codes
    assert merchant_key("PREFERRED REWARDS-MONTHLY FEE WAIVER OF $25") == "PREFERRED REWARDS-MONTHLY FEE WAIVER OF"


def test_saved_categories_follow_the_new_merchant_keys(conn):
    categorize.set_rule(conn, "WHOLE FOODS MKT", "Groceries", "ai")                   # unchanged key
    categorize.set_rule(conn, "SPEED", "Entertainment", "ai")                        # only K1 SPEED had it
    categorize.set_rule(conn, "RANCH", "Groceries", "ai")                            # lumped two merchants
    categorize.set_rule(conn, "US", "Other", "ai")                                   # the AI couldn't tell
    moved = categorize.upgrade_rule_keys(conn, ["WHOLE FOODS MKT", "K1 Speed", "99 RANCH #1", "RANCH 1234", "Us Mobile"])
    r = categorize.rules(conn)
    assert moved == 1 and r["K1 SPEED"] == ("Entertainment", "ai")
    assert "99 RANCH" not in r and "US MOBILE" not in r   # asked again, with real names


def test_mortgage_and_loan_payments_are_their_own_categories(conn, monkeypatch):
    assert insights.categorize("DOVENMUEHLE MTG DES:MORTG PYMT ID:1234 INDN:X", -5200) == "Mortgage"
    assert insights.categorize("HMF DES:HMFUSA.COM ID:99", -1323.78) == "Loan payments"
    assert "Mortgage" in categorize.CATEGORY_HELP and all(categorize.CATEGORY_HELP.values())
    assert all(c in categorize.SYSTEM for c in categorize.CATEGORIES)          # the AI gets the definitions


def test_recheck_redoes_ai_rules_but_never_yours(conn, monkeypatch):
    t = txns([("DOVENMUEHLE MTG DES:MORTG PYMT", -5200), ("NETFLIX", -15), ("SAFEWAY", -80)])
    categorize.set_rule(conn, "DOVENMUEHLE MTG MORTG PYMT", "Housing", "ai")
    categorize.set_rule(conn, "NETFLIX", "Subscriptions", "ai")
    categorize.set_rule(conn, "SAFEWAY", "Dining", "user")
    assert categorize.needs_recheck(conn)                                      # older AI rules, newer categories
    seen = []

    def fake(items):
        seen.extend(i["merchant"] for i in items)
        return {i["id"]: "Mortgage" if "MORTG" in i["merchant"] else "Subscriptions" for i in items}
    monkeypatch.setattr(categorize, "_call_model", fake)
    categorize.auto_categorize(conn, t, insights.is_transfer, recheck=True)
    r = categorize.rules(conn)
    assert sorted(seen) == ["DOVENMUEHLE MTG MORTG PYMT", "NETFLIX"]           # yours isn't even sent
    assert r["DOVENMUEHLE MTG MORTG PYMT"] == ("Mortgage", "ai") and r["SAFEWAY"] == ("Dining", "user")
    assert not categorize.needs_recheck(conn)
