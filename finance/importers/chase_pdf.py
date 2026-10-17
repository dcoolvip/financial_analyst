"""Chase statements: their transaction pages use fonts the Python PDF reader can't decode, so they're read with
macOS's own PDF engine (PDFKit, via a small Swift program compiled once into the data folder). All on this Mac.

Checking / savings: every transaction line carries the running balance, so each row is proven exactly
(previous balance + amount = printed balance) and the last must equal the Ending Balance - else nothing is
imported. Cards: payments print negative and purchases positive (no reliable section headings in the text), and
the sums must equal the Account Summary's Payment/Credits and Purchases + fees + interest.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path

from .. import paths

SWIFT_SOURCE = Path(__file__).with_name("pdfkit_text.swift")
MONEY = re.compile(r"^-?\$?[\d,]+\.\d{2}$")
_DATE_MD = re.compile(r"^(\d{2})/(\d{2})\b")


def _tool() -> list[str]:
    """The compiled helper (rebuilt when the source changes), else run the source with the Swift interpreter."""
    src = SWIFT_SOURCE.read_bytes()
    out = paths.data_dir() / "bin" / f"pdfkit_text-{hashlib.sha1(src).hexdigest()[:8]}"
    if not out.exists() and shutil.which("swiftc"):
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", "-o", str(out), str(SWIFT_SOURCE)], capture_output=True)
    return [str(out)] if out.exists() else ["swift", str(SWIFT_SOURCE)]


def pdfkit(pdf: bytes, lines: bool = False) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf)
        f.flush()
        r = subprocess.run(_tool() + (["--lines"] if lines else []) + [f.name], capture_output=True, text=True,
                           timeout=120)
    return r.stdout


def _money(tok: str) -> float:
    return float(tok.replace("$", "").replace(",", ""))


def _year_for(month: int, end: date) -> int:
    return end.year - (1 if month > end.month else 0)


def _rows(pdf: bytes) -> list[str]:
    """Text lines in reading order, with pieces on the same line (same page and height) joined left to right."""
    pieces = []
    for ln in pdfkit(pdf, lines=True).splitlines():
        parts = ln.split("\t", 3)
        if len(parts) == 4:
            pieces.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    rows, key = [], None
    for page, y, x, text in sorted(pieces):
        k = (page, y)
        if k != key:
            rows.append([])
            key = k
        rows[-1].append(text)
    return [" ".join(" ".join(r).split()) for r in rows]


def _amounts(text: str) -> list[float]:
    text = re.sub(r"(?<!\S)-\s+(?=\$?[\d,]+\.\d{2}\b)", "-", text)          # "- 12,345.67" -> "-12,345.67"
    return [_money(t) for t in text.split() if MONEY.match(t)]


def deposit_transactions(pdf: bytes, end: date) -> tuple[list[dict], list[str]]:
    rows = _rows(pdf)
    items, problems, bal = [], [], None
    try:
        i = next(n for n, r in enumerate(rows) if r.upper().startswith("TRANSACTION DETAIL"))
    except StopIteration:                          # no activity this period: fine only if the balance didn't move
        begin = next((_amounts(r)[-1] for r in rows if r.lower().startswith("beginning balance") and _amounts(r)), None)
        final = next((_amounts(r)[-1] for r in rows if r.lower().startswith("ending balance") and _amounts(r)), None)
        if begin is not None and final is not None and abs(begin - final) < 0.005:
            return [], []
        return [], ["no transaction list, but the balance changed"]
    for r in rows[i + 1:]:
        low = r.lower()
        if low.startswith("beginning balance"):
            bal = _amounts(r)[-1]
            continue
        if low.startswith("ending balance"):
            printed = _amounts(r)[-1]
            if bal is None or abs(printed - bal) > 0.005:
                problems.append(f"the transactions end at ${bal or 0:,.2f} but the Ending Balance is ${printed:,.2f}")
            break
        m = _DATE_MD.match(r)
        if not m or bal is None:
            continue
        nums = _amounts(r)
        pair = next(((a, b) for a, b in ((x, y) for x in nums for y in nums if x is not y)
                     if abs(bal + a - b) < 0.005), None)
        if pair is None:
            problems.append(f"a line dated {m.group(1)}/{m.group(2)} doesn't add up with the running balance")
            continue
        amount, bal = pair
        desc = re.sub(r"\s+", " ", " ".join(t for t in r[5:].split() if not MONEY.match(t) and t != "-")).strip()
        items.append({"date": date(_year_for(int(m.group(1)), end), int(m.group(1)), int(m.group(2))),
                      "description": desc, "amount": round(amount, 2)})
    else:
        problems.append("no Ending Balance found after the transactions")
    return items, problems


def card_transactions(text: str, end: date) -> tuple[list[dict], list[str]]:
    lines = [" ".join(l.split()) for l in text.splitlines()]
    # Only the "ACCOUNT ACTIVITY" list. (Prime Visa also has a points table - "PURCHASES AND REDEMPTIONS" with a
    # Rewards column - listing orders paid with points; those aren't card transactions. No list = no activity, which
    # the summary's totals then have to confirm.)
    starts = [n for n, l in enumerate(lines) if l.upper().startswith("ACCOUNT ACTIVITY")]
    items = []
    for l in lines[starts[0]:] if starts else []:
        if re.search(r"(?i)totals year-to-date", l):
            break
        m = _DATE_MD.match(l)
        toks = l.split()
        money = [n for n, t in enumerate(toks) if MONEY.match(t)]
        if m and money and money[-1] >= 2:
            k = money[-1]                                    # the last dollar figure
            items.append({"date": date(_year_for(int(m.group(1)), end), int(m.group(1)), int(m.group(2))),
                          "description": " ".join(toks[1:k]), "amount": round(-_money(toks[k]), 2)})   # spending: out
    problems = []

    def summary(label):
        m = re.search(rf"(?im)^{label}\s+([+-]?)\$([\d,]+\.\d{{2}})", text)
        return (-1 if m.group(1) == "-" else 1) * _money(m.group(2)) if m else None
    credits, purchases = summary(r"Payment,\s*Credits"), summary("Purchases")
    extras = sum(v or 0 for v in (summary("Cash Advances"), summary("Balance Transfers"), summary("Fees Charged"),
                                  summary("Interest Charged")))
    got_in = sum(t["amount"] for t in items if t["amount"] > 0)
    got_out = -sum(t["amount"] for t in items if t["amount"] < 0)
    if credits is None or purchases is None:
        problems.append("couldn't find the Account Summary totals to check against")
    else:
        if abs(got_in - abs(credits)) > 0.005:
            problems.append(f"payments and credits: read ${got_in:,.2f} but the summary says ${abs(credits):,.2f}")
        if abs(got_out - (purchases + extras)) > 0.005:
            problems.append(f"purchases, fees and interest: read ${got_out:,.2f} but the summary says "
                            f"${purchases + extras:,.2f}")
    return items, problems


def enrich(pdf: bytes, s) -> None:
    """Fill in a Chase statement (kind, dates, balance, card digits, transactions) from macOS's PDF reader."""
    text = pdfkit(pdf)
    if not text.strip():
        s.notes.append("Couldn't read this Chase statement with macOS's PDF reader")
        return
    if (m := re.search(r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})", text)):   # a card
        start, end = (datetime.strptime(x, "%m/%d/%y").date() for x in m.groups())
        s.kind, s.account_type, s.as_of, s.period = "credit_card", "credit_card", end, (start, end)
        if (b := re.search(r"(?m)^New Balance\s+\$?(-?[\d,]+\.\d{2})", text)):
            s.balance = _money(b.group(1))
        if (a := re.search(r"Account Number:\s*X{4} X{4} X{4} (\d{4})", text)):
            s.last4 = a.group(1)
        s.notes = [n for n in s.notes if not n.startswith("Not found")]
        s.transactions, problems = card_transactions(text, end)
    elif (m := re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})\s+through\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text)):
        start, end = (datetime.strptime(x, "%B %d, %Y").date() for x in m.groups())
        s.kind, s.as_of, s.period = "deposit", end, (start, end)
        s.account_type = "savings" if "SAVINGS SUMMARY" in text else "checking"
        if (b := re.search(r"Ending Balance\s+\$?(-?[\d,]+\.\d{2})", text)):
            s.balance = _money(b.group(1))
        s.last4 = None                    # not printed as text on these; the pull takes it from the file name
        s.transactions, problems = deposit_transactions(pdf, end)
    else:
        return
    s.totals_ok = not problems                   # a statement with nothing on it is complete too
    if problems:
        s.notes.append("Transactions not imported - " + "; ".join(problems[:3]))


def last4_from_filename(name: str) -> str | None:
    """Chase names statements after the account: 20260916-statements-2738-.pdf"""
    m = re.search(r"statements-(\d{4})-", name)
    return m.group(1) if m else None
