"""Market history the Future tab's assumptions come from.

Yearly figures, 1995-2025 (approximate - compiled from memory of the published series; long-run averages are
reliable, a single year may be a few tenths of a percent off; 2025 is the least certain):
  inflation  US CPI-U, December to December (BLS)
  stocks     S&P 500 total return, dividends reinvested
  cash       3-month US Treasury bill, yearly average yield (what savings / money-market accounts track)
  homes      S&P CoreLogic Case-Shiller US National Home Price Index, December to December

The forecast uses long-run averages of these as its starting assumptions, and replays real stock years for
its range of outcomes (instead of made-up random returns).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

#         year  inflation  stocks   cash   homes      (percent)
_ROWS = [(1995, 2.5, 37.6, 5.5, 1.8), (1996, 3.3, 23.0, 5.0, 2.4), (1997, 1.7, 33.4, 5.1, 3.8),
         (1998, 1.6, 28.6, 4.8, 5.9), (1999, 2.7, 21.0, 4.6, 6.6), (2000, 3.4, -9.1, 5.8, 8.9),
         (2001, 1.6, -11.9, 3.4, 6.8), (2002, 2.4, -22.1, 1.6, 9.6), (2003, 1.9, 28.7, 1.0, 10.2),
         (2004, 3.3, 10.9, 1.4, 13.5), (2005, 3.4, 4.9, 3.2, 13.6), (2006, 2.5, 15.8, 4.7, 1.7),
         (2007, 4.1, 5.5, 4.4, -5.4), (2008, 0.1, -37.0, 1.4, -12.0), (2009, 2.7, 26.5, 0.15, -3.9),
         (2010, 1.5, 15.1, 0.14, -4.0), (2011, 3.0, 2.1, 0.05, -3.9), (2012, 1.7, 16.0, 0.09, 6.5),
         (2013, 1.5, 32.4, 0.06, 10.7), (2014, 0.8, 13.7, 0.03, 4.6), (2015, 0.7, 1.4, 0.05, 5.2),
         (2016, 2.1, 12.0, 0.32, 5.3), (2017, 2.1, 21.8, 0.93, 6.2), (2018, 1.9, -4.4, 1.94, 4.6),
         (2019, 2.3, 31.5, 2.06, 3.8), (2020, 1.4, 18.4, 0.37, 10.4), (2021, 7.0, 28.7, 0.05, 18.8),
         (2022, 6.5, -18.1, 2.02, 5.8), (2023, 3.4, 26.3, 5.07, 5.5), (2024, 2.9, 25.0, 4.97, 3.9),
         (2025, 2.7, 17.9, 4.1, 1.5)]

YEARLY = pd.DataFrame(_ROWS, columns=["year", "inflation", "stocks", "cash", "homes"]).set_index("year") / 100
SOURCE = ("US CPI-U (BLS), S&P 500 total return, 3-month Treasury bills, Case-Shiller US National home prices; "
          "yearly 1995-2025, approximate")


def _growth(r: pd.Series) -> float:
    """Average yearly growth, compounded (what $1 actually did), not the simple average of the years."""
    return float(np.prod(1 + r) ** (1 / len(r)) - 1)


def summary(years: int) -> dict:
    """Averages over the last `years` years of history: inflation, stocks (compounded), stock volatility,
    cash (average yield), homes (compounded)."""
    h = YEARLY.tail(years)
    return {"inflation": _growth(h["inflation"]), "stocks": _growth(h["stocks"]),
            "stock_volatility": float(h["stocks"].std(ddof=1)), "cash": float(h["cash"].mean()),
            "homes": _growth(h["homes"]), "from": int(h.index[0]), "to": int(h.index[-1])}


def table() -> pd.DataFrame:
    """Last year, 10-, 20- and 30-year averages for each measure - what the ⓘ and the 'Where these come from'
    table show."""
    rows = []
    last = YEARLY.iloc[-1]
    for key, label in (("inflation", "Inflation"), ("stocks", "Stock returns"), ("cash", "Cash interest (T-bills)"),
                       ("homes", "Home prices")):
        rows.append({"measure": label, "last_year": float(last[key]),
                     **{f"y{n}": summary(n)[key] for n in (10, 20, 30)}})
    return pd.DataFrame(rows)


def stock_years() -> np.ndarray:
    """Real yearly stock returns to replay in the forecast's simulations."""
    return YEARLY["stocks"].to_numpy(dtype=float)


# Apple (AAPL) total return per year, 2011-2025 - approximate, same caveats as above. Used only for the SHAPE of
# a single big company's years (how far it swings, see single_stock_swings), never its level: nobody should plan
# on 27%/yr for decades.
APPLE = pd.Series([25.6, 32.6, 8.1, 40.0, -3.0, 12.5, 48.5, -5.4, 89.0, 82.3, 34.6, -26.4, 49.0, 30.7, 9.0],
                  index=range(2011, 2026)) / 100


# What one dominant company's stock might do from here, as a typical yearly return per coming year. Stylized from
# what happened to past #1 US companies after their peak (approximate, total return with dividends):
#   IBM after ~1985: fell ~75% by 1993, then recovered; roughly 4-5%/yr over ~40 years
#   GE after 2000: fell ~85-90% (2009, 2018), partial recovery after the split-up; roughly 1%/yr over 25 years
#   GM after the 1960s: decades of decline, then bankruptcy in 2009 - shareholders got $0
# After a stylized episode ends, the stock grows with the economy again.
GROWS_WITH_ECONOMY = 0.074     # ~4.5%/yr nominal GDP growth + ~2.9%/yr returned via buybacks and dividends
SINGLE_STOCK_PATHS = {
    "mix": "Weighted mix of the paths below (recommended)",
    "economy": "Grows with the economy (~7.4%/yr)",
    "market": "Keeps up with the stock market",
    "ibm": "Like IBM after 1985: -75% over 6 years, then recovers",
    "ge": "Like GE after 2000: -85% over 9 years, ~1%/yr over 25 years",
    "gm": "Like GM after the 1960s: declines, then $0 after 25 years",
}


def single_stock_path(kind: str, years: int, market_return: float) -> np.ndarray:
    """Typical yearly return of the single stock for each coming year."""
    tail = lambda n: [GROWS_WITH_ECONOMY] * max(n, 0)          # noqa: E731
    if kind == "mix":                                           # the typical single path of a mix: the middle one
        kind = "ibm"
    if kind == "market":
        path = [market_return] * years
    elif kind == "ibm":
        path = [-0.20] * 6 + [0.091] * 31
    elif kind == "ge":
        path = [-0.19] * 9 + [0.143] * 16
    elif kind == "gm":
        path = [-0.05] * 24 + [-1.0]                            # bankruptcy in year 25; nothing after
        return np.array((path + [0.0] * years)[:years])
    else:
        path = []
    return np.array((path + tail(years - len(path)))[:years])


def single_stock_swings() -> np.ndarray:
    """Apple's real yearly swings around its typical path: factors whose compounded average is 1, so a
    simulated year is typical-path x a real Apple year's deviation."""
    r = 1 + APPLE.to_numpy(dtype=float)
    return r / (np.prod(r) ** (1 / len(r)))
