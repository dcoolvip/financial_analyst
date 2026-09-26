"""Importer registry + the single function that writes a ParsedFile to the DB."""
from __future__ import annotations

from .. import db
from . import bofa, chase, statements
from .base import KIND_ACCOUNT_TYPES, ParsedFile, UnrecognizedFile

# Add new institutions here; each module exposes parse(content) -> ParsedFile
IMPORTERS = [bofa, chase]


def parse_file(content: bytes | str) -> ParsedFile:
    for importer in IMPORTERS:
        try:
            return importer.parse(content)
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


__all__ = ["apply_statement", "statements", "KIND_ACCOUNT_TYPES", "ParsedFile", "UnrecognizedFile", "apply", "parse_file"]
