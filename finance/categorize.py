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

# What each category covers - shown in the app and given to the AI, so both use the same definitions.
CATEGORY_HELP = {
    "Income": "Pay, salary and direct deposits from employers, interest, dividends, tax refunds, card rewards.",
    "Other income": "Other money in: refunds, reimbursements, things you sold, cash deposits.",
    "Mortgage": "Monthly payments on a home loan (principal + interest, plus escrow for property tax and home "
                "insurance when the lender collects it).",
    "Loan payments": "Monthly payments on car, student and personal loans.",
    "Housing": "Rent, HOA dues, property tax paid directly, and home repair, maintenance, pest control, "
               "cleaning and furnishing services.",
    "Utilities": "Electricity, gas, water, trash, internet, and phone/mobile plans.",
    "Groceries": "Supermarkets, grocery and specialty food stores (incl. Asian and Indian markets), "
                 "warehouse clubs.",
    "Dining": "Restaurants, cafes, bakeries, dessert shops, bars, food delivery and takeout.",
    "Transport": "Fuel, EV charging, parking, tolls, rideshare, transit, car service and repair, DMV fees.",
    "Shopping": "Clothing, shoes, electronics, department stores, online retail, home goods, hobby and toy "
                "stores.",
    "Health": "Doctors, dentists, hospitals, pharmacies, vision, therapy, lab tests.",
    "Subscriptions": "Recurring digital services: streaming, apps, cloud storage, news, software.",
    "Insurance": "Car, home, life, health and umbrella insurance premiums paid directly.",
    "Travel": "Flights, hotels, vacation rentals, car rentals, tours, passports and visas.",
    "Entertainment": "Movies, events, tickets, theme parks, games, sports activities, races, museums, parks.",
    "Education": "Tuition, courses, online learning, books for school, test fees.",
    "Kids & childcare": "Daycare, babysitting, camps, kids' classes and activities, school fees.",
    "Personal care": "Hair, nails, spa, gym and fitness memberships, laundry and dry cleaning.",
    "Gifts & donations": "Gifts, charities, fundraisers, religious donations.",
    "Fees & interest": "Bank and card fees, card interest charges, late fees, wire fees, cash-back reversals.",
    "Taxes": "Income tax payments, government fines and court fees, business filings and licences.",
    "Payments to people": "Zelle, Venmo, PayPal or Apple Cash to or from individuals, when the purpose is unknown.",
    "Transfer": "Money moving between your own accounts, credit card bill payments, investing. Not spending.",
    "Other": "Genuinely unclear, e.g. a bare check. Use a category above whenever there's a reasonable guess.",
}
CATEGORIES = list(CATEGORY_HELP)
CATEGORY_VERSION = 2          # bump when categories or their definitions change -> offer an AI re-check

_CUT = re.compile(r"\s(ID|INDN|CO ID|CONF|CONFIRMATION|TRN|DATE|TIME|SEQ|REF)\s*[:#].*$", re.IGNORECASE)
_NOISE_V1 = re.compile(r"\b(CHECKCARD|PURCHASE|POS|DEBIT|RECURRING|MOBILE)\b", re.IGNORECASE)
_NOISE = re.compile(r"\b(CHECKCARD|MOBILE PURCHASE|PURCHASE|POS|DEBIT|RECURRING)\b", re.IGNORECASE)  # not "US MOBILE"


def _merchant_key_v1(description: str) -> str:
    """The first version: dropped every word with a digit ("99 RANCH" -> "RANCH"). Kept to upgrade rules."""
    s = _CUT.sub("", f" {description}").upper()
    s = s.replace("DES:", " ")
    s = " ".join(t for t in s.split() if not re.search(r"\d", t))
    s = _NOISE_V1.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()[:60] or description.upper()[:60]


def _is_code(token: str) -> bool:
    """Order/store/building numbers, dates, amounts, card digits, phone numbers - not part of a merchant's
    name. Short names with a digit or two stay: 99 RANCH, K1 SPEED, O2, 76, 7-ELEVEN."""
    digits = sum(c.isdigit() for c in token)
    if not digits:
        return False
    return (digits >= 3 or token[0] in "#$" or bool(re.search(r"\d[/-]\d", token))
            or (len(token) >= 4 and digits >= 2))            # HS01, IL04: a store or building code


def merchant_key(description: str) -> str:
    s = _CUT.sub("", f" {description}").upper()
    s = s.replace("DES:", " ").replace("*", " ")
    s = " ".join(t for t in s.split() if not _is_code(t))
    s = _NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()[:60] or description.upper()[:60]


