"""Turns raw accounts/transactions into the simple numbers the UI shows.

All the fiddly parts live here so the UI can stay plain:
  * account types -> a handful of human groups
  * internal transfers (checking -> savings, card payments) are not spending
  * keyword categorization of transactions
  * a monthly-savings estimate that feeds the forecast
"""
from __future__ import annotations

import re

import pandas as pd

from .categorize import merchant_key

GROUPS = {
    "checking": "Cash", "savings": "Cash",
    "brokerage": "Investments", "retirement": "Investments",
    "property": "Property & valuables", "vehicle": "Property & valuables", "collectible": "Property & valuables",
    "precious_metal": "Property & valuables", "other_asset": "Property & valuables",
    "mortgage": "Loans", "auto_loan": "Loans", "heloc": "Loans", "personal_loan": "Loans",
    "student_loan": "Loans", "other_liability": "Loans",
    "credit_card": "Credit cards",
}
ASSET_GROUPS = ["Cash", "Investments", "Property & valuables"]
DEBT_GROUPS = ["Loans", "Credit cards"]

# Money moving between your own accounts - neither income nor spending
TRANSFER_RE = re.compile(
    r"ONLINE BANKING TRANSFER|TRANSFER (TO|FROM)|PAYMENT\s*-?\s*THANK YOU|ONLINE BANKING PAYMENT TO CRD"
    r"|PAYMENT TO CHASE CARD|CHASE CREDIT CRD|WEALTHFRONT|AMERICAN EXPRESS ACH PMT|AMEX EPAYMENT|PAYMENT RECEIVED|BARCLAYCARD|APPLECARD GSBANK|\(ACCOUNT \*+\d{4}\)"
    r"|BANK OF AMERICA CREDIT CARD|CREDIT CARD BILL PAYMENT|MERRILL|EDGE BROKERAGE|AUTOPAY PAYMENT",
    re.IGNORECASE,
)

# First match wins. Extend freely - it's just keywords.
CATEGORY_RULES = [
    ("Income", r"PAYROLL|DIRECT DEP|DES:SALARY|\bINTEREST(?! CHARGE)\b|DIVIDEND"),
    ("Mortgage", r"MORTG|MTG PYMT|HOME LOAN|DOVENMUEHLE|MR\.? COOPER|ROCKET MORTGAGE"),
    ("Loan payments", r"AUTO LOAN|CAR LOAN|HMFUSA|STUDENT LOAN|NAVIENT|NELNET|SALLIE MAE|LOAN PYMT"),
    ("Housing", r"\bRENT\b|HOA|PROPERTY TAX|PEST"),
    ("Utilities", r"PG&E|PGE|COMCAST|XFINITY|AT&T|VERIZON|T-MOBILE|WATER|ELECTRIC"),
    ("Groceries", r"WHOLE FOODS|SAFEWAY|TRADER JOE|COSTCO|KROGER|SPROUTS"),
    ("Dining", r"RESTAURANT|DOORDASH|UBER EATS|STARBUCKS|CHIPOTLE|CAFE|GRUBHUB"),
    ("Transport", r"SHELL|CHEVRON|EXXON|UBER|LYFT|TOLL|PARKING|DMV"),
    ("Shopping", r"AMAZON|AMZN|TARGET|WALMART|APPLE\.COM|BEST BUY|\bGAP\b|OLD NAVY|BANANA REPUBLIC|NORDSTROM|MACY"),
    ("Health", r"PHARMACY|CVS|WALGREENS|KAISER|DENTAL|MEDICAL"),
    ("Subscriptions", r"NETFLIX|SPOTIFY|HULU|DISNEY|YOUTUBE|ICLOUD"),
    ("Insurance", r"INSURANCE|GEICO|STATE FARM|ALLSTATE|PROGRESSIVE"),
]
_CATEGORY_RES = [(name, re.compile(p, re.IGNORECASE)) for name, p in CATEGORY_RULES]


def is_transfer(description: str) -> bool:
    return bool(TRANSFER_RE.search(description))


def categorize(description: str, amount: float, rules: dict | None = None) -> str:
    """Your rules > transfer detection > AI rules > keyword rules > Other."""
    rule = (rules or {}).get(merchant_key(description))
    if rule and rule[1] == "user":
        return rule[0]
    if is_transfer(description):
        return "Transfer"
    if rule:
        return rule[0]
    for name, rx in _CATEGORY_RES:
        if rx.search(description):
            return name
    return "Other income" if amount > 0 else "Other"


