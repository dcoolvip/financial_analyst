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


# --- real crash episodes, replayed year by year -----------------------------------------------------------------
# Each: yearly stocks (total return), inflation, cash (T-bill yield), homes, dividend change - all in percent.
# 2000-2013 come from YEARLY above; 1929-45, 1973-82 and Japan 1990-2019 are approximate (compiled from memory of
# the published series: S&P total returns / Nikkei, CPI, T-bill yields, US and Japanese home / land prices,
# S&P dividends). Japan: Nikkei 225 with ~1% dividends, Japanese CPI, BOJ rates, residential land prices.
_DEPRESSION = [  # year, stocks, inflation, cash, homes, dividends
    (1929, -8.4, 0.6, 4.4, -2, 5), (1930, -25.1, -6.4, 2.3, -5, -5), (1931, -43.8, -9.3, 1.4, -8, -20),
    (1932, -8.6, -10.3, 0.9, -12, -35), (1933, 50.0, 0.8, 0.3, -5, -15), (1934, -1.2, 1.5, 0.2, 3, 5),
    (1935, 46.7, 3.0, 0.2, 4, 10), (1936, 31.9, 1.4, 0.2, 5, 30), (1937, -35.3, 2.9, 0.3, 3, 10),
    (1938, 29.3, -2.8, 0.0, -1, -30), (1939, -1.1, 0.0, 0.0, 1, 10), (1940, -10.7, 0.7, 0.0, 2, 5),
    (1941, -12.8, 9.9, 0.1, 5, 5), (1942, 19.2, 9.0, 0.3, 6, -5), (1943, 25.1, 3.0, 0.4, 8, 5),
    (1944, 19.0, 2.3, 0.3, 9, 5), (1945, 35.8, 2.2, 0.3, 10, 5)]
_STAGFLATION = [
    (1973, -14.7, 8.7, 7.0, 9, 5), (1974, -26.5, 12.3, 7.9, 10, 7), (1975, 37.2, 6.9, 5.8, 8, 3),
    (1976, 23.8, 4.9, 5.0, 9, 10), (1977, -7.2, 6.7, 5.3, 14, 12), (1978, 6.6, 9.0, 7.2, 13, 10),
    (1979, 18.4, 13.3, 10.0, 12, 12), (1980, 32.4, 12.5, 11.4, 7, 7), (1981, -4.9, 8.9, 14.0, 5, 6),
    (1982, 21.4, 3.8, 10.6, 2, 3)]
_JAPAN = [
    (1990, -39, 3.1, 7.5, 7, 3), (1991, -3, 3.3, 7.0, -3, 2), (1992, -26, 1.7, 4.0, -6, 0), (1993, 3, 1.3, 2.5, -6, -2),
    (1994, 13, 0.7, 2.0, -5, -2), (1995, 1, -0.1, 1.0, -5, 0), (1996, -2, 0.1, 0.5, -4, 1), (1997, -21, 1.7, 0.4, -4, 2),
    (1998, -9, 0.7, 0.3, -5, -3), (1999, 37, -0.3, 0.1, -6, -2), (2000, -27, -0.7, 0.2, -6, 2), (2001, -23, -0.7, 0.1, -6, 0),
    (2002, -18, -0.9, 0.0, -7, -2), (2003, 25, -0.3, 0.0, -7, 5), (2004, 8, 0.0, 0.0, -6, 10), (2005, 42, -0.3, 0.0, -4, 15),
    (2006, 8, 0.2, 0.2, -2, 15), (2007, -10, 0.1, 0.5, 0, 10), (2008, -41, 1.4, 0.5, -2, 0), (2009, 20, -1.4, 0.1, -4, -15),
    (2010, -2, -0.7, 0.1, -3, 0), (2011, -16, -0.3, 0.1, -3, 5), (2012, 24, 0.0, 0.1, -2, 5), (2013, 58, 0.4, 0.1, -1, 10),
    (2014, 9, 2.7, 0.1, 0, 15), (2015, 10, 0.8, 0.1, 0, 15), (2016, 2, -0.1, 0.0, 0, 5), (2017, 21, 0.5, 0.0, 1, 10),
    (2018, -11, 1.0, 0.0, 1, 10), (2019, 20, 0.5, 0.0, 1, 5)]
_US_DIVIDENDS = {2000: 2, 2001: -3, 2002: 1, 2003: 8, 2004: 10, 2005: 12, 2006: 11, 2007: 10, 2008: -1, 2009: -21,
                 2010: 1, 2011: 16, 2012: 18, 2013: 12}


def _from_yearly(first: int, last: int) -> list:
    y = YEARLY.loc[first:last]
    return [(yr, r.stocks * 100, r.inflation * 100, r.cash * 100, r.homes * 100, _US_DIVIDENDS.get(yr, 5))
            for yr, r in y.iterrows()]


EPISODES = {   # key: (name, what happened, rows)
    "lost_decade": ("Lost decade 2000–2012", "dot-com bust then 2008: stocks roughly flat for 13 years with two "
                    "~50% falls; homes boom then fall", _from_yearly(2000, 2012)),
    "crisis_2008": ("Financial crisis 2007–2013", "stocks −37% in 2008, homes −27% over 2007–11, dividends cut "
                    "about 20%, rates near 0%", _from_yearly(2007, 2013)),
    "stagflation": ("Stagflation 1973–1982", "stocks −41% in 1973–74 while inflation ran 7–13% a year: costs "
                    "rose fast as savings fell", _STAGFLATION),
    "depression": ("Great Depression 1929–1945", "stocks −85% by 1932, prices fell ~25%, near-zero rates, a second "
                   "crash in 1937; about 15 years to recover", _DEPRESSION),
    "japan": ("Japan 1990–2019", "stocks −80% and land prices falling for 15 years, near-zero inflation and rates - "
              "no full recovery in 30 years", _JAPAN),
}
EPISODE_SOURCE = ("Yearly stock total returns, inflation, T-bill rates, home prices and dividend changes; 2000-2013 "
                  "from the history above, 1929-45, 1973-82 and Japan approximate")


def episode(key: str) -> pd.DataFrame:
    """The episode's years as fractions: stocks, inflation, cash, homes, dividends (change)."""
    rows = EPISODES[key][2]
    return pd.DataFrame(rows, columns=["year", "stocks", "inflation", "cash", "homes", "dividends"]).set_index("year") / 100
