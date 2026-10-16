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
    paths.write_private(keys_path(), data)


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


ALSO_CHECK = ["Morgan Stanley"]      # where the stock plans actually sit (statements say "Morgan Stanley at Work")


def institutions_to_check(names) -> list[str]:
    return sorted({n for n in names if n and n not in NOT_ON_PLAID} | set(ALSO_CHECK))


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
SYNC_EVERY_S = 30 * 24 * 3600        # the dashboard syncs on its own once a month (when opened after that)


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


def _symbol(security: dict) -> str:
    """Plaid names cash like 'CUR:USD'; the portfolio knows it as CASH."""
    t = security.get("ticker_symbol") or ""
    if security.get("type") == "cash" or t.startswith("CUR:"):
        return "CASH"
    return t or security.get("name") or "Unnamed position"


def owned_part(holdings: pd.DataFrame, current) -> pd.DataFrame:
    """Stock plans (Morgan Stanley at Work) list vested + unvested shares in holdings, but the account's current
    value is only what's vested and yours. Scale the holdings down to that, so the portfolio matches net worth and
    unvested RSUs aren't counted twice (they're tracked as grants from the statements)."""
    if "vested_value" in holdings and holdings["vested_value"].notna().any():    # Plaid says exactly what's vested
        vq = holdings["vested_quantity"].fillna(holdings["quantity"])
        vv = holdings["vested_value"].fillna(holdings["value"])
        if (vv < holdings["value"]).any():
            f = (vv / holdings["value"]).where(holdings["value"] > 0, 1.0)
            out = holdings.assign(quantity=vq, value=vv)
            return out.assign(cost_basis=out["cost_basis"] * f) if "cost_basis" in out else out
        return holdings
    total = float(holdings["value"].sum())
    if not current or current <= 0 or total <= float(current) * 1.01:
        return holdings
    f = float(current) / total
    out = holdings.assign(quantity=holdings["quantity"] * f, value=holdings["value"] * f)
    return out.assign(cost_basis=out["cost_basis"] * f) if "cost_basis" in out else out


