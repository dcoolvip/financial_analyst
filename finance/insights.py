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
    ("Cash & ATM", r"\bATM\b.*(WITHDRAW|WITHDRWL)|ATM WITHDRAWAL|CASH WITHDRAWAL"),
    ("Mortgage", r"MORTG|MTG PYMT|HOME LOAN|DOVENMUEHLE|MR\.? COOPER|ROCKET MORTGAGE"),
    ("Loan payments", r"AUTO LOAN|CAR LOAN|HMFUSA|STUDENT LOAN|NAVIENT|NELNET|SALLIE MAE|LOAN PYMT"),
    ("Property tax", r"DTAC|PROPERTY TAX|TAX COLLECTOR|TREASURER.*TAX"),
    ("Income tax", r"\bIRS\b|USATAXPYMT|FRANCHISE TAX|TURBOTAX|FREETAXUSA|H ?& ?R BLOCK"),
    ("Government & legal", r"COURT|SECRETARY OF STATE"),
    ("Housing", r"\bRENT\b|HOA|PEST"),
    ("Utilities", r"PG&E|PGE|COMCAST|XFINITY|AT&T|VERIZON|T-MOBILE|WATER|ELECTRIC"),
    ("Groceries", r"WHOLE FOODS|SAFEWAY|TRADER JOE|COSTCO|KROGER|SPROUTS"),
    ("Dining", r"RESTAURANT|BLACK ANGUS|DOORDASH|UBER EATS|STARBUCKS|CHIPOTLE|CAFE|GRUBHUB"),
    ("Transport", r"SHELL|CHEVRON|EXXON|\bBP\b|\bARCO\b|VALERO|UBER|LYFT|TOLL|PARKING|DMV"),
    ("Shopping", r"AMAZON|AMZN|TARGET|WALMART|APPLE\.COM|BEST BUY|MICRO CENTER|POSTAL SERVICE|USPS|\bGAP\b|OLD NAVY|BANANA REPUBLIC|NORDSTROM|MACY"),
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
    if rule and not (rule[1] == "ai" and rule[0] == "Other"):     # the AI saying "Other" = no opinion
        return rule[0]
    for name, rx in _CATEGORY_RES:
        if rx.search(description):
            return name
    return "Other income" if amount > 0 else "Other"


# Categories that are money coming in. Anything else that's positive is a refund or credit: it reduces
# that category's spending instead of counting as income.
INCOME_CATEGORIES = {"Income", "Other income", "Payments to people"}
AVERAGE_MONTHS = 12          # a full year, so once-a-year bills (income tax, property tax) count once


def _household_transfer(description: str, names: list[list[str]]) -> bool:
    """A transfer or Zelle naming someone in the household (the people on its shared cards): the household's
    own money moving - "Transfer Deepali Domba Salian" -> Transfer, not income."""
    d = description.upper()
    if not re.search(r"TRANSFER|ZELLE|XFER", d):
        return False
    return any(all(part in d for part in (n[0], n[-1])) for n in names if len(n) >= 2)


def enrich(txns: pd.DataFrame, rules: dict | None = None) -> pd.DataFrame:
    """Add category, merchant and is_transfer to a transactions frame."""
    if txns.empty:
        return txns.assign(category=pd.Series(dtype=str), merchant=pd.Series(dtype=str),
                           is_transfer=pd.Series(dtype=bool))
    out = txns.copy()
    out["merchant"] = out["description"].map(merchant_key)
    auto = [categorize(d, a, rules) for d, a in zip(out["description"], out["amount"])]
    if "purchaser" in out and out["purchaser"].notna().any():
        names = [n.upper().split() for n in out["purchaser"].dropna().unique()]
        yours = {m for m, (_, src) in (rules or {}).items() if src == "user"}
        auto = ["Transfer" if m not in yours and _household_transfer(d, names) else a
                for d, m, a in zip(out["description"], out["merchant"], auto)]
    out["category"] = out["category"].where(out["category"].notna(), pd.Series(auto, index=out.index))
    out["is_transfer"] = out["category"] == "Transfer"
    return out


