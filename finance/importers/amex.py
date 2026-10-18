"""American Express downloads (Statements & Activity -> Download -> Excel or CSV, with all details).

    Date,Description,Card Member,Account #,Amount,Extended Details,Appears On Your Statement As,
    Address,City/State,Zip Code,Country,Reference,Category

* Amex signs are the opposite of banks': a purchase is POSITIVE, a payment/credit negative - flipped here.
* Several fields span multiple lines inside quotes (the csv module handles that).
* One file can include additional cards on the same account (e.g. -07017 and -02000): they share one
  balance, so they import as one account; the card member is noted on each transaction.
* Every row has a unique Reference, used to skip duplicates across overlapping downloads.
"""
from __future__ import annotations

import collections
import csv
import io
from datetime import datetime

import pandas as pd

from .base import ParsedFile, UnrecognizedFile, to_money

INSTITUTION = "American Express"
REQUIRED = {"Date", "Description", "Amount", "Card Member", "Account #"}


def _excel_to_csv(content: bytes) -> str:
    """Amex's Excel download (Download -> Excel) has the CSV's columns under a few title rows: find the header row
    and hand the rest to the CSV reader, so both formats follow the same rules."""
    try:
        raw = pd.read_excel(io.BytesIO(content), header=None, dtype=str)
    except ImportError as e:
        raise UnrecognizedFile("Excel files need the openpyxl package: ~/.venvs/financial-analyst/bin/pip install "
                               "openpyxl") from e
    except Exception as e:
        raise UnrecognizedFile("Not an Excel file this reader understands") from e
    return header_table_to_csv(raw)


def header_table_to_csv(raw: pd.DataFrame) -> str:
    for i in range(min(len(raw), 30)):
        cells = [str(c).strip() for c in raw.iloc[i].tolist()]
        if REQUIRED <= set(cells):
            table = raw.iloc[i + 1:].copy()
            table.columns = cells
            table = table.loc[:, [c for c in table.columns if c and c != "nan"]].fillna("")
            if "Date" in table:              # Excel may give dates as 2026-09-24 00:00:00; the CSV rules want 09/24/2026
                table["Date"] = pd.to_datetime(table["Date"], errors="coerce").dt.strftime("%m/%d/%Y").fillna("")
            return table.to_csv(index=False)
    raise UnrecognizedFile("Not an American Express Excel export")


def parse(content: bytes | str, filename: str = "") -> ParsedFile:
    if isinstance(content, bytes) and content[:2] == b"PK":        # .xlsx is a zip file
        content = _excel_to_csv(content)
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED <= {h.strip() for h in reader.fieldnames}:
        raise UnrecognizedFile("Not an American Express CSV export")
    rows = [r for r in reader if (r.get("Date") or "").strip()]
    members = {r["Card Member"].strip() for r in rows}
    out = []
    for r in rows:
        amount = to_money(r["Amount"])
        if amount is None:
            continue
        desc = " ".join(r["Description"].split())
        who = r["Card Member"].strip().title() if len(members) > 1 else None
        ref = (r.get("Reference") or "").strip().strip("'")
        legacy = f"{desc} ({who})" if who else desc          # fingerprints of earlier imports used this
        out.append({"date": datetime.strptime(r["Date"].strip(), "%m/%d/%Y").date(), "description": desc,
                    "amount": -amount, "purchaser": who, "bank_category": (r.get("Category") or "").strip() or None,
                    "fingerprint": ref or f"{r['Date']}|{legacy}|{amount:.2f}"})
    df = pd.DataFrame(out, columns=["date", "description", "amount", "purchaser", "bank_category", "fingerprint"])
    if len(df) and not df["fingerprint"].is_unique:                      # only possible without references
        df["fingerprint"] = df["fingerprint"] + "|" + df.groupby("fingerprint").cumcount().astype(str)
    # The account's own card: the one payments are made on (else the most used)
    payers = collections.Counter(r["Account #"] for r in rows if (to_money(r["Amount"]) or 0) < 0)
    cards = payers or collections.Counter(r["Account #"] for r in rows)
    hint = "".join(ch for ch in cards.most_common(1)[0][0] if ch.isdigit())[-4:] if cards else ""
    return ParsedFile(kind="credit_card", institution=INSTITUTION, transactions=df,
                      as_of=df["date"].max() if len(df) else None, account_hint=hint,
                      note="" if len(members) < 2 else
                      f"Includes {len(members)} cards on this account ({', '.join(sorted(m.title() for m in members))}) "
                      "- imported together, since they share one balance.")
