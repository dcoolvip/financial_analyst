"""Chase CSV downloads.

Supported:
  * Checking / savings - Account activity -> Download -> CSV
      Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
  * Credit cards       - Account activity -> Download -> CSV
      Transaction Date,Post Date,Description,Category,Type,Amount,Memo
Chase lists newest first; amounts are already signed (money out is negative).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

import pandas as pd

from .base import ParsedFile, UnrecognizedFile, to_money

INSTITUTION = "Chase"


def _rows(content: bytes | str) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    reader = csv.reader(io.StringIO(text.strip()))
    header = [h.strip() for h in next(reader, [])]
    # Chase checking files often end every row with a stray comma: ignore unnamed extra columns
    return header, [dict(zip(header, (c.strip() for c in row))) for row in reader if any(c.strip() for c in row)]


def _mdy(s: str):
    return datetime.strptime(s.strip(), "%m/%d/%Y").date()


def parse(content: bytes | str) -> ParsedFile:
    header, rows = _rows(content)
    cols = set(header)
    if {"Posting Date", "Description", "Amount", "Balance"} <= cols:
        return _deposit(rows)
    if {"Transaction Date", "Post Date", "Description", "Amount"} <= cols:
        return _card(rows)
    raise UnrecognizedFile("Not a recognized Chase CSV export")


def _deposit(rows: list[dict]) -> ParsedFile:
    df = pd.DataFrame(rows)
    df = df[df["Posting Date"].str.match(r"\d{1,2}/\d{1,2}/\d{4}")].copy()
    df["date"] = df["Posting Date"].map(_mdy)
    df["amount"] = df["Amount"].map(to_money)
    df["running"] = df["Balance"].map(to_money)
    df["description"] = df["Description"].str.strip()
    df = df.dropna(subset=["amount"])
    # Newest first: the first row of each day carries that day's closing balance
    bal = df.dropna(subset=["running"]).groupby("date", sort=True)["running"].first()
    balances = pd.DataFrame({"date": bal.index, "balance": bal.values})
    df["fingerprint"] = (df["date"].astype(str) + "|" + df["description"] + "|" + df["amount"].map("{:.2f}".format)
                         + "|" + df["running"].map(lambda v: "" if pd.isna(v) else f"{v:.2f}"))
    return ParsedFile(kind="deposit", institution=INSTITUTION,
                      transactions=df[["date", "description", "amount", "fingerprint"]].reset_index(drop=True),
                      balances=balances.reset_index(drop=True),
                      as_of=balances["date"].max() if len(balances) else None)


def _card(rows: list[dict]) -> ParsedFile:
    df = pd.DataFrame(rows)
    df = df[df["Transaction Date"].str.match(r"\d{1,2}/\d{1,2}/\d{4}")].copy()
    df["date"] = df["Transaction Date"].map(_mdy)
    df["amount"] = df["Amount"].map(to_money)
    df["description"] = df["Description"].str.strip()
    df = df.dropna(subset=["amount"])
    key = (df["date"].astype(str) + "|" + df["Post Date"] + "|" + df["description"] + "|"
           + df["amount"].map("{:.2f}".format))
    df["fingerprint"] = key + "|" + key.groupby(key).cumcount().astype(str)   # identical same-day purchases
    return ParsedFile(kind="credit_card", institution=INSTITUTION,
                      transactions=df[["date", "description", "amount", "fingerprint"]].reset_index(drop=True),
                      as_of=df["date"].max() if len(df) else None)