def enrich(txns: pd.DataFrame, rules: dict | None = None) -> pd.DataFrame:
    """Add category, merchant and is_transfer to a transactions frame."""
    if txns.empty:
        return txns.assign(category=pd.Series(dtype=str), merchant=pd.Series(dtype=str),
                           is_transfer=pd.Series(dtype=bool))
    out = txns.copy()
    out["merchant"] = out["description"].map(merchant_key)
    auto = [categorize(d, a, rules) for d, a in zip(out["description"], out["amount"])]
    out["category"] = out["category"].where(out["category"].notna(), pd.Series(auto, index=out.index))
    out["is_transfer"] = out["category"] == "Transfer"
    return out


def monthly_cash_flow(txns: pd.DataFrame, rules: dict | None = None) -> pd.DataFrame:
    """Per month: money in, money out (positive number), and what was left over."""
    t = enrich(txns, rules)
    t = t[~t["is_transfer"]]
    if t.empty:
        return pd.DataFrame(columns=["month", "money_in", "money_out", "saved"])
    t = t.assign(month=t["date"].dt.to_period("M").dt.to_timestamp())
    g = t.groupby("month")["amount"]
    out = pd.DataFrame({
        "money_in": g.apply(lambda s: s[s > 0].sum()),
        "money_out": g.apply(lambda s: -s[s < 0].sum()),
    }).reset_index()
    out["saved"] = out["money_in"] - out["money_out"]
    return out


def estimate_monthly_savings(txns: pd.DataFrame, months: int = 6, rules: dict | None = None) -> float | None:
    """Average leftover per month over the last N *complete* months, or None if no data."""
    cf = monthly_cash_flow(txns, rules)
    if cf.empty:
        return None
    this_month = pd.Timestamp.today().to_period("M").to_timestamp()
    complete = cf[cf["month"] < this_month].tail(months)
    return float(complete["saved"].mean()) if len(complete) else None


def spending_by_category(txns: pd.DataFrame, months: int = 3, rules: dict | None = None) -> pd.DataFrame:
    """Average monthly spend per category over the last N complete months."""
    t = enrich(txns, rules)
    this_month = pd.Timestamp.today().to_period("M").to_timestamp()
    start = this_month - pd.DateOffset(months=months)
    t = t[(~t["is_transfer"]) & (t["amount"] < 0) & (t["date"] >= start) & (t["date"] < this_month)]
    if t.empty:
        return pd.DataFrame(columns=["category", "monthly"])
    s = (-t.groupby("category")["amount"].sum() / months).sort_values(ascending=False)
    return s.rename("monthly").reset_index()


def group_totals(accts: pd.DataFrame) -> dict[str, float]:
    a = accts.assign(group=accts["type"].map(GROUPS), balance=accts["balance"].fillna(0.0))
    totals = a.groupby("group")["balance"].sum().to_dict()
    return {g: float(totals.get(g, 0.0)) for g in ASSET_GROUPS + DEBT_GROUPS}


def stale_accounts(accts: pd.DataFrame, days: int = 45) -> pd.DataFrame:
    as_of = pd.to_datetime(accts["as_of"])
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=days)
    return accts[as_of.isna() | (as_of < cutoff)]


def source_label(source: str | None) -> str:
    """Where a recorded value came from, in plain words."""
    s = (source or "").strip()
    if not s or s == "manual":
        return "Typed in"
    if s.lower().startswith(("estimated", "approximate")):
        return s
    if s == "pokemon dashboard":
        return "Pokemon dashboard"
    if s.startswith("Redfin"):
        return "Redfin estimate"
    return f"Imported from {s}"


def is_estimate(source: str | None) -> bool:
    return (source or "").lower().startswith(("estimated", "approximate"))


def loan_payoff(balance, rate, payment) -> tuple[int, float] | None:
    """(months left, interest still to pay) at this rate and monthly payment, or None if it never pays off
    (payment doesn't cover the interest) or terms are missing."""
    import math
    if not balance or balance <= 0 or not payment or payment <= 0 or rate is None or pd.isna(rate):
        return None
    i = rate / 12
    if i <= 0:
        n = math.ceil(balance / payment)
    elif payment <= balance * i:
        return None
    else:
        n = math.ceil(-math.log(1 - i * balance / payment) / math.log(1 + i))
    return n, max(0.0, n * payment - balance)


def change_by_group(changes: pd.DataFrame, types: dict) -> dict[str, float]:
    """Sum account changes (from db.account_changes) into the Overview groups, in GROUPS order."""
    out = {g: 0.0 for g in ASSET_GROUPS + DEBT_GROUPS}
    for r in changes.itertuples():
        g = GROUPS.get(types.get(r.account_id))
        if g:
            out[g] += r.change
    return {g: v for g, v in out.items() if abs(v) >= 0.5}
