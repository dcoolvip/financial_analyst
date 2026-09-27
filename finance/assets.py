"""Property & valuables: homes, cars, collectibles, gold... - how each is expected to grow, from its history.

Rate = your asset's own historical trend blended with the long-run rate for its kind, where history earns
weight as it gets longer (credibility weighting):   weight = years / (years + CREDIBILITY_YEARS)
    1 year of history -> ~17% weight,  5 years -> 50%,  10 years -> ~67%
so a hot 15 months (Pokemon +81%/yr) informs the outlook without being extrapolated for a decade, while a
long, steady record mostly speaks for itself. A rate you type in the Accounts table always wins.

Uncertainty (yearly volatility) also comes from history - how much the value actually moves month to month -
and feeds the forecast's range. Too little history falls back to a typical figure for the kind of asset.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Long-run anchors (nominal, per year) - what history is blended toward
LONG_RUN_GROWTH = {
    "property": 0.035,        # US home prices over decades
    "vehicle": -0.15,         # typical depreciation
    "collectible": 0.03,      # trading cards / art: highly variable, conservative anchor
    "precious_metal": 0.04,   # gold over decades
    "other_asset": 0.0,
}
DEFAULT_GROWTH = LONG_RUN_GROWTH   # (older name, kept for callers)
TYPICAL_VOLATILITY = {"property": 0.05, "vehicle": 0.05, "collectible": 0.25, "precious_metal": 0.15,
                      "other_asset": 0.10}
CREDIBILITY_YEARS = 5.0
MIN_TREND_DAYS = 180          # need ~6 months before history counts at all
MIN_VOL_POINTS = 6            # month-to-month moves needed to measure volatility


def _clean(history: pd.DataFrame) -> pd.DataFrame:
    h = history.dropna(subset=["balance"]).sort_values("date")
    return h[h["balance"] > 0]


def trend(history: pd.DataFrame) -> float | None:
    """Yearly growth fitted across ALL recorded values (log-linear), or None with under ~6 months of history."""
    h = _clean(history)
    if len(h) < 2:
        return None
    t = (pd.to_datetime(h["date"]) - pd.Timestamp(h["date"].iloc[0])).dt.days.to_numpy() / 365.25
    if t[-1] * 365.25 < MIN_TREND_DAYS:
        return None
    slope = np.polyfit(t, np.log(h["balance"].to_numpy(dtype=float)), 1)[0]
    return float(np.expm1(slope))


def years_of_history(history: pd.DataFrame) -> float:
    h = _clean(history)
    return 0.0 if len(h) < 2 else (pd.Timestamp(h["date"].iloc[-1]) - pd.Timestamp(h["date"].iloc[0])).days / 365.25


def volatility(history: pd.DataFrame) -> float | None:
    """Yearly volatility from month-to-month changes in value, or None if there aren't enough."""
    h = _clean(history)
    if len(h) < MIN_VOL_POINTS + 1:
        return None
    s = h.set_index(pd.to_datetime(h["date"]))["balance"].resample("ME").last().dropna()
    moves = np.diff(np.log(s.to_numpy(dtype=float)))
    return float(np.std(moves, ddof=1) * np.sqrt(12)) if len(moves) >= MIN_VOL_POINTS else None


def outlook(account_type: str, own_rate, history: pd.DataFrame, long_run: float | None = None,
            trend_override: tuple[float, float] | None = None) -> dict:
    """How one asset is expected to grow: {rate, vol, source, weight, trend, years, long_run}.
    trend_override = (trend, years) for assets whose value history mixes in purchases (e.g. a card
    collection that grew), where a same-items price trend is the honest number."""
    base = LONG_RUN_GROWTH.get(account_type, 0.0) if long_run is None else long_run
    if trend_override:                                   # (trend, years[, volatility]) from a same-items index
        hist_trend, years = trend_override[0], trend_override[1]
        vol = (trend_override[2] if len(trend_override) > 2 and trend_override[2] else None)
    else:
        hist_trend, years, vol = trend(history), years_of_history(history), volatility(history)
    vol = vol or TYPICAL_VOLATILITY.get(account_type, 0.10)
    if own_rate is not None and not pd.isna(own_rate):
        return {"rate": float(own_rate), "vol": vol, "source": "yours", "weight": None,
                "trend": hist_trend, "years": years, "long_run": base}
    if hist_trend is None:
        return {"rate": base, "vol": vol, "source": "long-run", "weight": 0.0,
                "trend": None, "years": years, "long_run": base}
    w = years / (years + CREDIBILITY_YEARS)
    return {"rate": w * hist_trend + (1 - w) * base, "vol": vol, "source": "history", "weight": w,
            "trend": hist_trend, "years": years, "long_run": base}


def growth_rate(account_type: str, own_rate) -> tuple[float, str]:
    """Rate without history (kept for callers): yours, else the long-run anchor."""
    o = outlook(account_type, own_rate, pd.DataFrame(columns=["date", "balance"]))
    return o["rate"], ("yours" if o["source"] == "yours" else "default")


def explain(o: dict) -> str:
    if o["source"] == "yours":
        return "your rate"
    if o["source"] == "long-run":
        return f"long-run {o['long_run']:+.1%} (not enough history yet)"
    return (f"your history {o['trend']:+.1%}/yr over {o['years']:.1f} yrs ({o['weight']:.0%} weight) "
            f"+ long-run {o['long_run']:+.1%}")
