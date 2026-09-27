"""Net-worth projection.

Approach (the same idea ProjectionLab / Empower use, kept small):
  * Investments follow random yearly-ish returns -> run many simulations and
    report a range (10th / 50th / 90th percentile), not one false-precision line.
  * Cash earns a steady yield; homes appreciate; vehicles depreciate.
  * Each loan is amortized month by month. When it's paid off, its payment is
    redirected to savings (your spending doesn't change, the bill just ends).
  * Saving each month = income - living costs - loan payments. Income grows with raises, living costs with
    inflation; loan payments stay fixed (a mortgage payment doesn't inflate) and stop when the loan is paid off.
    (Older saved assumptions with a single "saved per month" still work: that amount grows with raises.)
  * Everything is also reported in today's dollars, which is what people
    actually mean by "how much will I have".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .insights import GROUPS

# Used when an account has no rate/payment set: (APR, remaining years)
LOAN_DEFAULTS = {
    "mortgage": (0.065, 25),
    "auto_loan": (0.07, 4),
    "heloc": (0.085, 10),
    "student_loan": (0.055, 10),
    "personal_loan": (0.11, 3),
    "other_liability": (0.08, 5),
}


@dataclass
class Assumptions:
    years: int = 10
    inflation: float = 0.03
    investment_return: float = 0.07       # nominal, long-run stock/bond blend
    investment_volatility: float = 0.15
    cash_yield: float = 0.03
    home_appreciation: float = 0.035
    vehicle_depreciation: float = 0.15
    monthly_savings: float = 0.0
    invest_share: float = 0.8             # share of new savings that gets invested
    savings_growth: float = 0.03
    monthly_income: float | None = None   # when both are set, saving = income - living costs - loan payments
    monthly_living: float | None = None   # everything spent except loan payments, today's prices
    income_growth: float = 0.03           # raises per year
    # planned changes to a loan, e.g. a refinance: [{"loan": name, "month": months from now, "payment": new
    # monthly payment, "rate": new yearly rate or None to keep the current one}]
    loan_changes: list = field(default_factory=list)
    replay_history: bool = True           # investment ups and downs replay real stock years (history.py)
    single_stock_share: float = 0.0       # share of investments held in one company (Apple): its bigger swings
    # Retirement (see retirement.py). people: [{name, born 'YYYY-MM', retire_age, pay (monthly take-home),
    # ss_monthly (today's $, at 67), ss_claim_age, k401_yearly (pre-tax in), roth_yearly, stock_yearly (RSU/ESPP
    # net)}]. With people, income = each person's pay until they retire + other_income + Social Security.
    people: list = field(default_factory=list)
    other_income: float | None = None     # monthly: rent, interest, dividends - continues after retiring
    pretax_balance: float = 0.0           # part of investments that is pre-tax 401(k)/IRA money
    private_health_yearly: float = 15_000.0   # per person, retired before 65 with nobody working (today's $)
    medicare_yearly: float = 12_000.0         # per person from 65: Parts B + D, supplement, IRMAA (today's $)
    health_extra_growth: float = 0.02         # healthcare costs grow this much faster than inflation
    pretax_tax_rate: float = 0.30             # tax on pre-tax 401(k) money when it comes out
    rmd_age: int = 75                         # required withdrawals start (born 1960 or later)
    simulations: int = 1000
    seed: int = 7

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Assumptions":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class Loan:
    name: str
    balance: float
    apr: float
    payment: float
    estimated: bool           # True when we filled in defaults


@dataclass
class Forecast:
    bands: pd.DataFrame                       # date, p10, p50, p90 (nominal) + *_real
    expected: pd.DataFrame                    # date, cash, investments, property, debt, net_worth (+ _real)
    loans: list[Loan]
    milestones: list[tuple[pd.Timestamp, str]] = field(default_factory=list)
    start_net_worth: float = 0.0
    valuables: pd.DataFrame = field(default_factory=pd.DataFrame)   # per asset: name, type, value, rate, source, end
    valuable_paths: pd.DataFrame = field(default_factory=pd.DataFrame)  # date x asset name, expected path
    cash_flow: pd.DataFrame = field(default_factory=pd.DataFrame)       # per year: income, living, loans, saved


def payment_for(balance: float, apr: float, years: float) -> float:
    n, r = years * 12, apr / 12
    return balance / n if r == 0 else balance * r / (1 - (1 + r) ** -n)


def _num(x) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def build_loans(accts: pd.DataFrame) -> tuple[list[Loan], float]:
    """Amortizing loans, plus revolving credit-card debt (assumed paid in full monthly)."""
    loans, revolving = [], 0.0
    for a in accts.itertuples():
        bal = float(a.balance or 0)
        if bal <= 0:
            continue
        rate, payment = _num(a.rate), _num(a.payment)
        if a.type == "credit_card" and not (rate and payment):
            revolving += bal
            continue
        if GROUPS.get(a.type) not in ("Loans", "Credit cards"):
            continue
        apr_d, years_d = LOAN_DEFAULTS.get(a.type, (0.08, 5))
        apr = rate if rate is not None else apr_d
        pay = payment or payment_for(bal, apr, years_d)
        loans.append(Loan(a.name, bal, apr, pay, estimated=rate is None or not payment))
    return loans, revolving


def valuables_table(accts: pd.DataFrame, a: Assumptions, history: pd.DataFrame | None = None,
                    trend_overrides: dict | None = None) -> pd.DataFrame:
    """Each home, car, collectible, gold... with the growth rate and volatility the forecast uses for it:
    your own rate if set, else its history blended with the long-run rate (the Home/Car sliders are the
    long-run anchors for those two kinds). See assets.py."""
    from .assets import explain, outlook
    from .db import VALUABLE_TYPES
    history = history if history is not None else pd.DataFrame(columns=["account_id", "date", "balance"])
    rows = []
    for r in accts[accts["type"].isin(VALUABLE_TYPES)].itertuples():
        anchor = {"property": a.home_appreciation, "vehicle": -a.vehicle_depreciation}.get(r.type)
        o = outlook(r.type, r.rate, history[history["account_id"] == r.id], long_run=anchor,
                    trend_override=(trend_overrides or {}).get(r.id))
        rows.append({"id": r.id, "name": r.name, "type": r.type, "value": float(r.balance or 0.0),
                     "rate": o["rate"], "vol": o["vol"], "source": o["source"], "trend": o["trend"],
                     "years": o["years"], "weight": o["weight"], "long_run": o["long_run"],
                     "why": explain(o) + (f"; fades to {o['long_run']:+.1%} over ~{max(o['years'], 1):.0f} yrs"
                                          if o["source"] == "history" and abs(o["rate"] - o["long_run"]) > 0.001 else "")})
    return pd.DataFrame(rows, columns=["id", "name", "type", "value", "rate", "vol", "source", "trend",
                                       "years", "weight", "long_run", "why"])


def rate_path(rate: float, long_run: float, years_of_history: float, source: str, months: int) -> np.ndarray:
    """Yearly growth rate for each coming month. A trend seen over N years says little about decade N+10, so a
    rate built from history moves back toward the long-run rate for the asset's kind, over about as many years
    as the history covers (1.2 years of hot Pokemon prices fade within a few years; 10 years of steady home
    prices fade slowly). A rate you typed in stays as you set it."""
    if source != "history":
        return np.full(months, rate)
    t = np.arange(months) / 12
    return long_run + (rate - long_run) * np.exp(-t / max(years_of_history, 1.0))


def _simulate(accts: pd.DataFrame, a: Assumptions, sims: int, sigma: float, history=None, overrides=None):
    months = a.years * 12
    bal = accts.assign(balance=accts["balance"].fillna(0.0))
    by_type = bal.groupby("type")["balance"].sum()
    cash0 = by_type.reindex(["checking", "savings"]).fillna(0).sum()
    inv0 = by_type.reindex(["brokerage", "retirement"]).fillna(0).sum()
    vt = valuables_table(bal, a, history, overrides)     # each valuable: its own rate and volatility
    val = np.tile(vt["value"].to_numpy(dtype=float), (sims, 1))
    v_rate, v_vol = vt["rate"].to_numpy(dtype=float), vt["vol"].to_numpy(dtype=float) * (sigma > 0)
    loans, revolving = build_loans(bal)

    rng = np.random.default_rng(a.seed)
    share = min(max(a.single_stock_share or 0.0, 0.0), 1.0)
    if a.replay_history and sigma > 0:
        # each simulated year is a real year from history, picked at random, scaled so the typical outcome
        # matches the return you chose (history's shape - crashes, booms - with your level)
        from .history import _growth, apple_years, stock_years
        real = stock_years()
        scaled = (1 + real) * (1 + a.investment_return) / (1 + _growth(pd.Series(real))) - 1
        n_years = -(-months // 12)
        picks = scaled[rng.integers(0, len(scaled), size=(sims, n_years))]
        if share:                            # one company: same average year as the market, Apple's swings
            one = apple_years(float(np.mean(scaled)))
            picks = share * one[rng.integers(0, len(one), size=(sims, n_years))] + (1 - share) * picks
        growth = np.repeat((1 + picks) ** (1 / 12), 12, axis=1)[:, :months]
    else:
        typical = a.investment_return
        if share:                            # the expected path: the typical compounded result with those swings
            from .history import APPLE, arithmetic_from_compounded
            arith = arithmetic_from_compounded(a.investment_return, a.investment_volatility)
            vol = np.sqrt((share * float(APPLE.std(ddof=1))) ** 2 + ((1 - share) * a.investment_volatility) ** 2)
            typical = arith - vol ** 2 / 2
        mu = np.log1p(typical) / 12 - sigma**2 / 24
        growth = np.exp(rng.normal(mu, sigma / np.sqrt(12), size=(sims, months)))
    paths_r = np.stack([rate_path(r.rate, r.long_run, r.years, r.source, months) for r in vt.itertuples()], axis=1) \
        if len(vt) else np.zeros((months, 0))                    # months x assets: each one's rate, fading
    v_mu = np.log1p(paths_r) / 12 - v_vol**2 / 24
    v_growth = np.exp(rng.normal(v_mu, v_vol / np.sqrt(12), size=(sims, months, len(v_rate)))) \
        if len(v_rate) else np.ones((sims, months, 0))
    cash_r = (1 + a.cash_yield) ** (1 / 12) - 1

    cash = np.full(sims, cash0, dtype=float)
    inv = np.full(sims, inv0, dtype=float)
    loan_bal = np.array([l.balance for l in loans], dtype=float)
    loan_rate = np.array([l.apr / 12 for l in loans], dtype=float)
    loan_pay = np.array([l.payment for l in loans], dtype=float)
    payoff: dict[str, int] = {}

    split = a.monthly_income is not None and a.monthly_living is not None
    people = [p for p in (a.people or []) if p.get("born")] if split else []
    if people:
        from . import retirement as ret
        now = pd.Timestamp.today().normalize()
        ages0 = [ret.age_on(p["born"], now) for p in people]
        pre = np.full(sims, min(float(a.pretax_balance or 0), inv0), dtype=float)
        inv = inv - pre                        # taxable investments; pre-tax 401(k) kept apart
    else:
        pre = np.zeros(sims)
    # per month (nominal): income, living costs, loan payments, healthcare, pay, social security, other income,
    # tax on 401(k) withdrawals
    flow = np.zeros((months, 8))
    nw = np.empty((sims, months + 1))
    parts = np.empty((months + 1, 4))  # cash, investments, property & valuables, debt (sim 0 / mean)
    paths = np.empty((months + 1, val.shape[1]))
    paths[0] = val.mean(axis=0)
    debt = loan_bal.sum() + revolving
    nw[:, 0] = cash + inv + pre + val.sum(axis=1) - debt
    parts[0] = [cash.mean(), (inv + pre).mean(), val.sum(axis=1).mean(), debt]

    changes = {}
    names = [l.name for l in loans]
    for ch in a.loan_changes or []:
        if ch.get("loan") in names and ch.get("payment"):
            changes.setdefault(int(ch.get("month") or 1), []).append(ch)

    for m in range(1, months + 1):
        for ch in changes.get(m, []):             # a refinance: new payment (and rate) from this month on
            i = names.index(ch["loan"])
            loan_pay[i] = float(ch["payment"])
            if ch.get("rate") is not None:
                loan_rate[i] = float(ch["rate"]) / 12
        # loans: accrue interest, pay, and free up the payment once cleared
        active = loan_bal > 0
        owed = loan_bal * (1 + loan_rate)
        loan_bal = np.where(active, np.maximum(owed - loan_pay, 0), 0)
        paid = np.where(active, owed - loan_bal, 0).sum()          # the last payment is only what's left
        for i in np.flatnonzero(active & (loan_bal == 0)):
            payoff[loans[i].name] = m
        freed = loan_pay[~active].sum()          # loans already cleared before this month

        tax_401k = 0.0
        if people:
            years = (m - 1) / 12
            price = (1 + a.inflation) ** years
            ages = [a0 + years for a0 in ages0]
            working = [age < p.get("retire_age", 65) for age, p in zip(ages, people)]
            raise_ = (1 + a.income_growth) ** ((m - 1) // 12)
            pay = sum(p.get("pay", 0) for p, w in zip(people, working) if w) * raise_
            other = (a.other_income or 0) * price
            ss = sum(ret.social_security(p) * price for p, age in zip(people, ages) if age >= p.get("ss_claim_age", 67))
            hprice = (1 + a.inflation + a.health_extra_growth) ** years
            health = sum(ret.health_cost(age, w, any(w2 for j, w2 in enumerate(working) if j != i),
                                         a.private_health_yearly, a.medicare_yearly)
                         for i, (age, w) in enumerate(zip(ages, working))) / 12 * hprice
            living = a.monthly_living * price
            income = pay + other + ss
            save = income - living - paid - health
            # while working: 401(k) contributions (pre-tax + match), after-tax -> Roth, RSU/ESPP shares
            pre += sum(p.get("k401_yearly", 0) for p, w in zip(people, working) if w) / 12
            inv += sum(p.get("roth_yearly", 0) + p.get("stock_yearly", 0) for p, w in zip(people, working) if w) / 12
            oldest = max(ages)
            if (m - 1) % 12 == 0 and oldest >= a.rmd_age and (d := ret.rmd_divisor(oldest)):
                out = pre / d                      # required yearly withdrawal, taxed, rest reinvested
                pre -= out
                inv += out * (1 - a.pretax_tax_rate)
                tax_401k += float((out * a.pretax_tax_rate).mean())
            flow[m - 1] = [income, living, paid, health, pay, ss, other, 0.0]
        elif split:
            income = a.monthly_income * (1 + a.income_growth) ** ((m - 1) // 12)
            living = a.monthly_living * (1 + a.inflation) ** ((m - 1) / 12)
            flow[m - 1, :3] = [income, living, paid]
            save = income - living - paid
        else:
            save = a.monthly_savings * (1 + a.savings_growth) ** ((m - 1) // 12) + freed
        inv = inv * growth[:, m - 1]
        pre = pre * growth[:, m - 1]
        cash = cash * (1 + cash_r)
        if save >= 0:
            inv += save * a.invest_share
            cash += save * (1 - a.invest_share)
        else:
            cash += save
            short = np.minimum(cash, 0)          # overdraw cash first, then taxable investments,
            cash -= short
            inv += short
            if people:                           # then pre-tax 401(k): taxed (and +10% before 59.5)
                need = np.maximum(-inv, 0)
                inv = np.maximum(inv, 0)
                rate = a.pretax_tax_rate + (0.10 if max(ages) < ret.PENALTY_FREE_AGE else 0)
                gross = need / (1 - rate)
                pre -= gross
                tax_401k += float((gross - need).mean())
        if people:
            flow[m - 1, 7] = tax_401k
        val = val * v_growth[:, m - 1, :]
        paths[m] = val.mean(axis=0)
        debt = loan_bal.sum() + revolving
        nw[:, m] = cash + inv + pre + val.sum(axis=1) - debt
        parts[m] = [cash.mean(), (inv + pre).mean(), val.sum(axis=1).mean(), debt]
    return nw, parts, loans, payoff, vt, paths, (flow if split else None)


def run(accts: pd.DataFrame, a: Assumptions, history: pd.DataFrame | None = None,
        trend_overrides: dict | None = None) -> Forecast:
    """history: balance history (for each valuable's own trend and volatility); trend_overrides: {account_id:
    (trend, years)} where a same-items price trend is more honest than the raw value history."""
    dates = pd.date_range(pd.Timestamp.today().normalize(), periods=a.years * 12 + 1, freq="MS")
    deflator = (1 + a.inflation) ** (np.arange(len(dates)) / 12)

    nw, _, loans, _, _, _, _ = _simulate(accts, a, a.simulations, a.investment_volatility, history, trend_overrides)
    p10, p50, p90 = np.percentile(nw, [10, 50, 90], axis=0)
    bands = pd.DataFrame({"date": dates, "p10": p10, "p50": p50, "p90": p90})
    for c in ("p10", "p50", "p90"):
        bands[f"{c}_real"] = bands[c] / deflator

    _, parts, _, payoff, vt, paths, flow = _simulate(accts, a, 1, 0.0, history, trend_overrides)  # expected path
    expected = pd.DataFrame(parts, columns=["cash", "investments", "property", "debt"])
    expected.insert(0, "date", dates)
    expected["net_worth"] = expected[["cash", "investments", "property"]].sum(axis=1) - expected["debt"]
    for c in ("cash", "investments", "property", "debt", "net_worth"):
        expected[f"{c}_real"] = expected[c] / deflator

    milestones = [(dates[m], f"{name} paid off") for name, m in payoff.items()]
    if loans and len(payoff) == len(loans):
        milestones.append((dates[max(payoff.values())], "Debt-free (all loans paid off)"))
    start = float(p50[0])
    for target in (100_000, 250_000, 500_000, 1_000_000, 2_000_000, 3_000_000, 5_000_000, 10_000_000):
        if start < target:
            hit = np.flatnonzero(bands["p50_real"].values >= target)
            if len(hit):
                milestones.append((dates[hit[0]], f"Net worth reaches ${target / 1e6:g}M in today's dollars"
                                   if target >= 1e6 else f"Net worth reaches ${target / 1e3:g}K in today's dollars"))
    milestones.sort(key=lambda x: x[0])
    vt = vt.assign(end=paths[-1] if len(vt) else [], end_real=(paths[-1] / deflator[-1]) if len(vt) else [])
    valuable_paths = pd.DataFrame(paths, columns=list(vt["name"]), index=dates)
    cash_flow = pd.DataFrame()
    if flow is not None:                                  # per year, nominal and in today's dollars
        f = pd.DataFrame(flow, columns=["income", "living", "loans", "health", "pay", "ss", "other", "tax_401k"])
        f["period"] = np.arange(len(f)) // 12                # 12-month periods from now: no partial years
        f["deflator"] = deflator[1:]
        f["month"] = dates[1:]
        cash_flow = f.groupby("period").agg(income=("income", "sum"), living=("living", "sum"),
                                            loans=("loans", "sum"), health=("health", "sum"), pay=("pay", "sum"),
                                            ss=("ss", "sum"), other=("other", "sum"), tax_401k=("tax_401k", "sum"),
                                            deflator=("deflator", "mean"),
                                            months=("income", "size"), first=("month", "min"), last=("month", "max"))
        cash_flow["year"] = cash_flow["last"].dt.year          # labelled by the year each period ends in
        cash_flow["saved"] = cash_flow["income"] - cash_flow["living"] - cash_flow["loans"] - cash_flow["health"]
        for c in ("income", "living", "loans", "health", "pay", "ss", "other", "tax_401k", "saved"):
            cash_flow[f"{c}_real"] = cash_flow[c] / cash_flow["deflator"]
        cash_flow = cash_flow.reset_index()
    return Forecast(bands, expected, loans, milestones, start, vt, valuable_paths, cash_flow)
