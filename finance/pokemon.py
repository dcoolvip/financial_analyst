"""Pokemon collection value, read from your Pokemon dashboard's database (read-only - never modified).

    value on a date = sum of the raw price of every card marked owned, from that day's price snapshot
    growth rate     = price change of the cards that were in BOTH the first and the latest snapshot, so
                      adding cards to the collection doesn't look like appreciation

The database is large (3 GB+), so this runs on demand (Accounts -> Refresh from Pokemon dashboard).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pandas as pd

DEFAULT_DB = Path.home() / "Documents" / "Pokemon" / "pokemon_cards.db"
ACCOUNT_NAME = "Pokemon collection"


def db_path() -> Path:
    return Path(os.environ.get("POKEMON_DB") or DEFAULT_DB)


def available() -> bool:
    return db_path().exists()


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def collection_history(path: Path | None = None) -> pd.DataFrame:
    """Month-end value (last snapshot of each month) plus the latest snapshot: columns date, value, cards."""
    with _connect(path or db_path()) as c:
        df = pd.read_sql_query(
            """SELECT p.snap_date AS date, COUNT(*) AS cards,
                      SUM(COALESCE(p.raw, p.tcg, 0)) AS value
               FROM price_history p JOIN cards k ON k.id = p.card_id
               WHERE k.owned = 1 GROUP BY p.snap_date ORDER BY p.snap_date""", c, parse_dates=["date"])
    if df.empty:
        return df
    month_end = df.groupby(df["date"].dt.to_period("M")).tail(1)
    return pd.concat([month_end, df.tail(1)]).drop_duplicates("date").reset_index(drop=True)


def price_growth(path: Path | None = None) -> float | None:
    """Yearly price appreciation of the cards owned in both the first and the latest snapshot."""
    with _connect(path or db_path()) as c:
        first, last = c.execute("SELECT MIN(snap_date), MAX(snap_date) FROM price_history").fetchone()
        if not first or first == last:
            return None
        row = c.execute(
            """SELECT SUM(COALESCE(a.raw, a.tcg)), SUM(COALESCE(b.raw, b.tcg))
               FROM price_history a
               JOIN price_history b ON b.card_id = a.card_id AND b.snap_date = ?
               JOIN cards k ON k.id = a.card_id
               WHERE a.snap_date = ? AND k.owned = 1
                 AND COALESCE(a.raw, a.tcg) > 0 AND COALESCE(b.raw, b.tcg) > 0""", (last, first)).fetchone()
    then, now = row if row else (None, None)
    if not then or not now:
        return None
    days = (pd.Timestamp(last) - pd.Timestamp(first)).days
    return float((now / then) ** (365.25 / days) - 1) if days >= 180 else None


def sync(conn, path: Path | None = None) -> dict:
    """Create/refresh the 'Pokemon collection' account from the dashboard. Safe to run any time."""
    from . import db
    hist = collection_history(path)
    if hist.empty:
        raise ValueError("No owned cards with prices found in the Pokemon dashboard")
    acct = db.get_or_create_account(conn, ACCOUNT_NAME, "Pokemon dashboard", "collectible")
    for r in hist.itertuples():
        db.upsert_balance(conn, acct, r.date.date(), float(r.value), "pokemon dashboard")
    growth = price_growth(path)
    return {"account_id": acct, "value": float(hist["value"].iloc[-1]), "cards": int(hist["cards"].iloc[-1]),
            "as_of": hist["date"].iloc[-1].date(), "points": len(hist), "growth": growth,
            "since": hist["date"].iloc[0].date()}