def upgrade_rule_keys(conn, descriptions) -> int:
    """Carry each saved category over to the merchant's new, more precise key. When an old key lumped
    different merchants together (all of "99 RANCH", "LA RANCH" were "RANCH"), or the AI had said Other,
    the rule isn't carried: those merchants get asked about again with their real names. Returns rules moved."""
    _ensure(conn)
    old_rules = rules(conn)
    targets: dict[str, set] = {}
    for d in set(descriptions):
        old, new = _merchant_key_v1(d), merchant_key(d)
        if old in old_rules:
            targets.setdefault(old, set()).add(new)
    moved = 0
    for old, news in targets.items():
        cat, src = old_rules[old]
        if len(news) == 1 and news != {old} and (src == "user" or cat != "Other"):
            new = next(iter(news))
            if new not in old_rules or (src == "user" and old_rules[new][1] != "user"):
                conn.execute("INSERT OR REPLACE INTO merchant_rules VALUES (?, ?, ?)", (new, cat, src))
                moved += 1
    conn.commit()
    return moved


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


SYSTEM = """You categorize personal bank and credit card transactions for a household budget.
Each item is a cleaned merchant string from a US bank or card statement (store numbers, dates and IDs
removed), whether money came in or went out, a typical amount, and how many times it occurs.
Pick exactly one category per item. The categories and what each covers:
""" + "\n".join(f"- {c}: {d}" for c, d in CATEGORY_HELP.items()) + """

Make your best guess from the name - most merchants are ordinary businesses you can place from their
words or what they're known for (a "RANCH" market, "K1 SPEED" go-karts, "US MOBILE" phone plans,
"NCOURT" / "COURT EPAY" court fees, "ZENBUSINESS" business filings, a "5K FUN RUN" event).
Payment-processor prefixes (SQ, TST, PAYPAL, APLPAY, SP) are not the merchant - look at what follows.
Transfer is only for the person's own money moving (own savings/brokerage, card bill payments).
Use Other only when there is truly no reasonable guess."""

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
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
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


def uncategorized_merchants(conn, txns: pd.DataFrame, is_transfer, recheck: bool = False) -> pd.DataFrame:
    """Merchants with no rule yet (skipping ones the transfer detector already handles).
    recheck=True: also merchants the AI already did - everything except your own choices."""
    if txns.empty:
        return pd.DataFrame(columns=["merchant", "amount", "n"])
    known = {m for m, (_, src) in rules(conn).items() if src == "user" or not recheck}
    t = txns[~txns["description"].map(is_transfer)].assign(merchant=txns["description"].map(merchant_key))
    t = t[~t["merchant"].isin(known)]
    return (t.groupby("merchant")["amount"].agg(amount="median", n="size").reset_index()
            .sort_values("n", ascending=False))


def needs_recheck(conn) -> bool:
    """The categories or their definitions changed since the AI last went over everything."""
    _ensure(conn)
    row = conn.execute("SELECT value FROM settings WHERE key = 'category_version'").fetchone()
    has_ai = conn.execute("SELECT 1 FROM merchant_rules WHERE source = 'ai' LIMIT 1").fetchone()
    return bool(has_ai) and (row is None or int(json.loads(row[0])) < CATEGORY_VERSION)


def auto_categorize(conn, txns: pd.DataFrame, is_transfer, progress=None, recheck: bool = False) -> int:
    """Classify every merchant that has no rule yet (recheck=True: every merchant except your own choices).
    Returns number of merchants categorized. Merchants the model leaves out are asked about once more."""
    todo = uncategorized_merchants(conn, txns, is_transfer, recheck=recheck)
    done = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo.iloc[start:start + BATCH].reset_index(drop=True)
        pending = list(range(len(chunk)))
        for _attempt in range(2):
            items = [{"id": i, "merchant": chunk.at[i, "merchant"],
                      "direction": "in" if chunk.at[i, "amount"] > 0 else "out",
                      "typical_amount": round(abs(chunk.at[i, "amount"])), "times": int(chunk.at[i, "n"])}
                     for i in pending]
            answered = {i: c for i, c in _call_model(items).items() if i in pending and c in CATEGORIES}
            for i, cat in answered.items():
                set_rule(conn, chunk.at[i, "merchant"], cat, source="ai")
            done += len(answered)
            pending = [i for i in pending if i not in answered]
            if not pending:
                break
        if progress:
            progress(min(start + BATCH, len(todo)) / len(todo))
    if recheck or not needs_recheck(conn):
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('category_version', ?)",
                     (json.dumps(CATEGORY_VERSION),))
        conn.commit()
    return done
