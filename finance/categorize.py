"""AI categorization of transactions, with rules that remember.

How it fits together:
  1. Every description is reduced to a merchant key ("VENMO DES:PAYMENT ID:105... INDN:Name"
     -> "VENMO PAYMENT"). Names, IDs, dates and card numbers are stripped here, so they
     are never sent anywhere.
  2. Each *merchant* (not each transaction) is classified once by Claude and saved as a
     rule. Hundreds of transactions typically collapse to a few dozen merchants.
  3. When you change a category in the app, that becomes a 'user' rule for the merchant,
     which always wins and is never overwritten by the AI.

Model calls go through Floodgate with your AppleConnect session - see _call_model.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

import pandas as pd


MODEL = "anthropic.claude-opus-5"   # Floodgate model id
BATCH = 80

CATEGORIES = [
    "Income", "Housing", "Utilities", "Groceries", "Dining", "Transport", "Shopping", "Health",
    "Subscriptions", "Insurance", "Travel", "Entertainment", "Education", "Kids & childcare",
    "Personal care", "Gifts & donations", "Fees & interest", "Taxes", "Payments to people",
    "Transfer", "Other",
]

_CUT = re.compile(r"\s(ID|INDN|CO ID|CONF|CONFIRMATION|TRN|DATE|TIME|SEQ|REF)\s*[:#].*$", re.IGNORECASE)
_NOISE = re.compile(r"\b(CHECKCARD|PURCHASE|POS|DEBIT|RECURRING|MOBILE)\b", re.IGNORECASE)


def merchant_key(description: str) -> str:
    s = _CUT.sub("", f" {description}").upper()
    s = s.replace("DES:", " ")
    s = " ".join(t for t in s.split() if not re.search(r"\d", t))   # dates, card #s, amounts
    s = _NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()[:60] or description.upper()[:60]


# --- rules storage --------------------------------------------------------------

def _ensure(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS merchant_rules (
        merchant TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        source   TEXT NOT NULL       -- 'user' (always wins) or 'ai'
    )""")


def rules(conn) -> dict[str, tuple[str, str]]:
    """merchant -> (category, source)."""
    _ensure(conn)
    return {m: (c, src) for m, c, src in conn.execute("SELECT merchant, category, source FROM merchant_rules")}


def set_rule(conn, merchant: str, category: str, source: str = "user") -> None:
    _ensure(conn)
    if source == "ai":  # never clobber something the user chose
        conn.execute("INSERT OR IGNORE INTO merchant_rules VALUES (?, ?, 'ai')", (merchant, category))
        conn.execute("UPDATE merchant_rules SET category = ? WHERE merchant = ? AND source = 'ai'",
                     (category, merchant))
    else:
        conn.execute("""INSERT INTO merchant_rules VALUES (?, ?, 'user')
                        ON CONFLICT(merchant) DO UPDATE SET category = excluded.category, source = 'user'""",
                     (merchant, category))
    conn.commit()


# --- the model call -------------------------------------------------------------
#
# Same path as adp-status-generator: an AppleConnect OIDC token, sent as a Bearer token to
# Floodgate's Anthropic surface. Needs an active AppleConnect session; billed to your personal
# Floodgate quota (a few cents per full run). Floodgate serves Claude via Vertex, which rejects
# the server-side `fallbacks` parameter, so refusals are handled by raising instead.

FLOODGATE_URL = "https://floodgate.g.apple.com/api/anthropic"
_OIDC_CLIENT_ID = "hvys3fcwcteqrvw3qzkvtk86viuoqv"
_OIDC_SCOPES = "openid,dsid,accountname,email,groups"
_TOKEN_TTL_S = 600
_token_cache = {"token": "", "at": 0.0}


