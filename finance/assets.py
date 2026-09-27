"""Property & valuables: homes, cars, collectibles, gold... - how each is expected to grow.

Every valuable has a yearly growth rate: + appreciates, - depreciates. It's the account's own rate if you
set one (Accounts table), otherwise a default for its type. Where the asset has enough value history of its
own, its actual trend is shown alongside, so you can adopt it.
"""
from __future__ import annotations

import pandas as pd

# Long-run, conservative defaults (nominal, per year)
DEFAULT_GROWTH = {
    "property": 0.035,        # US home prices, long-run average
    "vehicle": -0.15,         # typical depreciation
    "collectible": 0.03,      # highly variable; set your own
    "precious_metal": 0.04,   # gold, long-run
    "other_asset": 0.0,
}
MIN_TREND_DAYS = 180          # need at least ~6 months of history to call it a trend


def growth_rate(account_type: str, own_rate) -> tuple[float, str]:
    """(yearly rate, where it came from: 'yours' | 'default')"""
    if own_rate is not None and not pd.isna(own_rate):
        return float(own_rate), "yours"
    return DEFAULT_GROWTH.get(account_type, 0.0), "default"


def trend(history: pd.DataFrame) -> float | None:
    """Yearly growth implied by an asset's own value history (first -> last), or None if too short."""
    h = history.dropna(subset=["balance"]).sort_values("date")
    h = h[h["balance"] > 0]
    if len(h) < 2:
        return None
    days = (pd.Timestamp(h["date"].iloc[-1]) - pd.Timestamp(h["date"].iloc[0])).days
    if days < MIN_TREND_DAYS:
        return None
    return float((h["balance"].iloc[-1] / h["balance"].iloc[0]) ** (365.25 / days) - 1)
