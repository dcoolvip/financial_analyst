"""Plaid: the API keys, and which of the household's institutions Plaid can connect with the current access level.

The keys live in their own file in the data folder (plaid.json, readable only by this Mac user), never in the
code, the database or the import snapshots. Calls go straight to Plaid's REST API with the standard library.

Plaid access levels: Limited Production connects institutions that sign in through Plaid directly; institutions
that send you to their own sign-in page ("OAuth") need full production access.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import paths

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


def save_keys(client_id: str, secret: str, env: str = "production") -> None:
    p = keys_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # private from the first byte
    with os.fdopen(fd, "w") as f:
        json.dump({"client_id": client_id.strip(), "secret": secret.strip(), "env": env}, f)
    os.chmod(p, 0o600)


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