def _oidc_token() -> str:
    if _token_cache["token"] and time.monotonic() - _token_cache["at"] < _TOKEN_TTL_S:
        return _token_cache["token"]
    try:
        out = subprocess.run(
            ["appleconnect", "getToken", "-t", "oauth", "-G", "pkce", "-C", _OIDC_CLIENT_ID,
             "-o", _OIDC_SCOPES, "--interactivity-type", "none"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise RuntimeError("Couldn't get an AppleConnect token. Sign in to AppleConnect and try again.") from e
    token = next((line.split()[-1] for line in out.splitlines() if "id-token" in line), "")
    if not token:
        raise RuntimeError("AppleConnect returned no id-token")
    _token_cache.update(token=token, at=time.monotonic())
    return token


def available() -> bool:
    if os.environ.get("FINANCE_NO_AI") == "1":   # tests / offline use: never call the model
        return False
    return shutil.which("appleconnect") is not None


SYSTEM = f"""You categorize personal bank and credit card transactions for a household budget.
Each item is a cleaned merchant string from a US bank statement, whether money came in or went out,
and a typical amount. Pick exactly one category per item from: {", ".join(CATEGORIES)}.

Guidance:
- Income: payroll, salary, direct deposits from employers, interest, dividends, tax refunds.
- Transfer: money moving between the person's own accounts (own savings, own brokerage such as
  Wealthfront/Fidelity/Schwab/Robinhood, credit card bill payments, "Apple Cash" top-ups/cash-outs).
  These are excluded from spending, so only use it when it is clearly the person's own money moving.
- Payments to people: Zelle/Venmo/Apple Cash/PayPal to or from individuals when the purpose is unknown.
- Housing: mortgage, rent, HOA, property tax, home repair and maintenance services.
- Utilities: power, gas, water, internet, phone.
- Fees & interest: bank fees, wire fees, late fees, finance charges.
- Other: genuinely unclear (for example a bare "CHECK")."""

_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "category": {"type": "string", "enum": CATEGORIES}},
        "required": ["id", "category"], "additionalProperties": False,
    }}},
    "required": ["results"], "additionalProperties": False,
}


def _call_model(items: list[dict]) -> dict[int, str]:
    import anthropic
    import truststore

    truststore.inject_into_ssl()  # Apple corporate CA lives in the macOS keychain
    client = anthropic.Anthropic(auth_token=_oidc_token(), api_key=None, base_url=FLOODGATE_URL)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(items)}],
        )
    except anthropic.AuthenticationError:
        _token_cache["token"] = ""   # stale token: next attempt fetches a fresh one
        raise RuntimeError("Floodgate rejected the token. Try again (AppleConnect may need a sign-in).")
    except anthropic.APIConnectionError as e:
        raise RuntimeError("Can't reach Floodgate. Are you on the Apple network / VPN?") from e
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Categorization stopped early ({response.stop_reason})")
    text = next(b.text for b in response.content if b.type == "text")
    return {r["id"]: r["category"] for r in json.loads(text)["results"]}


def uncategorized_merchants(conn, txns: pd.DataFrame, is_transfer) -> pd.DataFrame:
    """Merchants with no rule yet (skipping ones the transfer detector already handles)."""
    if txns.empty:
        return pd.DataFrame(columns=["merchant", "amount", "n"])
    known = rules(conn)
    t = txns[~txns["description"].map(is_transfer)].assign(merchant=txns["description"].map(merchant_key))
    t = t[~t["merchant"].isin(known)]
    return (t.groupby("merchant")["amount"].agg(amount="median", n="size").reset_index()
            .sort_values("n", ascending=False))


def auto_categorize(conn, txns: pd.DataFrame, is_transfer, progress=None) -> int:
    """Classify every merchant that has no rule yet. Returns number of merchants categorized."""
    todo = uncategorized_merchants(conn, txns, is_transfer)
    done = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo.iloc[start:start + BATCH].reset_index(drop=True)
        items = [{"id": i, "merchant": r.merchant, "direction": "in" if r.amount > 0 else "out",
                  "typical_amount": round(abs(r.amount))} for i, r in chunk.iterrows()]
        for i, cat in _call_model(items).items():
            if 0 <= i < len(chunk):
                set_rule(conn, chunk.at[i, "merchant"], cat, source="ai")
                done += 1
        if progress:
            progress(min(start + BATCH, len(todo)) / len(todo))
    return done
