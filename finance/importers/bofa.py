"""Bank of America / Merrill CSV exports.

Supported:
  * Checking & savings  - Accounts > Download > "Microsoft Excel format" (.csv)
  * Credit cards        - Download transactions > .csv
  * Merrill holdings    - Positions/holdings export (.csv). Merrill's layout
                          varies by page, so this parser locates columns by name.
Loans (mortgage, auto, HELOC) have no CSV export - enter their balance manually.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

import pandas as pd

from .base import ParsedFile, UnrecognizedFile, to_money

INSTITUTION = "Bank of America"
_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")


def _lines(content: bytes | str) -> list[str]:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _find_header(lines: list[str], *required: str) -> int | None:
    for i, line in enumerate(lines[:60]):
        low = line.lower()
        if all(r.lower() in low for r in required):
            return i
    return None


def _mdy(s: str) -> date:
    return datetime.strptime(s.strip(), "%m/%d/%Y").date()


def parse(content: bytes | str) -> ParsedFile:
    lines = _lines(content)
    if (i := _find_header(lines, "date", "description", "amount", "running bal")) is not None:
        return _parse_deposit(lines, i)
    if (i := _find_header(lines, "posted date", "payee", "amount")) is not None:
        return _parse_credit_card(lines, i)
    if (i := _find_header(lines, "symbol", "quantity")) is not None:
        return _parse_holdings(lines, i)
    raise UnrecognizedFile("Not a recognized Bank of America / Merrill CSV export")


def _parse_deposit(lines: list[str], header: int) -> ParsedFile:
    df = pd.read_csv(io.StringIO("\n".join(lines[header:])), dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    df = df[df["Date"].str.match(r"\d{1,2}/\d{1,2}/\d{4}")].copy()
    df["date"] = df["Date"].map(_mdy)
    df["amount"] = df["Amount"].map(to_money)
    df["running"] = df["Running Bal."].map(to_money)
    df["description"] = df["Description"].str.strip()

    # Daily end-of-day balance = last running balance of each day (file is chronological)
    bal = df.dropna(subset=["running"]).groupby("date", sort=True)["running"].last()
    balances = pd.DataFrame({"date": bal.index, "balance": bal.values})

    # The summary block's ending balance is authoritative for the last day
    for line in lines[:header]:
        if line.lower().startswith("ending balance") and (m := _DATE_RE.search(line)):
            end_amt = to_money(next(csv.reader([line]))[-1])
            if end_amt is not None:
                end = _mdy(m.group(1))
                balances = pd.concat([balances[balances["date"] != end],
                                      pd.DataFrame({"date": [end], "balance": [end_amt]})])

    txns = df.dropna(subset=["amount"]).copy()
    # Running balance makes otherwise-identical same-day rows distinct
    txns["fingerprint"] = (txns["date"].astype(str) + "|" + txns["description"] + "|"
                           + txns["amount"].map("{:.2f}".format) + "|" + txns["running"].map(lambda v: f"{v:.2f}" if v is not None else ""))
    return ParsedFile(
        kind="deposit",
        institution=INSTITUTION,
        transactions=txns[["date", "description", "amount", "fingerprint"]].reset_index(drop=True),
        balances=balances.sort_values("date").reset_index(drop=True),
        as_of=balances["date"].max() if len(balances) else None,
    )


def _parse_credit_card(lines: list[str], header: int) -> ParsedFile:
    df = pd.read_csv(io.StringIO("\n".join(lines[header:])), dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    df = df[df["Posted Date"].str.match(r"\d{1,2}/\d{1,2}/\d{4}")].copy()
    df["date"] = df["Posted Date"].map(_mdy)
    df["amount"] = df["Amount"].map(to_money)
    df["description"] = df["Payee"].str.strip()
    df = df.dropna(subset=["amount"])
    ref = df["Reference Number"].str.strip() if "Reference Number" in df else pd.Series("", index=df.index)
    # Reference number is unique per transaction; fall back to content + occurrence index
    fallback = (df["date"].astype(str) + "|" + df["description"] + "|" + df["amount"].map("{:.2f}".format))
    fallback = fallback + "|" + fallback.groupby(fallback).cumcount().astype(str)
    df["fingerprint"] = ref.where(ref != "", fallback)
    return ParsedFile(
        kind="credit_card",
        institution=INSTITUTION,
        transactions=df[["date", "description", "amount", "fingerprint"]].reset_index(drop=True),
        as_of=df["date"].max() if len(df) else None,
    )


_VALUE_COLS = ["value", "current value", "market value", "value ($)", "current value ($)"]
_PRICE_COLS = ["price", "current price", "last price", "price ($)"]
_DESC_COLS = ["description", "security description", "name"]


def _pick(columns: list[str], candidates: list[str]) -> str | None:
    by_lower = {c.lower().strip(): c for c in columns}
    for cand in candidates:
        if cand in by_lower:
            return by_lower[cand]
    for cand in candidates:  # loose match, e.g. "Value ($) 09/25/2026"
        for low, orig in by_lower.items():
            if low.startswith(cand):
                return orig
    return None


def _parse_holdings(lines: list[str], header: int) -> ParsedFile:
    df = pd.read_csv(io.StringIO("\n".join(lines[header:])), dtype=str, keep_default_na=False,
                     on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]
    sym_col = _pick(list(df.columns), ["symbol", "symbol/cusip"])
    qty_col = _pick(list(df.columns), ["quantity", "qty"])
    val_col = _pick(list(df.columns), _VALUE_COLS)
    if not (sym_col and val_col):
        raise UnrecognizedFile(f"Holdings file is missing a symbol or value column: {list(df.columns)}")
    price_col = _pick(list(df.columns), _PRICE_COLS)
    desc_col = _pick(list(df.columns), _DESC_COLS)

    out = pd.DataFrame({
        "symbol": df[sym_col].str.strip(),
        "description": df[desc_col].str.strip() if desc_col else "",
        "quantity": df[qty_col].map(to_money) if qty_col else None,
        "price": df[price_col].map(to_money) if price_col else None,
        "value": df[val_col].map(to_money),
    }).dropna(subset=["value"])
    total_row = out["symbol"].str.lower().str.contains("total") | out["description"].str.lower().str.startswith("total")
    out = out[~total_row]
    out.loc[out["symbol"] == "", "symbol"] = "CASH"

    as_of = None
    for line in lines[:header]:
        if (m := _DATE_RE.search(line)):
            as_of = _mdy(m.group(1))
            break
    return ParsedFile(
        kind="holdings",
        institution=INSTITUTION,
        holdings=out.reset_index(drop=True),
        as_of=as_of or date.today(),
    )
