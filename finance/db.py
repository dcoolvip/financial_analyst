"""SQLite storage. Everything the dashboard shows is read from here.

Sign convention: balances are stored as positive magnitudes. Liability accounts
(loans, credit cards) store the amount owed; net worth = assets - liabilities.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "finance.db"

ASSET_TYPES = ["checking", "savings", "brokerage", "retirement", "property", "vehicle", "other_asset"]
LIABILITY_TYPES = ["credit_card", "mortgage", "auto_loan", "heloc", "personal_loan", "student_loan", "other_liability"]
ACCOUNT_TYPES = ASSET_TYPES + LIABILITY_TYPES

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    institution TEXT NOT NULL,
    type        TEXT NOT NULL,
    notes       TEXT,
    rate        REAL,                   -- APR for loans / expected growth for assets (optional)
    payment     REAL,                   -- monthly payment for loans (optional)
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL           -- JSON
);
CREATE TABLE IF NOT EXISTS balances (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date        TEXT NOT NULL,
    balance     REAL NOT NULL,
    source      TEXT NOT NULL,          -- 'manual' or import filename
    PRIMARY KEY (account_id, date)
);
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date        TEXT NOT NULL,
    description TEXT NOT NULL,
    amount      REAL NOT NULL,          -- + money in, - money out
    category    TEXT,
    fingerprint TEXT NOT NULL UNIQUE,   -- dedupe key so re-imports are safe
    source      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    as_of       TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    description TEXT,
    quantity    REAL,
    price       REAL,
    value       REAL NOT NULL,
    PRIMARY KEY (account_id, as_of, symbol)
);
CREATE TABLE IF NOT EXISTS imports (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    imported_at  TEXT NOT NULL,
    rows_added   INTEGER NOT NULL
);
"""


def is_liability(account_type: str) -> bool:
    return account_type in LIABILITY_TYPES


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or os.environ.get("FINANCE_DB") or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _iso(d: date | datetime | str) -> str:
    return d if isinstance(d, str) else d.strftime("%Y-%m-%d")


# --- accounts -----------------------------------------------------------------

def add_account(conn, name: str, institution: str, type: str, notes: str = "") -> int:
    if type not in ACCOUNT_TYPES:
        raise ValueError(f"Unknown account type {type!r}")
    cur = conn.execute(
        "INSERT INTO accounts (name, institution, type, notes) VALUES (?, ?, ?, ?)",
        (name.strip(), institution.strip(), type, notes),
    )
    conn.commit()
    return cur.lastrowid


def get_or_create_account(conn, name: str, institution: str, type: str) -> int:
    row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    return row[0] if row else add_account(conn, name, institution, type)


def update_account_terms(conn, account_id: int, rate: float | None, payment: float | None) -> None:
    conn.execute("UPDATE accounts SET rate = ?, payment = ? WHERE id = ?", (rate, payment, account_id))
    conn.commit()


def delete_account(conn, account_id: int) -> None:
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()


