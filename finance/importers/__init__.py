"""Importer registry + the single function that writes a ParsedFile to the DB."""
from __future__ import annotations

from .. import db
from . import bofa
from .base import KIND_ACCOUNT_TYPES, ParsedFile, UnrecognizedFile

# Add new institutions here; each module exposes parse(content) -> ParsedFile
IMPORTERS = [bofa]


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


__all__ = ["KIND_ACCOUNT_TYPES", "ParsedFile", "UnrecognizedFile", "apply", "parse_file"]
