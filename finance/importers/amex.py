"""American Express CSV downloads (Statements & Activity -> Download -> CSV, with all details).

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


def parse(content: bytes | str, filename: str = "") -> ParsedFile:
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
        if len(members) > 1:
            desc += f" ({r['Card Member'].strip().title()})"
        ref = (r.get("Reference") or "").strip().strip("'")
        out.append({"date": datetime.strptime(r["Date"].strip(), "%m/%d/%Y").date(), "description": desc,
                    "amount": -amount, "fingerprint": ref or f"{r['Date']}|{desc}|{amount:.2f}"})
    df = pd.DataFrame(out, columns=["date", "description", "amount", "fingerprint"])
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