def monthly_cash_flow(txns: pd.DataFrame, rules: dict | None = None) -> pd.DataFrame:
    """Per month: money in (income categories), money out (spending, net of refunds), and what was left over.
    Transfers between your own accounts and card payments are left out."""
    t = enrich(txns, rules)
    t = t[~t["is_transfer"]]
    if t.empty:
        return pd.DataFrame(columns=["month", "money_in", "money_out", "saved"])
    t = t.assign(month=t["date"].dt.to_period("M").dt.to_timestamp(), income=t["category"].isin(INCOME_CATEGORIES))
    out = pd.DataFrame({
        "money_in": t[t["income"] & (t["amount"] > 0)].groupby("month")["amount"].sum(),
        "money_out": -t[~t["income"]].groupby("month")["amount"].sum(),       # refunds net against spending
    }).fillna(0.0).rename_axis("month").reset_index()
    out["saved"] = out["money_in"] - out["money_out"]
    return out


def estimate_monthly_savings(txns: pd.DataFrame, months: int = AVERAGE_MONTHS,
                             rules: dict | None = None) -> float | None:
    """Average leftover per month over the last N *complete* months, or None if no data."""
    cf = monthly_cash_flow(txns, rules)
    if cf.empty:
        return None
    this_month = pd.Timestamp.today().to_period("M").to_timestamp()
    complete = cf[cf["month"] < this_month].tail(months)
    return float(complete["saved"].mean()) if len(complete) else None


def spending_by_category(txns: pd.DataFrame, months: int = 3, rules: dict | None = None) -> pd.DataFrame:
    """Average monthly spend per category over the last N complete months, net of refunds."""
    t = enrich(txns, rules)
    this_month = pd.Timestamp.today().to_period("M").to_timestamp()
    start = this_month - pd.DateOffset(months=months)
    t = t[(~t["is_transfer"]) & (~t["category"].isin(INCOME_CATEGORIES)) & (t["date"] >= start) & (t["date"] < this_month)]
    if t.empty:
        return pd.DataFrame(columns=["category", "monthly"])
    s = (-t.groupby("category")["amount"].sum() / months)
    return s[s > 0].sort_values(ascending=False).rename("monthly").reset_index()


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


INVESTMENT_RE = r"(?i)MSPBNA|MORGAN STANLEY|E\*?TRADE|WEALTHFRONT BROKERAGE|ROBINHOOD|MERRILL|SCHWAB|FIDELITY|VANGUARD"


def _cost_detail(category: str, total: float, spending: pd.DataFrame, shares: dict | None) -> str:
    """What's behind a big cost, in a few words: which homes a property tax covers, or the one payment that
    makes up most of it."""
    if category == "Property tax" and shares:
        whole = sum(shares.values())
        return " (" + ", ".join(f"{k.replace(' Home', '')} {_m(total * v / whole)}" for k, v in
                                sorted(shares.items(), key=lambda kv: -kv[1])) + ")"
    rows = spending[spending["category"] == category].sort_values("amount")
    if len(rows) and -rows["amount"].iloc[0] >= 10_000 and -rows["amount"].iloc[0] >= 0.5 * total:
        r = rows.iloc[0]
        return f" (mostly one {_short_name(r['merchant'])} payment of {_m(-r['amount'])}, {r['date']:%b %Y})"
    return ""


def _short_name(merchant: str) -> str:
    """A readable name for a merchant key: "IRS USATAXPYMT" -> "IRS", "SANTA CLARA DTAC SANTACLARA" -> "Santa Clara"."""
    words = merchant.split()
    if words and len(words[0]) <= 4 and words[0].isalpha():
        return words[0]
    return " ".join(words[:2]).title()


def _investment_name(description: str) -> str:
    m = re.search(INVESTMENT_RE, description)
    word = m.group(0).upper() if m else "Investments"
    return {"MSPBNA": "Morgan Stanley", "MORGAN STANLEY": "Morgan Stanley", "WEALTHFRONT BROKERAGE": "Wealthfront",
            "ROBINHOOD": "Robinhood", "MERRILL": "Merrill", "SCHWAB": "Schwab", "FIDELITY": "Fidelity",
            "VANGUARD": "Vanguard"}.get(word, "E*TRADE" if "TRADE" in word else word.title())