def accounts(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT a.id, a.name, a.institution, a.type, a.notes, a.rate, a.payment, a.active,
               b.balance, b.date AS as_of
        FROM accounts a
        LEFT JOIN balances b ON b.account_id = a.id
             AND b.date = (SELECT MAX(date) FROM balances WHERE account_id = a.id)
        ORDER BY a.institution, a.name
        """,
        conn,
    )
    df["is_liability"] = df["type"].map(is_liability)
    return df


# --- balances -----------------------------------------------------------------

def upsert_balance(conn, account_id: int, on: date | str, balance: float, source: str = "manual") -> None:
    conn.execute(
        """INSERT INTO balances (account_id, date, balance, source) VALUES (?, ?, ?, ?)
           ON CONFLICT(account_id, date) DO UPDATE SET balance = excluded.balance, source = excluded.source""",
        (account_id, _iso(on), float(balance), source),
    )
    conn.commit()


def balance_history(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        """SELECT b.account_id, a.name, a.type, b.date, b.balance
           FROM balances b JOIN accounts a ON a.id = b.account_id
           WHERE a.active = 1""",
        conn,
        parse_dates=["date"],
    )
    df["is_liability"] = df["type"].map(is_liability)
    return df


def net_worth_series(conn, freq: str = "ME") -> pd.DataFrame:
    """Month-end assets / liabilities / net worth.

    Each account carries its last known balance forward until a newer one
    arrives, so accounts updated on different days still line up.
    """
    hist = balance_history(conn)
    if hist.empty:
        return pd.DataFrame(columns=["date", "assets", "liabilities", "net_worth"])
    wide = hist.pivot_table(index="date", columns="account_id", values="balance", aggfunc="last").sort_index()
    end = max(wide.index.max(), pd.Timestamp(date.today()))
    grid = pd.date_range(wide.index.min(), end, freq=freq)
    grid = grid.union([end])  # include "today" so the last point is current
    wide = wide.reindex(wide.index.union(grid)).ffill().reindex(grid).fillna(0.0)
    liab_ids = set(hist.loc[hist["is_liability"], "account_id"])
    liab_cols = [c for c in wide.columns if c in liab_ids]
    asset_cols = [c for c in wide.columns if c not in liab_ids]
    out = pd.DataFrame({
        "date": grid,
        "assets": wide[asset_cols].sum(axis=1).values,
        "liabilities": wide[liab_cols].sum(axis=1).values,
    })
    out["net_worth"] = out["assets"] - out["liabilities"]
    return out


# --- transactions & holdings --------------------------------------------------

def insert_transactions(conn, account_id: int, txns: pd.DataFrame, source: str) -> int:
    """Insert rows with columns date, description, amount, fingerprint. Returns count added."""
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO transactions (account_id, date, description, amount, fingerprint, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            (account_id, _iso(r.date), r.description, float(r.amount), f"{account_id}|{r.fingerprint}", source)
            for r in txns.itertuples()
        ],
    )
    conn.commit()
    return conn.total_changes - before


def transactions(conn, account_ids: list[int] | None = None) -> pd.DataFrame:
    q = """SELECT t.id, t.date, a.name AS account, t.description, t.amount, t.category
           FROM transactions t JOIN accounts a ON a.id = t.account_id"""
    params: list = []
    if account_ids:
        q += f" WHERE t.account_id IN ({','.join('?' * len(account_ids))})"
        params = list(account_ids)
    return pd.read_sql_query(q + " ORDER BY t.date DESC, t.id DESC", conn, params=params, parse_dates=["date"])


def replace_holdings(conn, account_id: int, as_of: date | str, holdings: pd.DataFrame) -> None:
    as_of = _iso(as_of)
    conn.execute("DELETE FROM holdings WHERE account_id = ? AND as_of = ?", (account_id, as_of))
    conn.executemany(
        "INSERT INTO holdings VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (account_id, as_of, r.symbol, r.description, r.quantity, r.price, float(r.value))
            for r in holdings.itertuples()
        ],
    )
    conn.commit()


def latest_holdings(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT a.name AS account, h.as_of, h.symbol, h.description, h.quantity, h.price, h.value
           FROM holdings h JOIN accounts a ON a.id = h.account_id
           WHERE h.as_of = (SELECT MAX(as_of) FROM holdings WHERE account_id = h.account_id)
           ORDER BY h.value DESC""",
        conn,
    )


def log_import(conn, account_id: int, filename: str, kind: str, rows_added: int) -> None:
    conn.execute(
        "INSERT INTO imports (account_id, filename, kind, imported_at, rows_added) VALUES (?, ?, ?, ?, ?)",
        (account_id, filename, kind, datetime.now().isoformat(timespec="seconds"), rows_added),
    )
    conn.commit()


# --- settings -----------------------------------------------------------------

def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()
