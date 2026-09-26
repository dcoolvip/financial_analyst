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

from . import paths

ROOT = Path(__file__).resolve().parent.parent

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
CREATE TABLE IF NOT EXISTS equity_grants (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    as_of       TEXT NOT NULL,
    grant_id    TEXT NOT NULL,
    grant_date  TEXT,
    type        TEXT,                   -- RSU / PSU
    symbol      TEXT NOT NULL,
    quantity    REAL,
    value       REAL NOT NULL,          -- estimated pre-tax value; NOT counted in net worth until it vests
    PRIMARY KEY (account_id, as_of, grant_id)
);
CREATE TABLE IF NOT EXISTS edit_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,          -- local time
    account     TEXT NOT NULL,
    what        TEXT NOT NULL,          -- balance / name / type / rate / payment / removed
    old         TEXT,
    new         TEXT,
    device      TEXT                    -- e.g. "iPhone · Safari"
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
    path = Path(path or os.environ.get("FINANCE_DB") or paths.data_dir() / "finance.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # Additive migrations for databases created by earlier versions
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "last4" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN last4 TEXT")   # from statements, to auto-match later
        conn.commit()
    if "cost_basis" not in {r[1] for r in conn.execute("PRAGMA table_info(holdings)")}:
        conn.execute("ALTER TABLE holdings ADD COLUMN cost_basis REAL")
        conn.commit()
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


def update_account(conn, account_id: int, name: str, type: str) -> None:
    if type not in ACCOUNT_TYPES:
        raise ValueError(f"Unknown account type {type!r}")
    conn.execute("UPDATE accounts SET name = ?, type = ? WHERE id = ?", (name.strip(), type, account_id))
    conn.commit()


def apply_account_edits(conn, edits: dict[int, dict]) -> None:
    """Edits from the Accounts table: {account_id: {name?, type?, rate?, payment?}}. A rate/payment of
    None clears it. All-or-nothing, so a duplicate name doesn't leave half the edits applied."""
    try:
        for account_id, f in edits.items():
            if "type" in f and f["type"] not in ACCOUNT_TYPES:
                raise ValueError(f"Unknown account type {f['type']!r}")
            if "name" in f and not str(f["name"]).strip():
                raise ValueError("Account names can't be empty")
            sets = {k: (str(v).strip() if k == "name" else v) for k, v in f.items()
                    if k in ("name", "type", "rate", "payment")}
            if sets:
                conn.execute(f"UPDATE accounts SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                             (*sets.values(), account_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def update_account_details(conn, account_id: int, **fields) -> None:
    """Set only the given fields (rate, payment, last4); None values are skipped, never cleared."""
    fields = {k: v for k, v in fields.items() if k in ("rate", "payment", "last4") and v is not None}
    if fields:
        conn.execute(f"UPDATE accounts SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), account_id))
        conn.commit()


def delete_account(conn, account_id: int) -> None:
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()


def accounts(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT a.id, a.name, a.institution, a.type, a.notes, a.rate, a.payment, a.last4, a.active,
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

def add_balance_if_missing(conn, account_id: int, on: date | str, balance: float, source: str) -> None:
    """For approximate history (e.g. read off a statement chart): never overwrites an existing value."""
    conn.execute("INSERT OR IGNORE INTO balances (account_id, date, balance, source) VALUES (?, ?, ?, ?)",
                 (account_id, _iso(on), float(balance), source))
    conn.commit()


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


def _net_worth_wide(conn, freq: str = "ME"):
    """Per-account month-end balances (dates x account ids). Each account carries its last balance forward.
    Before an ASSET account's first known balance it's unknown (NaN) - never a made-up $0 or a copy.
    Loans are extended backwards (reverse amortization with rate and payment, else carried back), since a
    loan existed before you first typed its balance and its balance moves slowly and predictably."""
    hist = balance_history(conn)
    if hist.empty:
        return None, [], [], {}
    wide = hist.pivot_table(index="date", columns="account_id", values="balance", aggfunc="last").sort_index()
    end = max(wide.index.max(), pd.Timestamp(date.today()))
    grid = pd.date_range(wide.index.min(), end, freq=freq)
    grid = grid.union([end])  # include "today" so the last point is current
    wide = wide.reindex(wide.index.union(grid)).ffill().reindex(grid)
    liab_ids = set(hist.loc[hist["is_liability"], "account_id"])
    liab_cols = [c for c in wide.columns if c in liab_ids]
    asset_cols = [c for c in wide.columns if c not in liab_ids]
    names = dict(zip(hist["account_id"], hist["name"]))

    terms = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, rate, payment FROM accounts")}
    for col in liab_cols:
        first = wide[col].first_valid_index()
        if first is None or first == wide.index[0]:
            continue
        rate, payment = terms.get(col, (None, None))
        bal, later = float(wide.at[first, col]), first
        for d in reversed(wide.index[wide.index < first]):
            if rate and payment:   # undo one month of amortization per month back
                months = max(1, round((later - d).days / 30.44))
                for _ in range(months):
                    bal = (bal + payment) / (1 + rate / 12)
            wide.at[d, col] = bal
            later = d
    return wide, asset_cols, liab_cols, names


def net_worth_series(conn, freq: str = "ME") -> pd.DataFrame:
    """Month-end assets / liabilities / net worth, over all the history there is.
    `added` names significant accounts whose history starts at that point, so the chart can explain the step
    instead of it looking like a gain."""
    wide, asset_cols, liab_cols, names = _net_worth_wide(conn, freq)
    if wide is None:
        return pd.DataFrame(columns=["date", "assets", "liabilities", "net_worth", "added"])
    filled = wide.fillna(0.0)
    out = pd.DataFrame({
        "date": wide.index,
        "assets": filled[asset_cols].sum(axis=1).values,
        "liabilities": filled[liab_cols].sum(axis=1).values,
    })
    out["net_worth"] = out["assets"] - out["liabilities"]
    added = {d: [] for d in wide.index}
    for c in asset_cols:
        first = wide[c].first_valid_index()
        if first is not None and first != wide.index[0]:
            if wide.at[first, c] > 0.05 * max(filled.loc[first, asset_cols].sum(), 1):
                added[first].append(names.get(c, str(c)))
    out["added"] = [", ".join(added[d]) for d in wide.index]
    return out


def net_worth_change(conn, months: int):
    """Like-for-like change in net worth over the last `months`: only accounts that had a value both then
    and now count, so adding an account never looks like a gain. Returns (change, base, left_out_names)
    or (None, None, []) if there's no history that far back."""
    wide, asset_cols, liab_cols, names = _net_worth_wide(conn)
    if wide is None or len(wide) < 2:
        return None, None, []
    now = wide.index[-1]
    earlier = wide.index[wide.index <= now - pd.DateOffset(months=months)]
    if not len(earlier):
        return None, None, []
    then = earlier[-1]
    both = [c for c in wide.columns if pd.notna(wide.at[then, c]) and pd.notna(wide.at[now, c])]
    sign = {c: (-1 if c in liab_cols else 1) for c in wide.columns}
    change = float(sum(sign[c] * (wide.at[now, c] - wide.at[then, c]) for c in both))
    base = float(sum(sign[c] * wide.at[then, c] for c in both))
    left_out = [names.get(c, str(c)) for c in wide.columns
                if c not in both and pd.notna(wide.at[now, c]) and wide.at[now, c] != 0]
    return change, base, left_out


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


def transaction_overlap(conn, account_ids: list[int], fingerprints: list[str]) -> dict[int, int]:
    """For each account, how many of these (file-level) fingerprints it already holds."""
    out = {}
    for aid in account_ids:
        keys = [f"{aid}|{fp}" for fp in fingerprints]
        n = 0
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            n += conn.execute(f"SELECT COUNT(*) FROM transactions WHERE fingerprint IN ({','.join('?' * len(chunk))})",
                              chunk).fetchone()[0]
        out[aid] = n
    return out


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
    has_cost = "cost_basis" in holdings.columns
    conn.executemany(
        "INSERT INTO holdings (account_id, as_of, symbol, description, quantity, price, value, cost_basis) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (account_id, as_of, r.symbol, r.description, r.quantity, r.price, float(r.value),
             (None if pd.isna(r.cost_basis) else float(r.cost_basis)) if has_cost else None)
            for r in holdings.itertuples()
        ],
    )
    conn.commit()


def replace_grants(conn, account_id: int, as_of: date | str, grants: list[dict]) -> None:
    as_of = _iso(as_of)
    conn.execute("DELETE FROM equity_grants WHERE account_id = ? AND as_of = ?", (account_id, as_of))
    conn.executemany("INSERT INTO equity_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     [(account_id, as_of, str(g["grant_id"]), _iso(g["grant_date"]) if g.get("grant_date") else None,
                       g.get("type"), g["symbol"], g.get("quantity"), float(g["value"])) for g in grants])
    conn.commit()


