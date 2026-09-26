"""Turn an edited Accounts table into database edits (kept out of the UI so it can be tested)."""
from __future__ import annotations

import pandas as pd


def _num(v):
    return None if v is None or pd.isna(v) else float(v)


def to_display(balance, is_liability: bool):
    """Balances are shown the way banks show them: assets as what you have, cards and loans as what you
    owe (positive). A card in credit is negative. Same as stored, so a number copied from a bank app is right."""
    return None if balance is None or pd.isna(balance) else balance


def to_stored(shown: float, is_liability: bool) -> float:
    return shown


def balance_edits(before: pd.DataFrame, after: pd.DataFrame) -> dict[int, float]:
    """{account_id: value to store} for rows whose Balance cell was edited."""
    out = {}
    for b, a in zip(before.itertuples(), after.itertuples()):
        if _num(a.shown_balance) is not None and _num(a.shown_balance) != _num(b.shown_balance):
            out[int(b.id)] = to_stored(_num(a.shown_balance), bool(b.is_liability))
    return out


def account_edits(before: pd.DataFrame, after: pd.DataFrame, label_to_type: dict[str, str],
                  terms: bool) -> dict[int, dict]:
    """{account_id: {name?, type?, rate?, payment?}} for rows that changed. `terms`: the rate/payment
    columns were shown (and so may have been edited). Rate is shown in percent, stored as a fraction."""
    edits = {}
    for b, a in zip(before.itertuples(), after.itertuples()):
        f = {}
        if a.name != b.name:
            f["name"] = a.name
        if a.Type != b.Type:
            f["type"] = label_to_type.get(a.Type, b.type)
        if terms and _num(a.rate_pct) != _num(b.rate_pct):
            f["rate"] = None if _num(a.rate_pct) is None else _num(a.rate_pct) / 100
        if terms and _num(a.payment) != _num(b.payment):
            f["payment"] = _num(a.payment)
        if f:
            edits[int(b.id)] = f
    return edits