# How long before a value counts as out of date, by kind of account: statements come monthly; a home, car or
# gold is fine for months.
STALE_DAYS = {"property": 180, "vehicle": 180, "collectible": 120, "precious_metal": 120, "other_asset": 180}


def coverage(t: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Timestamp, str]:
    """Is every month in [start, end) complete? An account whose transactions begin inside the window either
    replaced another one (a new card taking over from an old one, pay moving to a new bank - household totals
    don't jump when it starts) or is missing its earlier history (totals jump). Missing history shortens the
    window to where it's complete. Returns (start to use, explanation)."""
    if "account" not in t:
        return start, ""
    t = t[(t["month"] < end)]
    volume = t.assign(v=t["amount"].abs()).groupby("month")["v"].sum()
    inside = t[t["month"] >= start]
    share = inside.assign(v=inside["amount"].abs()).groupby("account")["v"].sum() / max(inside["amount"].abs().sum(), 1)
    notes = []
    for acct, first in t.groupby("account")["date"].min().items():
        begins = first.to_period("M").to_timestamp() + pd.DateOffset(months=1 if first.day > 7 else 0)   # a full month
        if begins <= start or share.get(acct, 0) < 0.10:
            continue
        before = volume[(volume.index >= begins - pd.DateOffset(months=3)) & (volume.index < begins)].mean()
        after = volume[(volume.index >= begins) & (volume.index < begins + pd.DateOffset(months=3))].mean()
        if pd.notna(before) and before >= 0.7 * after:
            notes.append(f"{acct} starts {begins:%b %Y}, but household totals didn't jump then - it took over "
                         "from another account, so the earlier months are complete")
        else:
            start = max(start, begins)
            notes.append(f"{acct} has no history before {begins:%b %Y}, so the average starts there")
    return start, "; ".join(notes)


def _m(v: float) -> str:
    from .charts import money
    return money(v)


