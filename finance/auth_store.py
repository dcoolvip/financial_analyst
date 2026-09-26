"""Sessions, passkeys and the password check for the HTTPS gate (see gate.py).

Kept in its own database (data/auth.db) so it never mixes with financial data.
Session tokens are stored only as SHA-256 hashes: a copy of auth.db can't be replayed as a cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMEMBER_S = 90 * 24 * 3600      # "keep me signed in on this device"
SHORT_S = 12 * 3600              # otherwise: this browser session, max 12 hours
TOUCH_EVERY_S = 300


def auth_dir() -> Path:
    return Path(os.environ.get("FINANCE_AUTH_DIR") or ROOT / "data")


def connect() -> sqlite3.Connection:
    d = auth_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "auth.db", check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            id         TEXT NOT NULL UNIQUE,      -- public id, safe to show/revoke by
            created    REAL NOT NULL,
            expires    REAL NOT NULL,
            last_seen  REAL NOT NULL,
            remember   INTEGER NOT NULL,
            device     TEXT NOT NULL,
            method     TEXT NOT NULL              -- 'password' or 'passkey'
        );
        CREATE TABLE IF NOT EXISTS passkeys (
            id          TEXT PRIMARY KEY,         -- base64url credential id
            public_key  BLOB NOT NULL,
            sign_count  INTEGER NOT NULL,
            name        TEXT NOT NULL,
            created     REAL NOT NULL,
            last_used   REAL
        );
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v BLOB NOT NULL);
    """)
    os.chmod(d / "auth.db", 0o600)
    return conn


# --- password -------------------------------------------------------------------

def password_is_set() -> bool:
    return (auth_dir() / "app_password").exists()


def check_password(pw: str) -> bool:
    try:
        salt_hex, digest_hex = (auth_dir() / "app_password").read_text().strip().split(":")
    except (FileNotFoundError, ValueError):
        return False
    attempt = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(attempt.hex(), digest_hex)


# --- sessions -------------------------------------------------------------------

def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, remember: bool, device: str, method: str) -> tuple[str, int | None]:
    """Returns (cookie token, cookie max-age or None for a browser-session cookie)."""
    token, now = secrets.token_urlsafe(32), time.time()
    ttl = REMEMBER_S if remember else SHORT_S
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (_h(token), secrets.token_hex(6), now, now + ttl, now, int(remember), device[:200], method))
    conn.commit()
    return token, (REMEMBER_S if remember else None)


def valid_session(conn, token: str | None) -> dict | None:
    if not token:
        return None
    row = conn.execute("SELECT id, expires, last_seen FROM sessions WHERE token_hash = ?", (_h(token),)).fetchone()
    if not row:
        return None
    sid, expires, last_seen = row
    now = time.time()
    if expires < now:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_h(token),))
        conn.commit()
        return None
    if now - last_seen > TOUCH_EVERY_S:
        conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, _h(token)))
        conn.commit()
    return {"id": sid}


def end_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_h(token),))
        conn.commit()


def list_sessions(conn) -> list[dict]:
    conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    rows = conn.execute("SELECT id, created, expires, last_seen, remember, device, method FROM sessions "
                        "ORDER BY last_seen DESC").fetchall()
    keys = ["id", "created", "expires", "last_seen", "remember", "device", "method"]
    return [dict(zip(keys, r)) for r in rows]


def revoke_session(conn, sid: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    conn.commit()


def revoke_others(conn, keep_token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash != ?", (_h(keep_token),))
    conn.commit()


# --- passkeys -------------------------------------------------------------------

def user_handle(conn) -> bytes:
    """Stable random WebAuthn user id for the single user of this dashboard."""
    row = conn.execute("SELECT v FROM meta WHERE k = 'user_handle'").fetchone()
    if row:
        return row[0]
    uh = secrets.token_bytes(16)
    conn.execute("INSERT INTO meta VALUES ('user_handle', ?)", (uh,))
    conn.commit()
    return uh


def passkeys(conn) -> list[dict]:
    rows = conn.execute("SELECT id, public_key, sign_count, name, created, last_used FROM passkeys "
                        "ORDER BY created").fetchall()
    return [dict(zip(["id", "public_key", "sign_count", "name", "created", "last_used"], r)) for r in rows]


def get_passkey(conn, cred_id: str) -> dict | None:
    return next((p for p in passkeys(conn) if p["id"] == cred_id), None)


def add_passkey(conn, cred_id: str, public_key: bytes, sign_count: int, name: str) -> None:
    conn.execute("INSERT OR REPLACE INTO passkeys VALUES (?, ?, ?, ?, ?, NULL)",
                 (cred_id, public_key, sign_count, name[:60] or "Passkey", time.time()))
    conn.commit()


def touch_passkey(conn, cred_id: str, sign_count: int) -> None:
    conn.execute("UPDATE passkeys SET sign_count = ?, last_used = ? WHERE id = ?", (sign_count, time.time(), cred_id))
    conn.commit()


def delete_passkey(conn, cred_id: str) -> None:
    conn.execute("DELETE FROM passkeys WHERE id = ?", (cred_id,))
    conn.commit()
