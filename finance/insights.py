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

GROUPS = {
    "checking": "Cash", "savings": "Cash",
    "brokerage": "Investments", "retirement": "Investments",
    "property": "Property", "vehicle": "Property", "other_asset": "Property",
    "mortgage": "Loans", "auto_loan": "Loans", "heloc": "Loans", "personal_loan": "Loans",
    "student_loan": "Loans", "other_liability": "Loans",
    "credit_card": "Credit cards",
}
ASSET_GROUPS = ["Cash", "Investments", "Property"]
DEBT_GROUPS = ["Loans", "Credit cards"]

# Money moving between your own accounts - neither income nor spending
TRANSFER_RE = re.compile(
    r"ONLINE BANKING TRANSFER|TRANSFER (TO|FROM)|PAYMENT - THANK YOU|ONLINE BANKING PAYMENT TO CRD"
    r"|BANK OF AMERICA CREDIT CARD|CREDIT CARD BILL PAYMENT|MERRILL|EDGE BROKERAGE|AUTOPAY PAYMENT",
    re.IGNORECASE,
)

# First match wins. Extend freely - it's just keywords.
CATEGORY_RULES = [
    ("Income", r"PAYROLL|DIRECT DEP|DES:SALARY|INTEREST EARNED|DIVIDEND"),
    ("Housing", r"MORTGAGE|RENT|HOA|PROPERTY TAX"),
    ("Utilities", r"PG&E|PGE|COMCAST|XFINITY|AT&T|VERIZON|T-MOBILE|WATER|ELECTRIC"),
    ("Groceries", r"WHOLE FOODS|SAFEWAY|TRADER JOE|COSTCO|KROGER|SPROUTS"),
    ("Dining", r"RESTAURANT|DOORDASH|UBER EATS|STARBUCKS|CHIPOTLE|CAFE|GRUBHUB"),
    ("Transport", r"SHELL|CHEVRON|EXXON|UBER|LYFT|TOLL|PARKING|AUTO LOAN|DMV"),
    ("Shopping", r"AMAZON|AMZN|TARGET|WALMART|APPLE\.COM|BEST BUY"),
    ("Health", r"PHARMACY|CVS|WALGREENS|KAISER|DENTAL|MEDICAL"),
    ("Subscriptions", r"NETFLIX|SPOTIFY|HULU|DISNEY|YOUTUBE|ICLOUD"),
    ("Insurance", r"INSURANCE|GEICO|STATE FARM|ALLSTATE|PROGRESSIVE"),
]
_CATEGORY_RES = [(name, re.compile(p, re.IGNORECASE)) for name, p in CATEGORY_RULES]


def categorize(description: str, amount: float) -> str:
    if TRANSFER_RE.search(description):
        return "Transfer"
    for name, rx in _CATEGORY_RES:
        if rx.search(description):
            return name
    return "Other income" if amount > 0 else "Other"


def enrich(txns: pd.DataFrame) -> pd.DataFrame:
    """Add category + is_transfer to a transactions frame."""
    if txns.empty:
        return txns.assign(category=pd.Series(dtype=str), is_transfer=pd.Series(dtype=bool))
    out = txns.copy()
    auto = [categorize(d, a) for d, a in zip(out["description"], out["amount"])]
    out["category"] = out["category"].where(out["category"].notna(), pd.Series(auto, index=out.index))
    out["is_transfer"] = out["category"] == "Transfer"
    return out


def monthly_cash_flow(txns: pd.DataFrame) -> pd.DataFrame:
    """Per month: money in, money out (positive number), and what was left over."""
    t = enrich(txns)
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


def estimate_monthly_savings(txns: pd.DataFrame, months: int = 6) -> float | None:
    """Average leftover per month over the last N *complete* months, or None if no data."""
    cf = monthly_cash_flow(txns)
    if cf.empty:
        return None
    this_month = pd.Timestamp.today().to_period("M").to_timestamp()
    complete = cf[cf["month"] < this_month].tail(months)
    return float(complete["saved"].mean()) if len(complete) else None


def spending_by_category(txns: pd.DataFrame, months: int = 3) -> pd.DataFrame:
    """Average monthly spend per category over the last N complete months."""
    t = enrich(txns)
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
