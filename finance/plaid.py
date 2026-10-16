"""Plaid: the API keys, and which of the household's institutions Plaid can connect with the current access level.

The keys live in their own file in the data folder (plaid.json, readable only by this Mac user), never in the
code, the database or the import snapshots. Calls go straight to Plaid's REST API with the standard library.

Plaid access levels: Limited Production connects institutions that sign in through Plaid directly; institutions
that send you to their own sign-in page ("OAuth") need full production access.

Linking: the dashboard asks Plaid for a one-time link token, Plaid's own pop-up (plaid_link/index.html) signs in
to the bank, and the token it hands back is swapped for a long-lived access token, kept in plaid.json with the
keys. Syncing reads balances, new transactions and holdings into the matching dashboard accounts. Only read
products are ever requested.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

from . import db, paths

HOSTS = {"production": "https://production.plaid.com", "sandbox": "https://sandbox.plaid.com"}
NOT_ON_PLAID = {"Physical", "Pokemon dashboard", "Redfin estimate", "Hyundai"}   # values we track by hand
SHOWN_PER_SEARCH = 3


class PlaidError(Exception):
    pass


def keys_path() -> Path:
    return paths.data_dir() / "plaid.json"


def check_path() -> Path:
    return paths.data_dir() / "plaid_check.json"


def load_keys() -> dict | None:
    try:
        k = json.loads(keys_path().read_text())
    except (FileNotFoundError, ValueError):
        return None
    return k if k.get("client_id") and k.get("secret") else None


def _write(data: dict) -> None:
    p = keys_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # private from the first byte
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(p, 0o600)


def save_keys(client_id: str, secret: str, env: str = "production") -> None:
    old = load_keys() or {}
    _write({**old, "client_id": client_id.strip(), "secret": secret.strip(), "env": env})


def forget_keys() -> None:
    keys_path().unlink(missing_ok=True)


def _post(endpoint: str, body: dict, keys: dict) -> dict:
    req = urllib.request.Request(
        HOSTS[keys.get("env", "production")] + endpoint,
        data=json.dumps({"client_id": keys["client_id"], "secret": keys["secret"], **body}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except ValueError:
            raise PlaidError(f"Plaid answered {e.code}") from e
        raise PlaidError(f"{err.get('error_message') or err.get('error_code')} ({err.get('error_code')})") from e
    except urllib.error.URLError as e:
        raise PlaidError(f"Couldn't reach Plaid: {e.reason}") from e


def search(query: str, keys: dict) -> list[dict]:
    r = _post("/institutions/search", {"query": query, "country_codes": ["US"], "products": None}, keys)
    return r.get("institutions", [])


def _same_name(ours: str, theirs: str) -> bool:
    """Plaid's search is fuzzy ("Chase" also finds "Austin Bank"): keep matches that contain our name's words."""
    words = lambda t: set(re.findall(r"[a-z0-9*]+", t.lower()))
    return words(ours) <= words(theirs)


def institutions_to_check(names) -> list[str]:
    return sorted({n for n in names if n and n not in NOT_ON_PLAID})


def check(institutions: list[str], keys: dict) -> list[dict]:
    """One row per Plaid match for each institution: does it need OAuth (full production), and which
    of the products the dashboard would use does Plaid offer there."""
    rows = []
    for name in institutions:
        found = [i for i in search(name, keys) if _same_name(name, i.get("name", ""))][:SHOWN_PER_SEARCH]
        if not found:
            rows.append({"yours": name, "plaid_name": None, "oauth": None, "products": []})
        for inst in found:
            rows.append({"yours": name, "plaid_name": inst.get("name"), "oauth": bool(inst.get("oauth")),
                         "products": [p for p in ("transactions", "investments", "liabilities")
                                      if p in inst.get("products", [])]})
    check_path().write_text(json.dumps({"checked": time.time(), "rows": rows}, indent=1))
    return rows


def last_check() -> dict | None:
    try:
        return json.loads(check_path().read_text())
    except (FileNotFoundError, ValueError):
        return None


# --- linking and syncing -----------------------------------------------------------------------------------------

PLAID_KINDS = {"depository": ["checking", "savings"], "credit": ["credit_card"],
               "investment": ["brokerage", "retirement"], "loan": [t for t in db.LIABILITY_TYPES if t != "credit_card"]}
SOURCE = "Plaid"


def link_token(keys: dict) -> str:
    r = _post("/link/token/create", {
        "client_name": "Financial Analyst", "language": "en", "country_codes": ["US"],
        "user": {"client_user_id": "household"},
        "products": ["transactions"], "optional_products": ["investments"],      # read-only products only
        "transactions": {"days_requested": 730}}, keys)
    return r["link_token"]


def add_item(public_token: str, institution: str, keys: dict) -> dict:
    r = _post("/item/public_token/exchange", {"public_token": public_token}, keys)
    item = {"item_id": r["item_id"], "access_token": r["access_token"], "institution": institution,
            "cursor": "", "linked": time.time()}
    keys = {**keys, "items": [i for i in keys.get("items", []) if i["item_id"] != item["item_id"]] + [item]}
    _write(keys)
    return item


def remove_item(item_id: str, keys: dict) -> None:
    """Unlink at Plaid too, so the bank connection (and any billing for it) ends."""
    item = next(i for i in keys.get("items", []) if i["item_id"] == item_id)
    try:
        _post("/item/remove", {"access_token": item["access_token"]}, keys)
    finally:
        _write({**keys, "items": [i for i in keys["items"] if i["item_id"] != item_id]})


def _save_item(keys: dict, item: dict) -> None:
    _write({**keys, "items": [item if i["item_id"] == item["item_id"] else i for i in keys.get("items", [])]})


def match_account(plaid_acct: dict, institution: str, ours: pd.DataFrame) -> int | None:
    """Which dashboard account a Plaid account is: same institution and kind, then the same last 4 digits,
    else the only account of that kind there that has no digits on record."""
    kinds = PLAID_KINDS.get(plaid_acct.get("type"), [])
    if plaid_acct.get("subtype") in kinds:          # checking stays checking, savings stays savings
        kinds = [plaid_acct["subtype"]]
    c = ours[ours["type"].isin(kinds) & ours["institution"].map(lambda n: _same_name(n, institution))]
    mask = plaid_acct.get("mask")
    if mask and (hit := c[c["last4"] == mask]).shape[0] == 1:
        return int(hit["id"].iloc[0])
    blank = c[c["last4"].isna() | (c["last4"] == "")]
    return int(blank["id"].iloc[0]) if len(blank) == 1 and len(c) == 1 else None


def mapping(conn) -> dict:
    return db.get_setting(conn, "plaid_accounts", {}) or {}


def link_account(conn, plaid_account_id: str, account_id: int | None, keys: dict | None = None) -> None:
    """Point a Plaid account at a dashboard account (None = don't import it). Only transactions after the newest
    one already there are taken, so earlier CSV/statement imports aren't doubled."""
    m = mapping(conn)
    if account_id is None:
        m[plaid_account_id] = {"account_id": None}
    else:
        last = conn.execute("SELECT MAX(date) FROM transactions WHERE account_id = ? AND source != ?",
                            (int(account_id), SOURCE)).fetchone()[0]
        m[plaid_account_id] = {"account_id": int(account_id), "after": last}
        if keys:                     # its transactions may have gone by unmapped: read the item from the start again
            for item in keys.get("items", []):
                if plaid_account_id in item.get("accounts", []):
                    _save_item(keys, {**item, "cursor": ""})
                    keys = load_keys()
    db.set_setting(conn, "plaid_accounts", m)


def _transactions(item: dict, keys: dict) -> tuple[list, list, list, str]:
    added, modified, removed, cursor = [], [], [], item.get("cursor", "")
    while True:
        r = _post("/transactions/sync", {"access_token": item["access_token"], "cursor": cursor, "count": 500}, keys)
        added += r["added"]; modified += r["modified"]; removed += r["removed"]
        cursor = r["next_cursor"]
        if not r["has_more"]:
            return added, modified, removed, cursor


def _row(t: dict) -> dict:
    cat = (t.get("personal_finance_category") or {}).get("primary") or ""
    return {"date": date.fromisoformat(t["date"]), "description": t.get("name") or t.get("merchant_name") or "",
            "amount": -float(t["amount"]),                  # Plaid: money out is positive; here it's negative
            "fingerprint": f"plaid|{t['transaction_id']}", "bank_category": cat.replace("_", " ").title() or None}


def sync(conn, keys: dict) -> dict:
    """Balances, new transactions and holdings for every linked item. Returns what changed, plus the Plaid
    accounts that still need to be matched to a dashboard account."""
    ours, m, today = db.accounts(conn), mapping(conn), date.today()
    report = {"accounts": [], "unmatched": [], "errors": []}
    for item in keys.get("items", []):
        try:
            accts = _post("/accounts/get", {"access_token": item["access_token"]}, keys)["accounts"]
        except PlaidError as e:
            report["errors"].append(f"{item['institution']}: {e}")
            continue
        item = {**item, "accounts": [a["account_id"] for a in accts]}
        target = {}
        for a in accts:
            if a["account_id"] not in m and (aid := match_account(a, item["institution"], ours)) is not None:
                link_account(conn, a["account_id"], aid)
                m = mapping(conn)
            t = (m.get(a["account_id"]) or {}).get("account_id")
            if t is None:
                if a["account_id"] not in m:
                    report["unmatched"].append({"plaid_account_id": a["account_id"], "institution": item["institution"],
                                                "name": a.get("official_name") or a.get("name"), "mask": a.get("mask"),
                                                "type": a.get("type"), "subtype": a.get("subtype")})
                continue
            target[a["account_id"]] = (t, a)
            bal = a["balances"].get("current")
            if bal is not None:
                db.upsert_balance(conn, t, today, abs(float(bal)) if a["type"] in ("credit", "loan") else float(bal),
                                  SOURCE)
        counts = dict.fromkeys(target, 0)
        try:
            added, modified, removed, cursor = _transactions(item, keys)
        except PlaidError as e:
            report["errors"].append(f"{item['institution']} transactions: {e}")
        else:
            for t in added + modified:
                if t.get("pending") or t["account_id"] not in target:
                    continue
                aid, after = target[t["account_id"]][0], m[t["account_id"]].get("after")
                if after and t["date"] <= after:
                    continue
                row = _row(t)
                fp = f"{aid}|{row['fingerprint']}"
                conn.execute("UPDATE transactions SET date = ?, description = ?, amount = ? WHERE fingerprint = ?",
                             (row["date"].isoformat(), row["description"], row["amount"], fp))
                counts[t["account_id"]] += db.insert_transactions(conn, aid, pd.DataFrame([row]), SOURCE)
            for t in removed:
                conn.execute("DELETE FROM transactions WHERE fingerprint LIKE ?", (f"%|plaid|{t['transaction_id']}",))
            conn.commit()
            item = {**item, "cursor": cursor}
        if any(a["type"] == "investment" for _, a in target.values()):
            try:
                h = _post("/investments/holdings/get", {"access_token": item["access_token"]}, keys)
            except PlaidError as e:
                report["errors"].append(f"{item['institution']} holdings: {e}")
            else:
                sec = {s["security_id"]: s for s in h["securities"]}
                for pid, (aid, a) in target.items():
                    rows = [{"symbol": sec[x["security_id"]].get("ticker_symbol") or sec[x["security_id"]].get("name"),
                             "description": sec[x["security_id"]].get("name") or "",
                             "quantity": x.get("quantity"), "price": x.get("institution_price"),
                             "value": float(x.get("institution_value") or 0)}
                            for x in h["holdings"] if x["account_id"] == pid]
                    if rows:
                        db.replace_holdings(conn, aid, today, pd.DataFrame(rows))
        _save_item(keys, item)
        keys = load_keys()
        for pid, (aid, a) in target.items():
            report["accounts"].append({"account_id": aid, "institution": item["institution"],
                                       "added": counts[pid], "balance": a["balances"].get("current")})
    db.set_setting(conn, "plaid_last_sync", time.time())
    return report