def latest_grants(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT a.name AS account, g.as_of, g.grant_id, g.grant_date, g.type, g.symbol, g.quantity, g.value
           FROM equity_grants g JOIN accounts a ON a.id = g.account_id
           WHERE g.as_of = (SELECT MAX(as_of) FROM equity_grants WHERE account_id = g.account_id)
           ORDER BY g.grant_date""", conn)


def latest_holdings(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT a.name AS account, h.as_of, h.symbol, h.description, h.quantity, h.price, h.value, h.cost_basis
           FROM holdings h JOIN accounts a ON a.id = h.account_id
           WHERE h.as_of = (SELECT MAX(as_of) FROM holdings WHERE account_id = h.account_id)
           ORDER BY h.value DESC""",
        conn,
    )


def log_edit(conn, account: str, what: str, old, new, device: str = "") -> None:
    fmt = lambda v: None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)   # noqa: E731
    conn.execute("INSERT INTO edit_log (at, account, what, old, new, device) VALUES (?, ?, ?, ?, ?, ?)",
                 (datetime.now().isoformat(timespec="seconds"), account, what, fmt(old), fmt(new), device[:60]))
    conn.commit()


def recent_edits(conn, limit: int = 20) -> pd.DataFrame:
    return pd.read_sql_query("SELECT at, account, what, old, new, device FROM edit_log ORDER BY id DESC LIMIT ?",
                             conn, params=(limit,))


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
