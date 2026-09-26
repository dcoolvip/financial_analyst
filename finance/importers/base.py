"""Common shape every importer produces, regardless of institution or source.

A future API-based source (SimpleFIN, Teller, ...) just needs to return a
ParsedFile too - nothing downstream changes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

TXN_COLUMNS = ["date", "description", "amount", "fingerprint"]
BALANCE_COLUMNS = ["date", "balance"]
HOLDING_COLUMNS = ["symbol", "description", "quantity", "price", "value"]

# Which account types each kind of file may be imported into
KIND_ACCOUNT_TYPES = {
    "deposit": ["checking", "savings"],
    "credit_card": ["credit_card"],
    "holdings": ["brokerage", "retirement"],
}


@dataclass
class ParsedFile:
    kind: str                       # key of KIND_ACCOUNT_TYPES
    institution: str
    transactions: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=TXN_COLUMNS))
    balances: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=BALANCE_COLUMNS))
    holdings: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=HOLDING_COLUMNS))
    as_of: date | None = None
    account_hint: str = ""          # e.g. last 4 digits, if the file reveals it
    note: str = ""                  # something the user should know about this file (shown on import)

    @property
    def summary(self) -> str:
        parts = [f"{len(self.transactions)} transactions"] if len(self.transactions) else []
        if len(self.balances):
            parts.append(f"{len(self.balances)} daily balances")
        if len(self.holdings):
            parts.append(f"{len(self.holdings)} positions")
        return ", ".join(parts) or "no rows"


class UnrecognizedFile(ValueError):
    pass


_MONEY_JUNK = re.compile(r"[,$\s\"]")


def to_money(value) -> float | None:
    """'1,234.56' / '$1,234.56' / '(12.00)' / '' -> float or None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = _MONEY_JUNK.sub("", str(value))
    if s in ("", "-", "--", "nan"):
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negative else n