def overview(enriched: pd.DataFrame, accts: pd.DataFrame, totals: dict, moves: pd.DataFrame,
             year_change: tuple, today: pd.Timestamp | None = None,
             property_tax_shares: dict | None = None) -> list[dict]:
    """Plain-language takeaways, worked out from everything imported so far. Each: icon, text, help (how it's
    calculated - shown on the ⓘ). enriched = enrich(transactions); moves = db.account_changes(conn, 12);
    year_change = db.net_worth_change(conn, 12). property_tax_shares = {home name: its share of each property tax
    payment}, when one payment covers several homes."""
    today = (today or pd.Timestamp.today()).normalize()
    this_month = today.to_period("M").to_timestamp()
    out = []
    t = enriched[~enriched["is_transfer"]] if len(enriched) else enriched
    t = t.assign(month=t["date"].dt.to_period("M").dt.to_timestamp()) if len(t) else t
    start = this_month - pd.DateOffset(months=AVERAGE_MONTHS)
    start, covered = coverage(t, start, this_month) if len(t) else (start, "")
    recent = t[(t["month"] >= start) & (t["month"] < this_month)] if len(t) else t
    months = recent["month"].nunique() if len(recent) else 0

    spend = 0.0
    if months:
        is_in = recent["category"].isin(INCOME_CATEGORIES)
        came_in = recent.loc[is_in & (recent["amount"] > 0), "amount"].sum()
        spending = recent[~is_in]
        went_out = -spending["amount"].sum()
        income, spend = came_in / months, went_out / months            # monthly, for the cash cushion below
        period = (f"the last 12 months ({start:%b %Y} – {this_month - pd.DateOffset(days=1):%b %Y})" if months == 12
                  else f"the last {months} months ({start:%b %Y} – {this_month - pd.DateOffset(days=1):%b %Y})")
        costs = (-spending.groupby("category")["amount"].sum()).sort_values(ascending=False)
        top3 = list(costs.head(3).index)
        top = ", ".join(f"{c} {_m(costs[c])}{_cost_detail(c, costs[c], spending, property_tax_shares)}" for c in top3)
        gap = came_in - went_out
        if gap >= 0:
            text = (f"Over {period}, **{_m(came_in)} came in and {_m(went_out)} went out: you kept {_m(gap)}** "
                    f"({gap / came_in:.0%}).")
        else:
            text = (f"Over {period}, **{_m(went_out)} went out and {_m(came_in)} came in: {_m(-gap)} more than came "
                    "in.**")
        text += f" Biggest costs: {top}."
        others = spending[(spending["amount"] <= -10_000) & ~spending["category"].isin(top3)].sort_values("amount")
        if len(others):                                 # big payments the costs above don't already explain
            text += " Also: " + ", ".join(f"{_short_name(r.merchant)} {_m(-r.amount)} ({r.category}, {r.date:%b %Y})"
                                          for r in others.head(2).itertuples()) + "."
        if gap < 0 and "account_type" in enriched:
            cash = enriched[enriched["is_transfer"] & enriched["account_type"].isin(["checking", "savings"])]
            cash = cash[(cash["date"] >= start) & (cash["date"] < this_month)]
            inv = cash[cash["description"].str.contains(INVESTMENT_RE, regex=True)]
            net = inv["amount"].sum()                           # in from brokerages, minus what went back in
            if net > 0:
                flows = inv.groupby(inv["description"].map(_investment_name))["amount"].sum().sort_values(ascending=False)
                ins = [f"{_m(v)} from {k}" for k, v in flows.items() if v >= 500]
                outs = [f"{_m(-v)} put into {k}" for k, v in flows.items() if v <= -500]
                text += (f" The difference came from investments: net {_m(net)} ("
                         + " and ".join(ins[:3]) + (", less " + " and ".join(outs[:2]) if outs else "") + ").")
        how = (f"Totals over {period} - a full year, so once-a-year bills like income and property tax count "
               "once. Came in = pay and other income; went out = spending net of refunds, incl. mortgage, taxes and "
               "card purchases. Transfers between your own accounts and card bill payments are left out. Stock "
               "vesting and investment growth aren't cash coming in - they show in net worth."
               + (f" Coverage: {covered}." if covered else " Every account has transactions for the whole period."))
        out.append({"icon": "💰" if gap >= 0 else "💸", "help": how, "text": text})

        # what changed: last complete month against the months before it
        last = t[t["month"] == this_month - pd.DateOffset(months=1)]
        before = t[(t["month"] >= this_month - pd.DateOffset(months=7)) & (t["month"] < this_month - pd.DateOffset(months=1))]
        n_before = before["month"].nunique()
        if len(last) and n_before >= 2:
            now_c = -last[last["amount"] < 0].groupby("category")["amount"].sum()
            usual = -before[before["amount"] < 0].groupby("category")["amount"].sum() / n_before
            jump = (now_c - usual.reindex(now_c.index).fillna(0)).sort_values(ascending=False)
            if len(jump) and jump.iloc[0] >= 300 and now_c[jump.index[0]] >= 1.5 * usual.get(jump.index[0], 0):
                c = jump.index[0]
                month_name = (this_month - pd.DateOffset(months=1)).strftime("%B")
                out.append({"icon": "🔎", "help": f"{month_name}'s spending per category, against its average over "
                                                   f"the {n_before} months before. Shows the biggest jump.",
                            "text": f"**{c}** was {_m(now_c[c])} in {month_name}, against a usual "
                                    f"{_m(usual.get(c, 0))} a month."})

    yr, base, left_out = year_change
    if yr is not None and base:
        drivers = moves.sort_values("change", ascending=yr < 0).head(2) if len(moves) else moves
        why = (", mostly " + " and ".join(f"{r.name} ({'+' if r.change >= 0 else ''}{_m(r.change)})"
                                          for r in drivers.itertuples()) if len(drivers) else "")
        out.append({"icon": "📈" if yr >= 0 else "📉",
                    "help": "Like-for-like: only accounts that had a value a year ago and today, so adding an account "
                            "never looks like a gain."
                            + (f" Not included yet (no value a year ago): {', '.join(left_out)}." if left_out else ""),
                    "text": f"Net worth is {'up' if yr >= 0 else 'down'} **{_m(abs(yr))}** over the past year "
                            f"({yr / abs(base):+.0%}){why}."
                            + (f" {len(left_out)} newer account{'s' if len(left_out) > 1 else ''} not included." if left_out else "")})

    if spend > 0 and totals.get("Cash"):
        out.append({"icon": "🛟", "help": "Cash (checking + savings) divided by your average monthly spending. "
                                         "3–6 months is the usual cushion.",
                    "text": f"Your cash would cover **{totals['Cash'] / spend:.1f} months** of spending."})

    own = sum(totals.get(g, 0) for g in ASSET_GROUPS)
    owe = sum(totals.get(g, 0) for g in DEBT_GROUPS)
    if own:
        text = f"Debt is **{owe / own:.0%}** of what you own."
        payoffs = []
        for r in accts[accts["type"].isin(["mortgage", "auto_loan", "heloc", "personal_loan", "student_loan"])].itertuples():
            if p := loan_payoff(r.balance, r.rate, r.payment):
                payoffs.append((p[0], r.name, r.payment))
        if payoffs:
            n, name, pay = min(payoffs)
            text += (f" Next paid off: **{name}** around {today + pd.DateOffset(months=n):%b %Y}, freeing "
                     f"{_m(pay)} a month.")
        out.append({"icon": "🏦", "text": text,
                    "help": "What you owe on cards and loans, against everything you own. Payoff dates use each loan's "
                            "rate and monthly payment."})

    missing = accts[accts["balance"].isna()]
    if len(missing):
        out.append({"icon": "💳", "help": "Card downloads don't include a balance.",
                    "text": f"**{len(missing)} account(s) have no balance yet:** " + ", ".join(missing["name"])
                            + ". Type it from the bank's app into **Accounts**, or import a statement PDF."})

    have = accts[accts["balance"].notna()].copy()
    if len(have):
        age = (today - pd.to_datetime(have["as_of"])).dt.days
        due = have[age > have["type"].map(lambda k: STALE_DAYS.get(k, 45))]
        if len(due):
            items = ", ".join(f"{r.name} ({pd.Timestamp(r.as_of):%b %-d})" for r in due.itertuples())
            out.append({"icon": "⏰", "help": "Accounts with statements: after 45 days. Homes, cars, gold and "
                                             "collectibles: after 4–6 months.",
                        "text": f"**Due for an update:** {items}. Import their latest statements, or type the value "
                                "in **Accounts**."})
    return out


