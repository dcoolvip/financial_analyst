"""Net-worth projection.

Approach (the same idea ProjectionLab / Empower use, kept small):
  * Investments follow random yearly-ish returns -> run many simulations and
    report a range (10th / 50th / 90th percentile), not one false-precision line.
  * Cash earns a steady yield; homes appreciate; vehicles depreciate.
  * Each loan is amortized month by month. When it's paid off, its payment is
    redirected to savings (your spending doesn't change, the bill just ends).
  * Monthly savings rise with inflation (raises roughly keep pace).
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


def valuables_table(accts: pd.DataFrame, a: Assumptions) -> pd.DataFrame:
    """Each home, car, collectible, gold... with the yearly growth rate the forecast uses for it.
    Your own rate (Accounts table) wins; otherwise the Home/Car sliders or the type's default."""
    from .assets import DEFAULT_GROWTH, growth_rate
    from .db import VALUABLE_TYPES
    rows = []
    for r in accts[accts["type"].isin(VALUABLE_TYPES)].itertuples():
        rate, source = growth_rate(r.type, r.rate)
        if source == "default" and r.type == "property":
            rate = a.home_appreciation
        elif source == "default" and r.type == "vehicle":
            rate = -a.vehicle_depreciation
        rows.append({"id": r.id, "name": r.name, "type": r.type, "value": float(r.balance or 0.0),
                     "rate": rate, "source": source})
    return pd.DataFrame(rows, columns=["id", "name", "type", "value", "rate", "source"])


def _simulate(accts: pd.DataFrame, a: Assumptions, sims: int, sigma: float):
    months = a.years * 12
    bal = accts.assign(balance=accts["balance"].fillna(0.0))
    by_type = bal.groupby("type")["balance"].sum()
    cash0 = by_type.reindex(["checking", "savings"]).fillna(0).sum()
    inv0 = by_type.reindex(["brokerage", "retirement"]).fillna(0).sum()
    vt = valuables_table(bal, a)                 # every valuable grows at its own rate
    val = vt["value"].to_numpy(dtype=float)
    val_r = ((1 + vt["rate"].to_numpy(dtype=float)) ** (1 / 12) - 1) if len(vt) else np.zeros(0)
    loans, revolving = build_loans(bal)

    rng = np.random.default_rng(a.seed)
    mu = np.log1p(a.investment_return) / 12 - sigma**2 / 24
    growth = np.exp(rng.normal(mu, sigma / np.sqrt(12), size=(sims, months)))
    cash_r = (1 + a.cash_yield) ** (1 / 12) - 1

    cash = np.full(sims, cash0, dtype=float)
    inv = np.full(sims, inv0, dtype=float)
    loan_bal = np.array([l.balance for l in loans], dtype=float)
    loan_rate = np.array([l.apr / 12 for l in loans], dtype=float)
    loan_pay = np.array([l.payment for l in loans], dtype=float)
    payoff: dict[str, int] = {}

    nw = np.empty((sims, months + 1))
    parts = np.empty((months + 1, 4))  # cash, investments, property & valuables, debt (sim 0 / mean)
    paths = np.empty((months + 1, len(val)))
    paths[0] = val
    debt = loan_bal.sum() + revolving
    nw[:, 0] = cash + inv + val.sum() - debt
    parts[0] = [cash.mean(), inv.mean(), val.sum(), debt]

    for m in range(1, months + 1):
        # loans: accrue interest, pay, and free up the payment once cleared
        active = loan_bal > 0
        loan_bal = np.where(active, np.maximum(loan_bal * (1 + loan_rate) - loan_pay, 0), 0)
        for i in np.flatnonzero(active & (loan_bal == 0)):
            payoff[loans[i].name] = m
        freed = loan_pay[~active].sum()          # loans already cleared before this month

        save = a.monthly_savings * (1 + a.savings_growth) ** ((m - 1) // 12) + freed
        inv = inv * growth[:, m - 1]
        cash = cash * (1 + cash_r)
        if save >= 0:
            inv += save * a.invest_share
            cash += save * (1 - a.invest_share)
        else:
            cash += save
            short = np.minimum(cash, 0)          # overdraw cash first, then investments
            cash -= short
            inv += short
        val = val * (1 + val_r)
        paths[m] = val
        debt = loan_bal.sum() + revolving
        nw[:, m] = cash + inv + val.sum() - debt
        parts[m] = [cash.mean(), inv.mean(), val.sum(), debt]
    return nw, parts, loans, payoff, vt, paths


def run(accts: pd.DataFrame, a: Assumptions) -> Forecast:
    dates = pd.date_range(pd.Timestamp.today().normalize(), periods=a.years * 12 + 1, freq="MS")
    deflator = (1 + a.inflation) ** (np.arange(len(dates)) / 12)

    nw, _, loans, _, _, _ = _simulate(accts, a, a.simulations, a.investment_volatility)
    p10, p50, p90 = np.percentile(nw, [10, 50, 90], axis=0)
    bands = pd.DataFrame({"date": dates, "p10": p10, "p50": p50, "p90": p90})
    for c in ("p10", "p50", "p90"):
        bands[f"{c}_real"] = bands[c] / deflator

    _, parts, _, payoff, vt, paths = _simulate(accts, a, 1, 0.0)   # expected-return path for the breakdown
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
    return Forecast(bands, expected, loans, milestones, start, vt, valuable_paths)
