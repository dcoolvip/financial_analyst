"""Barclays credit card CSV downloads.

    Barclays Bank Delaware
    Account Number: XXXXXXXXXXXX1974
    Account Balance as of September 26 2026:    $0.00
    <blank>
    Transaction Date,Description,Category,Amount,Card Last 4 Digits,Purchased by
    08/01/2026,"Payment Received","CREDIT","81.43",="1974",Jane Sample

Unlike most card downloads, the header states the current balance, so no balance needs typing in.
Amounts already use the usual sign (purchases negative, payments positive). The card digits are written
as a spreadsheet formula (="1974") and are cleaned up.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

import pandas as pd

from .base import ParsedFile, UnrecognizedFile, to_money

INSTITUTION = "Barclays"
HEADER_START = "Transaction Date,Description,Category,Amount"


def parse(content: bytes | str, filename: str = "") -> ParsedFile:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or "barclays" not in lines[0].lower():
        raise UnrecognizedFile("Not a Barclays CSV export")
    head = next((i for i, l in enumerate(lines[:15]) if l.startswith(HEADER_START)), None)
    if head is None:
        raise UnrecognizedFile("Barclays file without a transaction table")
    preamble = "\n".join(lines[:head])
    m = re.search(r"Account Number:\s*\S*?(\d{4})\b", preamble)
    last4 = m.group(1) if m else ""
    balances = pd.DataFrame(columns=["date", "balance"])
    m = re.search(r"Account Balance as of\s+([A-Za-z]+ \d{1,2},? \d{4}):\s*(-?\$?-?[\d,]+\.\d{2})", preamble)
    if m:
        on = datetime.strptime(m.group(1).replace(",", ""), "%B %d %Y").date()
        balances = pd.DataFrame({"date": [on], "balance": [to_money(m.group(2))]})   # what you owe

    rows = list(csv.DictReader(io.StringIO("\n".join(lines[head:]))))
    out = []
    for r in rows:
        if not (r.get("Transaction Date") or "").strip():
            continue
        amount = to_money(r["Amount"])
        if amount is None:
            continue
        desc = " ".join(r["Description"].split())
        out.append({"date": datetime.strptime(r["Transaction Date"].strip(), "%m/%d/%Y").date(),
                    "description": desc, "amount": amount})
    df = pd.DataFrame(out, columns=["date", "description", "amount"])
    key = df["date"].astype(str) + "|" + df["description"] + "|" + df["amount"].map("{:.2f}".format)
    df["fingerprint"] = key + "|" + key.groupby(key).cumcount().astype(str)          # identical same-day buys
    return ParsedFile(kind="credit_card", institution=INSTITUTION, transactions=df, balances=balances,
                      as_of=balances["date"].max() if len(balances) else (df["date"].max() if len(df) else None),
                      account_hint=last4)
