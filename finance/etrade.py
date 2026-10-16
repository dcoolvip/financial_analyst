"""E*TRADE's own API: balances and holdings for the brokerage accounts, without Plaid.

You approve access on etrade.com itself (text code and all); the dashboard never sees the password. E*TRADE
uses OAuth 1.0a: a request token, your approval on their site (which shows a short code to paste back), then an
access token. Access tokens end at midnight US Eastern time and go idle after 2 hours, so a sync renews the
token and, after midnight, asks you to approve again.

Keys and tokens live in etrade.json in the data folder (readable only by this Mac user). A sandbox key returns
E*TRADE's made-up test accounts, so in sandbox mode nothing is saved - the dashboard only shows what came back.
Only reads are ever made: accounts, balances, portfolio.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import db, paths

HOSTS = {"live": "https://api.etrade.com", "sandbox": "https://apisb.etrade.com"}
AUTHORIZE = "https://us.etrade.com/e/t/etws/authorize?key={key}&token={token}"
EASTERN = ZoneInfo("America/New_York")
SOURCE = "E*TRADE API"


class ETradeError(Exception):
    pass


class NeedsApproval(ETradeError):
    """The access token ended (midnight Eastern) or was revoked: approve again on etrade.com."""


def keys_path() -> Path:
    return paths.data_dir() / "etrade.json"


def test_path() -> Path:
    return paths.data_dir() / "etrade_test.json"


def load() -> dict | None:
    try:
        k = json.loads(keys_path().read_text())
    except (FileNotFoundError, ValueError):
        return None
    return k if k.get("consumer_key") and k.get("consumer_secret") else None


def save_keys(consumer_key: str, consumer_secret: str, env: str) -> None:
    old = load() or {}
    if old.get("env") != env:
        old.pop("connections", None)          # tokens belong to one environment
    paths.write_private(keys_path(), {**old, "consumer_key": consumer_key.strip(),
                                      "consumer_secret": consumer_secret.strip(), "env": env})


def _save(keys: dict) -> None:
    paths.write_private(keys_path(), keys)


def forget() -> None:
    keys_path().unlink(missing_ok=True)


# --- OAuth 1.0a (HMAC-SHA1), standard library only ------------------------------------------------------------

def _q(s) -> str:
    return urllib.parse.quote(str(s), safe="~")


def _auth_header(method: str, url: str, query: dict, keys: dict, token: str = "", token_secret: str = "",
                 extra: dict | None = None) -> str:
    oauth = {"oauth_consumer_key": keys["consumer_key"], "oauth_nonce": secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(int(time.time())),
             "oauth_version": "1.0", **({"oauth_token": token} if token else {}), **(extra or {})}
    params = sorted((_q(k), _q(v)) for k, v in {**query, **oauth}.items())
    base = "&".join([method.upper(), _q(url), _q("&".join(f"{k}={v}" for k, v in params))])
    key = f"{_q(keys['consumer_secret'])}&{_q(token_secret)}".encode()
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth realm=\"\"," + ",".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(oauth.items()))


def _get(path: str, keys: dict, token: str = "", token_secret: str = "", query: dict | None = None,
         extra: dict | None = None) -> bytes:
    url, query = HOSTS[keys.get("env", "live")] + path, query or {}
    req = urllib.request.Request(url + ("?" + urllib.parse.urlencode(query) if query else ""), headers={
        "Authorization": _auth_header("GET", url, query, keys, token, token_secret, extra),
        "Accept": "application/json"})
    try:
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except urllib.error.URLError as e:
            if isinstance(e, urllib.error.HTTPError) or "timed out" not in str(e.reason):
                raise
            with urllib.request.urlopen(req, timeout=30) as r:            # E*TRADE is sometimes slow: once more
                return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 401:
            raise NeedsApproval(f"E*TRADE needs you to approve access again ({body or 'unauthorized'})") from e
        raise ETradeError(f"E*TRADE answered {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise ETradeError(f"Couldn't reach E*TRADE: {e.reason}") from e


def start_approval(keys: dict) -> tuple[str, str, str]:
    """Step 1: a request token, and the etrade.com page where you approve it. Returns (url, token, secret)."""
    r = urllib.parse.parse_qs(_get("/oauth/request_token", keys, extra={"oauth_callback": "oob"}).decode())
    token, secret = r["oauth_token"][0], r["oauth_token_secret"][0]
    return AUTHORIZE.format(key=_q(keys["consumer_key"]), token=_q(token)), token, secret


def finish_approval(code: str, token: str, secret: str, keys: dict) -> dict:
    """Step 2: swap the code etrade.com showed for an access token, and remember which accounts it opens.
    Approving again for the same login replaces its old connection."""
    r = urllib.parse.parse_qs(_get("/oauth/access_token", keys, token, secret,
                                   extra={"oauth_verifier": code.strip()}).decode())
    conn_ = {"token": r["oauth_token"][0], "secret": r["oauth_token_secret"][0],
             "approved": datetime.now(EASTERN).date().isoformat()}
    accts = accounts(keys, conn_)
    conn_["accounts"] = sorted(a["accountIdKey"] for a in accts)
    others = [c for c in keys.get("connections", []) if c.get("accounts") != conn_["accounts"]]
    _save({**keys, "connections": others + [conn_]})
    return conn_


def is_current(c: dict) -> bool:
    return c.get("approved") == datetime.now(EASTERN).date().isoformat()


def _json(path: str, keys: dict, c: dict, query: dict | None = None) -> dict:
    if not is_current(c):
        raise NeedsApproval("E*TRADE access ends at midnight Eastern - approve again on etrade.com")
    return json.loads(_get(path, keys, c["token"], c["secret"], query))


def renew(keys: dict, c: dict) -> None:
    """Wake a token that went idle (2 hours without calls)."""
    _get("/oauth/renew_access_token", keys, c["token"], c["secret"])


def revoke(keys: dict, c: dict) -> None:
    try:
        _get("/oauth/revoke_access_token", keys, c["token"], c["secret"])
    finally:
        _save({**keys, "connections": [x for x in keys.get("connections", []) if x["token"] != c["token"]]})


# --- reading ---------------------------------------------------------------------------------------------------

def _as_list(x) -> list:
    return x if isinstance(x, list) else [x] if x else []


def accounts(keys: dict, c: dict) -> list[dict]:
    r = _json("/v1/accounts/list.json", keys, c)
    return _as_list(((r.get("AccountListResponse") or {}).get("Accounts") or {}).get("Account"))


def balance(keys: dict, c: dict, account_key: str, inst_type: str = "BROKERAGE") -> float | None:
    r = _json(f"/v1/accounts/{account_key}/balance.json", keys, c,
              {"instType": inst_type or "BROKERAGE", "realTimeNAV": "true"})
    computed = (r.get("BalanceResponse") or {}).get("Computed") or {}
    v = (computed.get("RealTimeValues") or {}).get("totalAccountValue")
    return float(v) if v is not None else None


def positions(keys: dict, c: dict, account_key: str) -> pd.DataFrame:
    rows, page = [], 1
    while True:
        r = _json(f"/v1/accounts/{account_key}/portfolio.json", keys, c, {"count": "100", "pageNumber": str(page)})
        ports = _as_list((r.get("PortfolioResponse") or {}).get("AccountPortfolio"))
        for p in ports:
            for x in _as_list(p.get("Position")):
                sym = (x.get("Product") or {}).get("symbol") or x.get("symbolDescription") or "Unnamed position"
                rows.append({"symbol": sym, "description": x.get("symbolDescription") or sym,
                             "quantity": x.get("quantity"), "price": (x.get("Quick") or {}).get("lastTrade"),
                             "value": float(x.get("marketValue") or 0), "cost_basis": x.get("totalCost")})
        pages = max([int(p.get("totalPages") or 1) for p in ports] or [1])
        if page >= pages:
            break
        page += 1
    df = pd.DataFrame(rows, columns=["symbol", "description", "quantity", "price", "value", "cost_basis"])
    # one row per stock: E*TRADE lists each lot (bought at different times) separately
    return df.groupby("symbol", as_index=False, sort=False).agg(
        description=("description", "first"), quantity=("quantity", "sum"), price=("price", "first"),
        value=("value", "sum"), cost_basis=("cost_basis", lambda c: c.sum(min_count=1)))


def match_account(acct: dict, ours: pd.DataFrame) -> int | None:
    """Same last 4 digits among the E*TRADE brokerage/retirement accounts."""
    c = ours[ours["institution"].str.contains("E*TRADE", case=False, regex=False)
             & ours["type"].isin(["brokerage", "retirement"])]
    hit = c[c["last4"] == str(acct.get("accountId", ""))[-4:]]
    return int(hit["id"].iloc[0]) if len(hit) == 1 else None


def mapping(conn) -> dict:
    return db.get_setting(conn, "etrade_accounts", {}) or {}


def link_account(conn, account_key: str, account_id: int | None) -> None:
    m = mapping(conn)
    m[account_key] = {"account_id": None if account_id is None else int(account_id)}
    db.set_setting(conn, "etrade_accounts", m)


def load_test() -> dict | None:
    try:
        return json.loads(test_path().read_text())
    except (FileNotFoundError, ValueError):
        return None


def sandbox_test(keys: dict, c: dict) -> list[dict]:
    """Sandbox: read E*TRADE's test accounts and keep a summary to look at - nothing goes into the database."""
    out = []
    for a in accounts(keys, c):
        row = {"account": a.get("accountName") or a.get("accountDesc"), "type": a.get("accountType"),
               "ends": str(a.get("accountId", ""))[-4:], "balance": None, "positions": None, "error": None}
        try:
            row["balance"] = balance(keys, c, a["accountIdKey"], a.get("institutionType"))
            row["positions"] = len(positions(keys, c, a["accountIdKey"]))
        except ETradeError as e:
            row["error"] = str(e)
        out.append(row)
    test_path().write_text(json.dumps({"checked": time.time(), "accounts": out}, indent=1))
    return out