def sync(conn, keys: dict, only: set[str] | None = None) -> dict:
    """Balances, new transactions and holdings for the linked items (`only` = just these item ids, so linking
    one bank doesn't spend calls refreshing the others). Returns what changed, plus the Plaid accounts that
    still need to be matched to a dashboard account."""
    ours, m, today = db.accounts(conn), mapping(conn), date.today()
    report = {"accounts": [], "unmatched": [], "errors": []}
    todo = [i for i in keys.get("items", []) if only is None or i["item_id"] in only]
    for item in todo:
        try:
            accts = _post("/accounts/get", {"access_token": item["access_token"]}, keys)["accounts"]
        except PlaidError as e:
            report["errors"].append(f"{item['institution']}: {e}")
            _save_item(keys, {**item, "error": str(e), "tried": time.time()})
            keys = load_keys()
            continue
        item = {k: v for k, v in item.items() if k != "error"}
        item.update(accounts=[a["account_id"] for a in accts], synced=time.time(), tried=time.time())
        target = {}
        for a in accts:
            if a["account_id"] not in m and (aid := match_account(a, item["institution"], ours)) is not None:
                link_account(conn, a["account_id"], aid)
                m = mapping(conn)
            t = (m.get(a["account_id"]) or {}).get("account_id")
            if t is None:
                if a["account_id"] not in m:
                    report["unmatched"].append({"plaid_account_id": a["account_id"], "item_id": item["item_id"],
                                                "institution": item["institution"],
                                                "name": a.get("official_name") or a.get("name"), "mask": a.get("mask"),
                                                "type": a.get("type"), "subtype": a.get("subtype")})
                continue
            target[a["account_id"]] = (t, a)
            bal = a["balances"].get("current")
            prev = conn.execute("SELECT balance FROM balances WHERE account_id = ? AND source != ? "
                                "ORDER BY date DESC LIMIT 1", (t, SOURCE)).fetchone()
            if bal is not None and a["type"] == "investment" and prev and prev[0] > 0 and float(bal) < prev[0] / 2:
                name = ours.loc[ours["id"] == t, "name"].iloc[0]      # investments don't halve overnight: wrong match?
                report["errors"].append(f"{item['institution']} ••{a.get('mask')} is worth ${float(bal):,.2f} on Plaid, "
                                        f"but {name} was last ${prev[0]:,.2f} - not saved. Is it the right account?")
            elif bal is not None:
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
                    rows = [{"symbol": _symbol(sec[x["security_id"]]),
                             "description": sec[x["security_id"]].get("name") or "",
                             "quantity": x.get("quantity"), "price": x.get("institution_price"),
                             "value": float(x.get("institution_value") or 0), "cost_basis": x.get("cost_basis"),
                             "vested_quantity": x.get("vested_quantity"), "vested_value": x.get("vested_value")}
                            for x in h["holdings"] if x["account_id"] == pid]
                    if rows:                    # one row per symbol, as the holdings table keeps them
                        df = pd.DataFrame(rows).groupby("symbol", as_index=False, sort=False).agg(
                            description=("description", "first"), quantity=("quantity", "sum"),
                            price=("price", "first"), value=("value", "sum"),
                            cost_basis=("cost_basis", lambda c: c.sum(min_count=1)),
                            vested_quantity=("vested_quantity", lambda c: c.sum(min_count=1)),
                            vested_value=("vested_value", lambda c: c.sum(min_count=1)))
                        owned = owned_part(df, a["balances"].get("current"))
                        db.replace_holdings(conn, aid, today, owned.drop(columns=["vested_quantity", "vested_value"]))
                        if owned is not df:      # the rest is unvested: keep it as today's grants bucket
                            db.replace_grants(conn, aid, today, [
                                {"grant_id": f"unvested {r.symbol}", "type": "Unvested (all grants)",
                                 "symbol": r.symbol, "quantity": r.quantity - o.quantity, "value": r.value - o.value}
                                for r, o in zip(df.itertuples(), owned.itertuples())])
        _save_item(keys, item)
        keys = load_keys()
        for pid, (aid, a) in target.items():
            report["accounts"].append({"account_id": aid, "institution": item["institution"],
                                       "added": counts[pid], "balance": a["balances"].get("current")})
    db.set_setting(conn, "plaid_last_sync", time.time())
    done = {i["item_id"] for i in todo}                               # keep other banks' open questions
    kept = [u for u in db.get_setting(conn, "plaid_unmatched", []) or [] if u.get("item_id") not in done]
    db.set_setting(conn, "plaid_unmatched", kept + report["unmatched"])   # still asked after a refresh
    return report


RETRY_AFTER_S = 24 * 3600


def next_sync(item: dict) -> float:
    """Each bank refreshes a month after its own last refresh (a failed try is retried the next day)."""
    nxt = (item.get("synced") or 0) + SYNC_EVERY_S
    if item.get("error") and item.get("tried"):
        nxt = max(nxt, item["tried"] + RETRY_AFTER_S) if item.get("synced") else item["tried"] + RETRY_AFTER_S
    return nxt


def due_items(keys: dict | None, now: float | None = None) -> set[str]:
    now = now or time.time()
    return {i["item_id"] for i in (keys or {}).get("items", []) if next_sync(i) <= now}


def mark_tried(item_ids: set[str], error: str) -> None:
    keys = load_keys() or {}
    _write({**keys, "items": [{**i, "tried": time.time(), "error": error} if i["item_id"] in item_ids else i
                              for i in keys.get("items", [])]})


def refresh_status(conn, keys: dict | None) -> dict:
    """For each dashboard account fed by Plaid: which bank login, when it last refreshed, any problem."""
    m, out = mapping(conn), {}
    for item in (keys or {}).get("items", []):
        for pid in item.get("accounts", []):
            if (aid := (m.get(pid) or {}).get("account_id")) is not None:
                out[aid] = {"institution": item["institution"], "synced": item.get("synced"),
                            "error": item.get("error"), "next": next_sync(item)}
    return out


def unmatched(conn) -> list[dict]:
    """Plaid accounts from the last sync still waiting for "which of your accounts is this?"."""
    m = mapping(conn)
    return [u for u in db.get_setting(conn, "plaid_unmatched", []) or [] if u["plaid_account_id"] not in m]
