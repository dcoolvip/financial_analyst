"""Apple Card CSV downloads (Wallet -> Apple Card -> Card Balance -> monthly statement -> Export Transactions).

    Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By

* Signs are the reverse of banks': Purchase/Debit are positive, Payment/Credit negative - all flipped here.
* The clean Merchant name ("Hulu") is used as the description; for payments the Description
  ("ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN ...") is kept, which reads as a transfer.
* Several people can share an Apple Card (Apple Card Family): one account; the purchaser is noted.
* No balance and no reference numbers in the file: identical same-day rows are real repeat charges and
  are kept apart by their order.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

import pandas as pd

from .base import ParsedFile, UnrecognizedFile, to_money

INSTITUTION = "Apple Card"
REQUIRED = {"Transaction Date", "Clearing Date", "Merchant", "Type", "Amount (USD)"}


def parse(content: bytes | str, filename: str = "") -> ParsedFile:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED <= {h.strip() for h in reader.fieldnames}:
        raise UnrecognizedFile("Not an Apple Card CSV export")
    rows = [r for r in reader if (r.get("Transaction Date") or "").strip()]
    people = {(r.get("Purchased By") or "").strip() for r in rows} - {""}
    out = []
    for r in rows:
        amount = to_money(r["Amount (USD)"])
        if amount is None:
            continue
        kind = r["Type"].strip()
        desc = " ".join((r["Description"] if kind == "Payment" or not r["Merchant"].strip() else r["Merchant"]).split())
        if len(people) > 1 and kind != "Payment" and r.get("Purchased By"):
            desc += f" ({r['Purchased By'].strip()})"
        out.append({"date": datetime.strptime(r["Transaction Date"].strip(), "%m/%d/%Y").date(),
                    "description": desc, "amount": -amount,
                    "key": f"{r['Transaction Date']}|{r['Clearing Date']}|{r['Description'].strip()}|{kind}|{amount:.2f}"})
    df = pd.DataFrame(out, columns=["date", "description", "amount", "key"])
    df["fingerprint"] = df["key"] + "|" + df.groupby("key").cumcount().astype(str)
    return ParsedFile(kind="credit_card", institution=INSTITUTION,
                      transactions=df[["date", "description", "amount", "fingerprint"]],
                      as_of=df["date"].max() if len(df) else None,
                      note="" if len(people) < 2 else
                      f"Shared card ({', '.join(sorted(people))}) - imported as one account; each purchase notes who made it.")
