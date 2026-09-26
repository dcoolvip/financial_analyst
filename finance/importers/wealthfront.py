"""Wealthfront CSV downloads.

Supported:
  * Cash account activity - Activity -> Download (CSV):  Transaction date,Description,Type,Amount
    Newest first, signed amounts, no balance column. Wealthfront names a full export
    "<account> - All-time.csv"; only then is the running total the real balance, so only then do we
    build balance history from it. A partial date range imports transactions only.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

import pandas as pd

from .base import ParsedFile, UnrecognizedFile, to_money

INSTITUTION = "Wealthfront"
HEADER = ["Transaction date", "Description", "Type", "Amount"]


def parse(content: bytes | str, filename: str = "") -> ParsedFile:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    rows = list(csv.reader(io.StringIO(text.strip())))
    if not rows or [h.strip() for h in rows[0]] != HEADER:
        raise UnrecognizedFile("Not a Wealthfront CSV export")
    df = pd.DataFrame([r[:4] for r in rows[1:] if len(r) >= 4 and r[0].strip()], columns=HEADER)
    df["date"] = df["Transaction date"].map(lambda s: datetime.strptime(s.strip(), "%m/%d/%Y").date())
    df["amount"] = df["Amount"].map(to_money)
    df["description"] = df["Description"].str.strip()
    # Moves between your own Wealthfront accounts only say the other account's name; make them read as transfers
    is_move = df["Type"].str.strip().str.lower() == "transfer"
    df.loc[is_move, "description"] = [("Transfer to " if a < 0 else "Transfer from ") + d
                                       for d, a in zip(df.loc[is_move, "description"], df.loc[is_move, "amount"])]
    df = df.dropna(subset=["amount"]).iloc[::-1].reset_index(drop=True)      # oldest first
    key = df["date"].astype(str) + "|" + df["description"] + "|" + df["Type"] + "|" + df["amount"].map("{:.2f}".format)
    df["fingerprint"] = key + "|" + key.groupby(key).cumcount().astype(str)

    balances, note = pd.DataFrame(columns=["date", "balance"]), ""
    running = df["amount"].cumsum().round(2)
    if re.search(r"all[- ]?time", filename, re.IGNORECASE) and running.min() >= -0.01:
        by_day = df.assign(balance=running).groupby("date")["balance"].last()
        balances = pd.DataFrame({"date": by_day.index, "balance": by_day.values})
    else:
        note = ("This file doesn't start at the account's first deposit, so balances can't be worked out from it. "
                "Download the All-time activity, or add the current balance in Accounts.")
    return ParsedFile(kind="deposit", institution=INSTITUTION,
                      transactions=df[["date", "description", "amount", "fingerprint"]],
                      balances=balances, as_of=df["date"].max() if len(df) else None, note=note)
