"""Importer registry + the single function that writes a ParsedFile to the DB."""
from __future__ import annotations

import csv

from .. import db
from . import amex, apple_card, barclays, bofa, chase, statements, wealthfront
from .base import KIND_ACCOUNT_TYPES, ParsedFile, UnrecognizedFile

# Add new institutions here; each module exposes parse(content) -> ParsedFile
IMPORTERS = [bofa, chase, wealthfront, amex, barclays, apple_card]


EXCEL_IMPORTERS = [amex]          # the ones that read .xlsx (a zip file); the rest are text CSV readers


def parse_file(content: bytes | str, filename: str = "") -> ParsedFile:
    excel = isinstance(content, bytes) and content[:2] == b"PK"
    for importer in (EXCEL_IMPORTERS if excel else IMPORTERS):
        try:
            return importer.parse(content, filename=filename)
        except (UnrecognizedFile, csv.Error):           # not this bank's file (a CSV reader choking on it too)
            continue
    raise UnrecognizedFile("No importer recognized this file")


def _same_row_other_name(conn, account_id: int, txns):
    """Amex rows have a reference number, but a row exported without one is known by date + description + amount
    instead (also_known_as). A row arriving with its reference renames the copy saved without one (keeping its
    category), and a row without one is skipped if the account already has it - so neither way doubles it."""
    if "also_known_as" not in txns or not len(txns):
        return txns
    keep = []
    for r in txns.itertuples():
        old = f"{account_id}|{r.also_known_as}"
        if r.fingerprint != r.also_known_as:                    # has a reference: take over a copy saved without
            hit = conn.execute("SELECT id FROM transactions WHERE account_id = ? AND (fingerprint = ? OR fingerprint "
                               "LIKE ?) LIMIT 1", (account_id, old, old + "|%")).fetchone()
            if hit and not conn.execute("SELECT 1 FROM transactions WHERE fingerprint = ?",
                                        (f"{account_id}|{r.fingerprint}",)).fetchone():
                conn.execute("UPDATE transactions SET fingerprint = ? WHERE id = ?", (f"{account_id}|{r.fingerprint}", hit[0]))
            keep.append(True)
        else:                                                   # no reference: skip if the account has it already
            when = r.date.isoformat() if hasattr(r.date, "isoformat") else str(r.date)[:10]
            keep.append(not conn.execute("SELECT 1 FROM transactions WHERE account_id = ? AND date = ? AND amount = ? "
                                         "AND description = ?", (account_id, when, float(r.amount), r.description)).fetchone())
    conn.commit()
    return txns[keep]


def apply(conn, parsed: ParsedFile, account_id: int, filename: str) -> dict:
    """Write parsed rows to an account. Safe to run repeatedly on the same file."""
    txns = _same_row_other_name(conn, account_id, parsed.transactions)
    added = db.insert_transactions(conn, account_id, txns, filename) if len(txns) else 0
    for r in parsed.balances.itertuples():
        db.upsert_balance(conn, account_id, r.date, r.balance, filename)
    if len(parsed.holdings):
        db.replace_holdings(conn, account_id, parsed.as_of, parsed.holdings)
        db.upsert_balance(conn, account_id, parsed.as_of, parsed.holdings["value"].sum(), filename)
    db.log_import(conn, account_id, filename, parsed.kind, added)
    return {
        "transactions_added": added,
        "transactions_skipped": len(parsed.transactions) - added,
        "balances": len(parsed.balances) + (1 if len(parsed.holdings) else 0),
        "positions": len(parsed.holdings),
    }


def last4_from_filename(filename: str) -> str | None:
    """Chase names downloads after the account, e.g. Chase1234_Activity_20260926.CSV."""
    import re
    m = re.match(r"(?i)chase(\d{4})_", filename)
    return m.group(1) if m else None


