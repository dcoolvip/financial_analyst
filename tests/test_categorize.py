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

    def fake(items, household=None):
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

    def fake(items, household=None):
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

    def fake(items, household=None):
        seen.extend(i["merchant"] for i in items)
        return {i["id"]: "Mortgage" if "MORTG" in i["merchant"] else "Subscriptions" for i in items}
    monkeypatch.setattr(categorize, "_call_model", fake)
    categorize.auto_categorize(conn, t, insights.is_transfer, recheck=True)
    r = categorize.rules(conn)
    assert sorted(seen) == ["DOVENMUEHLE MTG MORTG PYMT", "NETFLIX"]           # yours isn't even sent
    assert r["DOVENMUEHLE MTG MORTG PYMT"] == ("Mortgage", "ai") and r["SAFEWAY"] == ("Dining", "user")
    assert not categorize.needs_recheck(conn)


def test_payments_at_a_loans_exact_amount_are_that_loan(conn, monkeypatch):
    """Regression: DOVENMUEHLE -$3,135.97 every month was guessed as Housing - it's the Golden 1 mortgage's
    exact monthly payment, which the data already knew."""
    chk = db.add_account(conn, "Checking", "Bank of America", "checking")
    mtg = db.add_account(conn, "Golden 1 Schott Loan", "Golden 1 Credit Union", "mortgage")
    car = db.add_account(conn, "Hyundai loan", "Hyundai Motor Finance", "auto_loan")
    db.update_account_terms(conn, mtg, 0.0675, 3135.97)
    db.update_account_terms(conn, car, 0.0099, 1323.78)
    rows = []
    for m in range(1, 7):
        rows += [(f"2026-0{m}-01", f"DOVENMUEHLE MTG DES:MORTG PYMT ID:00{m}", -3135.97, f"d{m}"),
                 (f"2026-0{m}-08", f"HMF DES:HMFUSA.COM ID:{m}", -1323.78, f"h{m}"),
                 (f"2026-0{m}-15", "SAFEWAY", -1323.78 if m == 1 else -80.0, f"s{m}")]   # one coincidence
    db.insert_transactions(conn, chk, pd.DataFrame(rows, columns=["date", "description", "amount", "fingerprint"]), "x")
    categorize.set_rule(conn, "DOVENMUEHLE MTG MORTG PYMT", "Housing", "ai")          # the old guess
    t = db.transactions(conn)
    found = categorize.match_loan_payments(conn, t)
    assert found == {"DOVENMUEHLE MTG MORTG PYMT": ("Mortgage", "Golden 1 Schott Loan"),
                     "HMF HMFUSA.COM": ("Loan payments", "Hyundai loan")}             # not Safeway's one-off
    r = categorize.rules(conn)
    assert r["DOVENMUEHLE MTG MORTG PYMT"] == ("Mortgage", "loan")
    categorize.set_rule(conn, "DOVENMUEHLE MTG MORTG PYMT", "Housing", "ai")          # AI can't undo it
    assert categorize.rules(conn)["DOVENMUEHLE MTG MORTG PYMT"] == ("Mortgage", "loan")
    assert "DOVENMUEHLE MTG MORTG PYMT" not in set(
        categorize.uncategorized_merchants(conn, t, insights.is_transfer, recheck=True)["merchant"])


def test_ai_gets_the_context_not_just_a_name(conn, monkeypatch):
    chk = db.add_account(conn, "Checking", "Bank of America", "checking")
    card = db.add_account(conn, "Sapphire", "Chase", "credit_card")
    db.update_account_terms(conn, db.add_account(conn, "Mortgage", "Golden 1 Credit Union", "mortgage"), 0.0675, 3135.97)
    db.add_account(conn, "Brokerage", "Robinhood", "brokerage")
    db.insert_transactions(conn, card, pd.DataFrame(
        [("2026-01-05", "K1 SPEED #22", -40.0, "Entertainment", "a"), ("2026-02-05", "K1 SPEED #22", -40.0, None, "b")],
        columns=["date", "description", "amount", "bank_category", "fingerprint"]), "x")
    seen = {}

    def fake(items, household=None):
        seen.update(items=items, household=household)
        return {i["id"]: "Entertainment" for i in items}
    monkeypatch.setattr(categorize, "_call_model", fake)
    categorize.auto_categorize(conn, db.transactions(conn), insights.is_transfer)
    item = seen["items"][0]
    assert item["merchant"] == "K1 SPEED" and item["where"] == "credit card" and item["bank_category"] == "Entertainment"
    assert item["times"] == 2 and item["months"] == 2 and item["same_amount_every_time"] is True
    hh = seen["household"]
    assert hh["loans"] == [{"lender": "Golden 1 Credit Union", "kind": "mortgage", "monthly_payment": 3135.97}]
    assert hh["credit_cards_at"] == ["Chase"] and hh["investment_accounts_at"] == ["Robinhood"]
    assert "Checking" not in str(hh) and "Sapphire" not in str(hh)            # institutions only, no account names


