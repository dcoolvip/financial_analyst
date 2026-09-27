"""Importer registry + the single function that writes a ParsedFile to the DB."""
from __future__ import annotations

from .. import db
from . import amex, apple_card, barclays, bofa, chase, statements, wealthfront
from .base import KIND_ACCOUNT_TYPES, ParsedFile, UnrecognizedFile

# Add new institutions here; each module exposes parse(content) -> ParsedFile
IMPORTERS = [bofa, chase, wealthfront, amex, barclays, apple_card]


def parse_file(content: bytes | str, filename: str = "") -> ParsedFile:
    for importer in IMPORTERS:
        try:
            return importer.parse(content, filename=filename)
        except UnrecognizedFile:
            continue
    raise UnrecognizedFile("No importer recognized this file")


def apply(conn, parsed: ParsedFile, account_id: int, filename: str) -> dict:
    """Write parsed rows to an account. Safe to run repeatedly on the same file."""
    added = db.insert_transactions(conn, account_id, parsed.transactions, filename) if len(parsed.transactions) else 0
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
    db.update_account_details(conn, account_id, last4=s.last4,
                              rate=s.rate if latest and s.kind in ("loan", "credit_card") else None,
                              payment=s.payment if latest and s.kind == "loan" else None)


__all__ = ["apply_statement", "statements", "suggest_csv_account", "last4_from_filename", "KIND_ACCOUNT_TYPES", "ParsedFile", "UnrecognizedFile", "apply", "parse_file"]