def error_path() -> Path:
    return paths.data_dir() / "etrade_errors.log"


def _log_error(acct: dict, e: Exception) -> None:
    """Details for fixing the reader later - the traceback only, never keys or tokens."""
    import traceback
    with error_path().open("a") as f:
        f.write(f"--- {datetime.now():%Y-%m-%d %H:%M} account ••{str(acct.get('accountId', ''))[-4:]}\n"
                + "".join(traceback.format_exception(e)))


def sync(conn, keys: dict) -> dict:
    """Live: balance and holdings for every approved E*TRADE account, into the matching dashboard account."""
    if keys.get("env") != "live":
        raise ETradeError("This is a sandbox key - it returns test accounts, so nothing is saved")
    ours, m, today = db.accounts(conn), mapping(conn), date.today()
    report = {"accounts": [], "unmatched": [], "needs_approval": 0, "errors": []}
    for c in keys.get("connections", []):
        try:
            if is_current(c):
                renew(keys, c)
            accts = accounts(keys, c)
        except NeedsApproval:
            report["needs_approval"] += 1
            continue
        except ETradeError as e:
            report["errors"].append(str(e))
            continue
        for a in accts:
            k = a["accountIdKey"]
            if k not in m and (aid := match_account(a, ours)) is not None:
                link_account(conn, k, aid)
                m = mapping(conn)
            aid = (m.get(k) or {}).get("account_id")
            if aid is None:
                if k not in m:
                    report["unmatched"].append({"key": k, "name": a.get("accountName") or a.get("accountDesc"),
                                                "ends": str(a.get("accountId", ""))[-4:], "type": a.get("accountType")})
                continue
            try:
                held = positions(keys, c, k)
                total = balance(keys, c, k, a.get("institutionType"))
            except ETradeError as e:
                report["errors"].append(f"{a.get('accountDesc') or k}: {e}")
                continue
            except Exception as e:                      # an answer we didn't expect: keep going, keep the details
                _log_error(a, e)
                report["errors"].append(f"{a.get('accountDesc') or k}: couldn't read E*TRADE's answer ({e})")
                continue
            if len(held):
                db.replace_holdings(conn, aid, today, held)
            if not total and len(held) and held["value"].sum() > 0:     # $0 next to real positions isn't the value
                total = float(held["value"].sum())
            if total is not None:
                db.upsert_balance(conn, aid, today, total, SOURCE)
            report["accounts"].append({"account_id": aid, "balance": total, "positions": len(held)})
    db.set_setting(conn, "etrade_last_sync", time.time())
    return report