def test_bank_category_filled_in_by_reimport_without_duplicates(conn):
    from finance.importers import apply, parse_file
    from tests.test_bofa import read
    card = db.add_account(conn, "Amex", "American Express", "credit_card")
    parsed = parse_file(read("amex_activity.csv"), filename="activity.csv")
    db.insert_transactions(conn, card, parsed.transactions.drop(columns=["bank_category"]), "old import")
    assert db.transactions(conn)["bank_category"].isna().all()
    assert apply(conn, parsed, card, "activity.csv")["transactions_added"] == 0     # nothing doubled
    assert db.transactions(conn)["bank_category"].notna().any()                     # hints filled in


def test_a_check_is_categorized_on_its_own(conn):
    """Regression: one $2,730 check made 'Other' ~$900/month; categorizing it must not recategorize every check."""
    chk = db.add_account(conn, "Checking", "Bank of America", "checking")
    db.insert_transactions(conn, chk, pd.DataFrame(
        [("2026-08-04", "Check 220", -2730.0, "a"), ("2026-09-18", "Check 210", -50.0, "b")],
        columns=["date", "description", "amount", "fingerprint"]), "x")
    assert categorize.is_one_off(merchant_key("Check 220")) and not categorize.is_one_off("SAFEWAY")
    t = db.transactions(conn)
    db.set_transaction_category(conn, int(t.loc[t["description"] == "Check 220", "id"].iloc[0]), "Housing")
    e = insights.enrich(db.transactions(conn), categorize.rules(conn)).set_index("description")
    assert e.loc["Check 220", "category"] == "Housing" and e.loc["Check 210", "category"] == "Other"


def test_store_name_glued_to_its_number_is_kept():
    assert merchant_key("BP#9563966TTA# 39") == "BP 39"
    assert merchant_key("Arco#82967senter Stqps") == "ARCO STQPS"
    assert merchant_key("99 RANCH #1779") == "99 RANCH"


def test_common_chains_known_without_ai():
    assert insights.categorize("BP#9563966TTA# 39", -27.47) == "Transport"
    assert insights.categorize("MICRO CENTER# 195", -518.98) == "Shopping"
    assert insights.categorize("BPX CONSULTING", -50) != "Transport"          # whole word only


def test_ai_other_does_not_block_keywords_and_names_survive():
    rules = {"BLACK ANGUS": ("Other", "ai")}
    assert insights.categorize("Black Angus-1083", -38, rules) == "Dining"         # AI "Other" = no opinion
    assert insights.categorize("Black Angus-1083", -38, {"BLACK ANGUS": ("Travel", "user")}) == "Travel"
    assert merchant_key("AplPay SKINSPIRIT_49LOS GATOS CA") == "APLPAY SKINSPIRIT GATOS CA"
    assert insights.categorize("EVENT ATM #1-4 06/07 #000640474 WITHDRWL DE ANZA COLLEGE", -60) == "Cash & ATM"


def test_property_tax_is_its_own_category():
    assert insights.categorize("Santa Clara DTAC DES:SantaClara ID:3910396148 INDN:X", -25130.14) == "Property tax"
    assert "Property tax" in categorize.CATEGORIES and "property tax" not in categorize.CATEGORY_HELP["Housing"]


def test_taxes_renamed_to_income_tax_and_the_rest_moved(conn):
    for m in ("IRS USATAXPYMT", "INTUIT TURBOTAX", "RSM - SILICON VALLEY", "MDE COURT EPAY", "WWW.ZENBUSINESS.COM"):
        conn.execute("INSERT INTO merchant_rules VALUES (?, 'Taxes', ?)", (m, "user" if "RSM" in m else "ai"))
    conn.commit()
    assert categorize.rename_taxes_category(conn) == 5
    r = categorize.rules(conn)
    assert r["IRS USATAXPYMT"] == ("Income tax", "ai") and r["RSM - SILICON VALLEY"] == ("Income tax", "user")
    assert r["MDE COURT EPAY"][0] == r["WWW.ZENBUSINESS.COM"][0] == "Government & legal"
    assert "Taxes" not in categorize.CATEGORIES
    assert insights.categorize("IRS DES:USATAXPYMT ID:1", -100) == "Income tax"
