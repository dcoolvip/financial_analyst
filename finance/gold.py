"""Gold (or silver) value history = how much you hold x the market price on each date.

    value = grams x purity / 31.1035 (grams per troy ounce) x price per troy ounce

Prices come from a downloaded price-history CSV (e.g. stooq.com XAUUSD monthly: Date,Open,High,Low,Close),
read locally - any CSV with a date column and a close/price column works.
"""
from __future__ import annotations

import io

import pandas as pd

GRAMS_PER_TROY_OUNCE = 31.1035
KARAT_PURITY = {24: 0.999, 22: 0.916, 18: 0.750, 14: 0.585}


def read_prices(content: bytes) -> pd.Series:
    """Price per troy ounce by date, from a price-history CSV."""
    df = pd.read_csv(io.BytesIO(content))
    cols = {c.lower().strip(): c for c in df.columns}
    date = next((cols[c] for c in cols if "date" in c), None)
    price = next((cols[c] for c in ("close", "price", "usd", "value", "adj close") if c in cols), None)
    if date is None or price is None:
        raise ValueError("Price file needs a date column and a close/price column")
    s = pd.Series(pd.to_numeric(df[price].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce").to_numpy(),
                  index=pd.to_datetime(df[date], errors="coerce"))
    s = s[s.index.notna()].dropna().sort_index()
    if s.empty:
        raise ValueError("No prices found in the file")
    return s


def history(grams: float, prices: pd.Series, since=None, purity: float = 0.999) -> pd.DataFrame:
    """Month-end value (date, value) of the metal held, from `since` (when you got it) onward."""
    ounces = grams * purity / GRAMS_PER_TROY_OUNCE
    monthly = prices.resample("ME").last().dropna()
    if prices.index[-1] > monthly.index[-1] or prices.index[-1] not in monthly.index:
        monthly.index = list(monthly.index[:-1]) + [prices.index[-1]]     # latest point on its real date
    if since is not None:
        monthly = monthly[monthly.index >= pd.Timestamp(since)]
    return pd.DataFrame({"date": [d.date() for d in monthly.index], "value": (monthly * ounces).round(0).to_numpy()})