LOAN_CATEGORIES = {"Mortgage", "Loan payments"}


def cash_flow_baseline(enriched: pd.DataFrame, today: pd.Timestamp | None = None) -> dict | None:
    """Your average month over the last 12 complete months (same window and coverage check as the Overview):
    income, living costs (all spending except loan payments), and the loan payments seen. The forecast grows
    income with raises and living costs with inflation; loan payments stay fixed and stop when paid off."""
    if enriched.empty:
        return None
    today = (today or pd.Timestamp.today()).normalize()
    this_month = today.to_period("M").to_timestamp()
    t = enriched[~enriched["is_transfer"]]
    t = t.assign(month=t["date"].dt.to_period("M").dt.to_timestamp())
    start, _ = coverage(t, this_month - pd.DateOffset(months=AVERAGE_MONTHS), this_month)
    t = t[(t["month"] >= start) & (t["month"] < this_month)]
    months = t["month"].nunique()
    if not months:
        return None
    is_in = t["category"].isin(INCOME_CATEGORIES)
    loans = t["category"].isin(LOAN_CATEGORIES)
    return {"months": months, "start": start,
            "income": float(t.loc[is_in & (t["amount"] > 0), "amount"].sum() / months),
            "living": float(-t.loc[~is_in & ~loans, "amount"].sum() / months),
            "loans_seen": float(-t.loc[loans, "amount"].sum() / months)}
