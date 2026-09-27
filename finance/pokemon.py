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


COMPLETE = 0.95     # a snapshot counts only if it priced at least 95% of the month's best coverage


def _snapshots(c) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT p.snap_date AS date, COUNT(*) AS cards, SUM(COALESCE(p.raw, p.tcg, 0)) AS value
           FROM price_history p JOIN cards k ON k.id = p.card_id
           WHERE k.owned = 1 GROUP BY p.snap_date ORDER BY p.snap_date""", c, parse_dates=["date"])


def _complete_month_ends(snaps: pd.DataFrame) -> pd.DataFrame:
    """Per month, the latest snapshot that priced (nearly) all your cards - partial runs are skipped,
    so a snapshot that only priced 25 cards can't look like the collection crashed."""
    if snaps.empty:
        return snaps
    month = snaps["date"].dt.to_period("M")
    full = snaps[snaps["cards"] >= COMPLETE * snaps.groupby(month)["cards"].transform("max")]
    # also skip months whose best run is far below the collection size around it (a partial month overall)
    full = full[full["cards"] >= COMPLETE * full["cards"].cummax()]
    picked = full.groupby(full["date"].dt.to_period("M")).tail(1)
    latest = snaps.tail(1)
    if latest["cards"].iloc[0] >= COMPLETE * full["cards"].max():
        picked = pd.concat([picked, latest]).drop_duplicates("date")
    return picked.reset_index(drop=True)


def collection_history(path: Path | None = None) -> pd.DataFrame:
    """Month-end value of your owned cards from complete snapshots: columns date, value, cards."""
    with _connect(path or db_path()) as c:
        return _complete_month_ends(_snapshots(c))


def price_index(path: Path | None = None, dates: list | None = None) -> pd.DataFrame:
    """Same-cards price index between consecutive month-end snapshots: each step compares only cards priced
    on both dates, so buying cards (or a partial snapshot) never shows up as a price move."""
    with _connect(path or db_path()) as c:
        if dates is None:
            dates = list(_complete_month_ends(_snapshots(c))["date"])
        idx, level = [], 1.0
        for i, d in enumerate(dates):
            if i:
                then, now = c.execute(
                    """SELECT SUM(COALESCE(a.raw, a.tcg)), SUM(COALESCE(b.raw, b.tcg))
                       FROM price_history a JOIN price_history b ON b.card_id = a.card_id AND b.snap_date = ?
                       JOIN cards k ON k.id = a.card_id
                       WHERE a.snap_date = ? AND k.owned = 1
                         AND COALESCE(a.raw, a.tcg) > 0 AND COALESCE(b.raw, b.tcg) > 0""",
                    (d.strftime("%Y-%m-%d"), dates[i - 1].strftime("%Y-%m-%d"))).fetchone()
                if then and now:
                    level *= now / then
            idx.append((d, level))
    return pd.DataFrame(idx, columns=["date", "index"])


def price_stats(index: pd.DataFrame) -> dict | None:
    """Yearly trend and volatility of the same-cards price index."""
    from .assets import trend, volatility
    h = index.rename(columns={"index": "balance"})
    t = trend(h)
    if t is None:
        return None
    years = (index["date"].iloc[-1] - index["date"].iloc[0]).days / 365.25
    return {"growth": t, "years": years, "vol": volatility(h)}


def price_growth(path: Path | None = None) -> float | None:
    """(Kept for callers) yearly same-cards price trend."""
    s = price_stats(price_index(path))
    return s["growth"] if s else None


def sync(conn, path: Path | None = None) -> dict:
    """Create/refresh the 'Pokemon collection' account from the dashboard. Safe to run any time."""
    from . import db
    hist = collection_history(path)
    if hist.empty:
        raise ValueError("No owned cards with prices found in the Pokemon dashboard")
    acct = db.get_or_create_account(conn, ACCOUNT_NAME, "Pokemon dashboard", "collectible")
    # replace earlier synced values (an older sync may have used partial snapshots)
    conn.execute("DELETE FROM balances WHERE account_id = ? AND source = 'pokemon dashboard'", (acct,))
    for r in hist.itertuples():
        db.upsert_balance(conn, acct, r.date.date(), float(r.value), "pokemon dashboard")
    stats = price_stats(price_index(path, list(hist["date"]))) or {}
    return {"account_id": acct, "value": float(hist["value"].iloc[-1]), "cards": int(hist["cards"].iloc[-1]),
            "as_of": hist["date"].iloc[-1].date(), "points": len(hist), "since": hist["date"].iloc[0].date(),
            "growth": stats.get("growth"), "years": stats.get("years"), "vol": stats.get("vol")}