def suggest_csv_account(conn, parsed: ParsedFile, filename: str, candidates) -> tuple[str | None, str]:
    """Which existing account a CSV belongs to, and why. None = can't tell, ask the user.
    1) it shares transactions with an account (overlapping downloads)  2) account digits in the file name
    3) the only account of this kind at this bank."""
    if candidates.empty:
        return None, ""
    if len(parsed.transactions):
        overlap = db.transaction_overlap(conn, [int(i) for i in candidates["id"]],
                                         list(parsed.transactions["fingerprint"]))
        best = max(overlap, key=overlap.get)
        if overlap[best]:
            name = candidates.loc[candidates["id"] == best, "name"].iloc[0]
            return name, f"already has {overlap[best]} of these transactions"
    digits = last4_from_filename(filename) or parsed.account_hint or None
    if digits:
        hit = candidates[candidates["last4"] == digits]
        if len(hit) == 1:
            return hit["name"].iloc[0], f"account ending {digits}, from the file name"
    same_bank = candidates[candidates["institution"] == parsed.institution]
    if len(same_bank) == 1 and (not digits or same_bank["last4"].isna().iloc[0]):
        return same_bank["name"].iloc[0], f"your only {parsed.institution} account of this kind"
    return None, ""


def apply_statement(conn, s: "statements.Statement", account_id: int, filename: str, latest: bool = True) -> None:
    """Save what a PDF statement says about an account. `latest`: this is the account's newest
    statement in the batch, so its rate/payment become the account's current loan terms."""
    if s.as_of and s.balance is not None:
        db.upsert_balance(conn, account_id, s.as_of, abs(s.balance), filename)
    for d, value, exact in s.history:
        (db.upsert_balance if exact else db.add_balance_if_missing)(conn, account_id, d, abs(value), filename)
    if latest and s.as_of and s.holdings:
        import pandas as pd
        db.replace_holdings(conn, account_id, s.as_of, pd.DataFrame(s.holdings))
    if latest and s.as_of:
        db.replace_grants(conn, account_id, s.as_of, s.grants)
    if latest and s.tax_sources:              # retirement plans: how much is pre-tax vs Roth (for taxes later)
        db.set_setting(conn, f"tax_sources:{account_id}", {**s.tax_sources, "as_of": str(s.as_of)})
    db.update_account_details(conn, account_id, last4=s.last4,
                              rate=s.rate if latest and s.kind in ("loan", "credit_card") else None,
                              payment=s.payment if latest and s.kind == "loan" else None)
    if s.transactions:
        if not s.totals_ok:
            return {"error": "transactions not imported: they don't add up to the statement's own totals"}
        return merge_statement_transactions(conn, account_id, s, filename)
    return {}


def merge_statement_transactions(conn, account_id: int, s: "statements.Statement", filename: str) -> dict:
    """Add a statement's transactions without doubling any already there (from a CSV or an earlier import):
    within the statement's period, each (date, amount) on the statement is matched to one already in the
    dashboard; only the unmatched ones are added. Re-importing the same statement adds nothing."""
    from collections import Counter
    import pandas as pd
    dates = [t["date"] for t in s.transactions]
    p_lo, p_hi = s.period or (min(dates), max(dates))
    lo, hi = min(p_lo, min(dates)), max(p_hi, max(dates))   # a card lists purchases made just before the period
    have = Counter((d, round(a, 2)) for d, a in conn.execute(
        "SELECT date, amount FROM transactions WHERE account_id = ? AND date BETWEEN ? AND ?",
        (account_id, lo.isoformat(), hi.isoformat())))
    nth, new = Counter(), []
    for t in s.transactions:
        key = (t["date"].isoformat(), round(t["amount"], 2))
        if have[key] > 0:
            have[key] -= 1
            continue
        nth[key] += 1
        new.append({"date": t["date"], "description": t["description"], "amount": t["amount"],
                    "fingerprint": f"stmt|{key[0]}|{key[1]:.2f}|{nth[key]}"})
    added = db.insert_transactions(conn, account_id, pd.DataFrame(new), filename) if new else 0
    from datetime import timedelta
    # cards: purchases made in the last few days often post after the closing date (they're on the next statement)
    last = p_hi - timedelta(days=4) if s.kind == "credit_card" else p_hi
    left = sum(n for (d, _), n in have.items() if p_lo.isoformat() <= d <= last.isoformat())   # its own period only
    return {"period": (p_lo, p_hi), "on_statement": len(s.transactions), "already_there": len(s.transactions) - len(new),
            "added": added, "only_in_dashboard": left}


__all__ = ["apply_statement", "merge_statement_transactions", "statements", "suggest_csv_account", "last4_from_filename", "KIND_ACCOUNT_TYPES", "ParsedFile", "UnrecognizedFile", "apply", "parse_file"]
