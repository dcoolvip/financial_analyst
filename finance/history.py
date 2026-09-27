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
