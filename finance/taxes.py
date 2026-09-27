"""US federal + California income tax for a married couple filing jointly - used for the retirement years.

2025 figures (approximate, from memory of the published tables; the forecast indexes them with inflation). Kept
simple on purpose: no itemizing, no credits, no AMT; rental income taxed in full (depreciation ignored).
  Ordinary income: 401(k) withdrawals and Roth conversions, rent, interest, 85% of Social Security (federal only)
  Qualified dividends and long-term gains: 0% / 15% / 20% federal (stacked on top of ordinary income), plus the
  3.8% net investment income tax over $250K (not indexed); California taxes them as ordinary income.
"""
from __future__ import annotations

import numpy as np

FED_STANDARD = 31_500.0
FED_BRACKETS = [(23_850, 0.10), (96_950, 0.12), (206_700, 0.22), (394_600, 0.24), (501_050, 0.32),
                (751_600, 0.35), (float("inf"), 0.37)]          # (top of taxable income, rate)
FED_GAINS = [(96_700, 0.0), (600_050, 0.15), (float("inf"), 0.20)]
NIIT_THRESHOLD, NIIT_RATE = 250_000.0, 0.038
CA_STANDARD = 11_080.0
CA_BRACKETS = [(21_512, 0.01), (50_998, 0.02), (80_490, 0.04), (111_732, 0.06), (141_212, 0.08), (721_318, 0.093),
               (865_574, 0.103), (1_442_628, 0.113), (float("inf"), 0.123)]
CA_MENTAL_HEALTH = (1_000_000.0, 0.01)                          # +1% over $1M, not indexed
SS_TAXABLE = 0.85


def _brackets(income, table, scale):
    """Tax from progressive brackets (vectorized over simulations)."""
    income = np.maximum(np.asarray(income, dtype=float), 0)
    tax, low = np.zeros_like(income), 0.0
    for top, rate in table:
        top = top * scale
        tax += np.clip(income - low, 0, top - low) * rate
        low = top
    return tax


def bracket_top(rate: float, scale: float = 1.0) -> float:
    """Top of the federal bracket with this rate, as taxable income."""
    return next(top for top, r in FED_BRACKETS if abs(r - rate) < 1e-9) * scale


def income_tax(ordinary, qualified, social_security, scale: float = 1.0):
    """Federal + California tax for a year. scale = price level vs 2025 (brackets rise with inflation)."""
    ordinary, qualified = np.asarray(ordinary, dtype=float), np.asarray(qualified, dtype=float)
    fed_ord = np.maximum(ordinary + SS_TAXABLE * np.asarray(social_security, dtype=float) - FED_STANDARD * scale, 0)
    fed = _brackets(fed_ord, FED_BRACKETS, scale)
    # qualified income sits on top of ordinary income in the 0% / 15% / 20% brackets
    top = fed_ord + np.maximum(qualified, 0)
    fed += _brackets(top, FED_GAINS, scale) - _brackets(fed_ord, FED_GAINS, scale)
    magi = ordinary + SS_TAXABLE * np.asarray(social_security, dtype=float) + qualified
    fed += NIIT_RATE * np.minimum(np.maximum(qualified, 0), np.maximum(magi - NIIT_THRESHOLD, 0))
    ca_income = np.maximum(ordinary + qualified - CA_STANDARD * scale, 0)          # no tax on Social Security
    ca = _brackets(ca_income, CA_BRACKETS, scale) + CA_MENTAL_HEALTH[1] * np.maximum(ca_income - CA_MENTAL_HEALTH[0], 0)
    return fed + ca


def room_in_bracket(rate: float, ordinary, social_security, scale: float = 1.0):
    """How much more ordinary income (a 401(k) withdrawal or Roth conversion) fits before leaving the federal
    bracket with this rate."""
    used = np.asarray(ordinary, dtype=float) + SS_TAXABLE * np.asarray(social_security, dtype=float) - FED_STANDARD * scale
    return np.maximum(bracket_top(rate, scale) - used, 0)
