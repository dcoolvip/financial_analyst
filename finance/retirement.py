"""Retirement pieces for the forecast: ages, Social Security, healthcare by age, 401(k) withdrawal rules, and
whose paycheck is whose. The household itself (names, birth months, pay patterns, estimates) lives in the
database (setting 'household_people'), never in the code.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

FULL_RETIREMENT_AGE = 67          # Social Security, for anyone born 1960 or later
MEDICARE_AGE = 65
PENALTY_FREE_AGE = 59.5           # 401(k) withdrawals before this pay an extra 10%

# IRS Uniform Lifetime Table (distribution period by age) - the required yearly 401(k) withdrawal is balance / this
_RMD = {72: 27.4, 73: 26.5, 74: 25.5, 75: 24.6, 76: 23.7, 77: 22.9, 78: 22.0, 79: 21.1, 80: 20.2, 81: 19.4,
        82: 18.5, 83: 17.7, 84: 16.8, 85: 16.0, 86: 15.2, 87: 14.4, 88: 13.7, 89: 12.9, 90: 12.2, 91: 11.5,
        92: 10.8, 93: 10.1, 94: 9.5, 95: 8.9, 96: 8.4, 97: 7.8, 98: 7.3, 99: 6.8, 100: 6.4}


def age_on(born: str, when: pd.Timestamp) -> float:
    """Age in years on a date, from a birth month like '1979-04'."""
    b = pd.Timestamp(f"{born}-01")
    return (when.year - b.year) + (when.month - b.month) / 12


def social_security_factor(claim_age: float) -> float:
    """Share of the full-retirement-age benefit when claiming at this age: 70% at 62, 100% at 67, 124% at 70."""
    months = round((claim_age - FULL_RETIREMENT_AGE) * 12)
    if months >= 0:
        return 1 + min(months, 36) * (2 / 3) / 100          # +8% a year, up to 70
    early = -months
    return 1 - min(early, 36) * (5 / 9) / 100 - max(early - 36, 0) * (5 / 12) / 100


def social_security(person: dict, claim_age: float | None = None) -> float:
    """Monthly benefit (today's dollars) when claiming at this age. Uses the person's own SSA estimates
    (ss_table {62: .., 67: .., 70: ..}, interpolated between ages) when there are any; else the standard
    early/late adjustment on their full-retirement-age amount."""
    age = claim_age if claim_age is not None else person.get("ss_claim_age", FULL_RETIREMENT_AGE)
    table = {float(k): float(v) for k, v in (person.get("ss_table") or {}).items()}
    if len(table) >= 2:
        ages = sorted(table)
        return float(np.interp(age, ages, [table[a] for a in ages]))     # held flat outside 62-70
    return float(person.get("ss_monthly", 0)) * social_security_factor(age)


def rmd_divisor(age: float) -> float | None:
    a = int(age)
    return _RMD.get(min(a, 100)) if a >= 72 else None


def health_cost(age: float, working: bool, partner_working: bool, private_yearly: float, medicare_yearly: float) -> float:
    """Yearly healthcare cost for one person in today's dollars: employer plan while they (or their partner)
    work, private insurance if retired before 65 with nobody working, Medicare (+ supplement, IRMAA) from 65."""
    if age >= MEDICARE_AGE:
        return medicare_yearly
    if working or partner_working:
        return 0.0
    return private_yearly


def _matches(rule: dict, description: str, account: str) -> bool:
    return (re.search(rule["pattern"], description, re.IGNORECASE) is not None
            and (not rule.get("account") or rule["account"] == account))


def pay_owner(people: list[dict], description: str, account: str) -> str | None:
    for p in people:
        if any(_matches(r, description, account) for r in p.get("pay", [])):
            return p["name"]
    return None


def pay_by_person(enriched: pd.DataFrame, people: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Average monthly take-home pay per person over [start, end), from the paycheck patterns."""
    t = enriched[(enriched["date"] >= start) & (enriched["date"] < end) & (enriched["amount"] > 0)]
    months = max(1, round((end - start).days / 30.44))
    owners = [pay_owner(people, d, a) for d, a in zip(t["description"], t["account"])]
    t = t.assign(owner=owners)
    return {p["name"]: float(t.loc[t["owner"] == p["name"], "amount"].sum() / months) for p in people}
