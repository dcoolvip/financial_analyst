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


# --- the monthly price: fetched when the dashboard opens ---------------------------------------------------------
# A gold account's amount and purity come from its name, e.g. "Gold (324 g, 22k)". Once a month (the first time the
# dashboard opens in a month with no value yet), today's price is fetched and the value saved; if no price source
# answers, the old value stays and it tries again the next day.

PRICE_SOURCES = [
    ("stooq.com", "https://stooq.com/q/l/?s=xauusd&f=sd2t2c&h&e=csv"),                         # Symbol,Date,Time,Close
    ("Yahoo Finance", "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1d"),
]


def holding(name: str) -> tuple[float, float] | None:
    """(grams, purity) from an account name like "Gold (324 g, 22k)"."""
    import re
    g = re.search(r"([\d.]+)\s*g\b", name or "")
    k = re.search(r"\b(\d{2})\s*k\b", name or "", re.I)
    if not g:
        return None
    return float(g.group(1)), KARAT_PURITY.get(int(k.group(1)), 0.999) if k else 0.999


def parse_price(source: str, body: bytes) -> float | None:
    """Price per troy ounce from a source's answer (None if it isn't a sensible price)."""
    import json
    try:
        if source == "stooq.com":
            price = float(pd.read_csv(io.BytesIO(body))["Close"].iloc[-1])
        else:
            price = float(json.loads(body)["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None
    return price if 500 < price < 50_000 else None


def fetch_price(timeout: float = 10) -> tuple[float, str] | None:
    import urllib.request
    import os
    if os.environ.get("FINANCE_NO_BANK_PULL"):                  # tests: never on the internet
        return None
    for source, url in PRICE_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if (price := parse_price(source, r.read())) is not None:
                    return price, source
        except Exception:
            continue
    return None


def monthly_update(conn, today=None, fetch=fetch_price) -> list[str]:
    """Save today's value for each gold account with no value yet this month. Returns what happened."""
    from datetime import date
    from finance import db
    today = today or date.today()
    accts = db.accounts(conn)
    due = []
    for r in accts[(accts["type"] == "precious_metal") & accts["name"].str.contains("gold", case=False)].itertuples():
        last = conn.execute("SELECT MAX(date) FROM balances WHERE account_id = ?", (r.id,)).fetchone()[0]
        if (h := holding(r.name)) and r.active and not (last and last >= today.replace(day=1).isoformat()):
            due.append((r, h))
    if not due or db.get_setting(conn, "gold_price_tried") == today.isoformat():
        return []
    db.set_setting(conn, "gold_price_tried", today.isoformat())              # at most one try a day
    got = fetch()
    if got is None:
        return ["Couldn't get today's gold price - the gold value stays as it was; trying again tomorrow."]
    price, source = got
    out = []
    for r, (grams, purity) in due:
        value = round(grams * purity / GRAMS_PER_TROY_OUNCE * price, 0)
        db.upsert_balance(conn, int(r.id), today, value,
                          f"Gold price ${price:,.0f}/oz ({source}) x {grams:g} g x {purity} purity")
        out.append(f"{r.name}: ${value:,.0f} at today's gold price")
    return out
