"""Read key numbers from PDF statements - locally, nothing leaves the Mac.

Statements carry names, addresses and full account numbers, so unlike merchant names they are never
sent to an AI. Instead the text is extracted with pypdf and the few numbers we need are found by
their labels. Labels are fairly standard across US banks, so this works beyond Bank of America;
anything not found is left blank for you to fill in on the review screen.

    loan         principal balance, interest rate, principal+interest payment (escrow excluded)
    deposit      ending balance on the statement date (checking / savings)
    credit_card  new balance, purchase APR
    investment   total account value (Merrill etc.)
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

MONEY = r"\$?\s*(-?[\d,]+\.\d{2})"
_DATE_NUM = r"(\d{1,2}/\d{1,2}/\d{2,4})"
_DATE_LONG = r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})"
WINDOW = 160   # how far after a label to look for its value

KIND_ACCOUNT_TYPE = {"deposit": "checking", "credit_card": "credit_card", "investment": "brokerage"}


@dataclass
class Statement:
    kind: str                       # loan | deposit | credit_card | investment | unknown
    institution: str = ""
    as_of: date | None = None
    balance: float | None = None    # principal owed / ending balance / new balance / account value
    rate: float | None = None       # annual, as a fraction (0.0625)
    payment: float | None = None    # loans: monthly principal + interest
    last4: str | None = None
    account_type: str | None = None  # suggested dashboard account type
    notes: list[str] = field(default_factory=list)
    text: str = ""                  # what was read (shown on request; never stored)


# --- text extraction -------------------------------------------------------------

def extract_text(pdf: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise ValueError("This PDF is password-protected. Save an unprotected copy and try again.")
    return "\n".join((p.extract_text() or "") for p in reader.pages)


# --- small parsing helpers -----------------------------------------------------------

def _money(s: str) -> float:
    return float(s.replace(",", "").replace("$", "").strip())


def _parse_date(s: str) -> date | None:
    s = s.replace(".", "").replace(",", "")
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(" ".join(s.split()), fmt).date()
        except ValueError:
            continue
    return None


def _after(text: str, labels: list[str], pattern: str, window: int = WINDOW) -> str | None:
    """First `pattern` match within `window` chars after the first label found (labels in priority order)."""
    for label in labels:
        for m in re.finditer(label, text, re.IGNORECASE):
            hit = re.search(pattern, text[m.end():m.end() + window])
            if hit:
                return hit.group(1)
    return None


def _money_after(text, labels, window=WINDOW) -> float | None:
    v = _after(text, labels, MONEY, window)
    return _money(v) if v is not None else None


def _date_after(text, labels, window=WINDOW) -> date | None:
    for label in labels:
        for m in re.finditer(label, text, re.IGNORECASE):
            chunk = text[m.end():m.end() + window]
            hits = [h for h in (re.search(_DATE_NUM, chunk), re.search(_DATE_LONG, chunk, re.IGNORECASE)) if h]
            if hits:
                d = _parse_date(min(hits, key=lambda h: h.start()).group(1))
                if d:
                    return d
    return None


def _percent_after(text, labels, window=WINDOW) -> float | None:
    v = _after(text, labels, r"(\d{1,2}(?:\.\d{1,4})?)\s*%", window)
    return float(v) / 100 if v is not None else None


def _line_money(text: str, label: str) -> float | None:
    """A money value on a line that *starts* with the label (e.g. 'Principal  $512.34')."""
    m = re.search(rf"(?im)^\s*{label}\s*[:.]?\s*{MONEY}\s*$", text)
    return _money(m.group(1)) if m else None


def _institution(text: str) -> str:
    for name in ("Merrill", "Bank of America", "Chase", "Wells Fargo", "Citi", "Capital One", "American Express",
                 "Discover", "U.S. Bank", "Fidelity", "Schwab", "Vanguard", "Rocket Mortgage", "Mr. Cooper"):
        if name.lower() in text.lower():
            return name
    return ""


def _last4(text: str) -> str | None:
    for pat in (r"ending in\s*(\d{4})\b", r"[X*•]{2,}[\s-]*(\d{4})\b",
                r"(?:account|loan)\s*(?:number|no\.?|#)\s*[:\s]*(?:[\dX*•]{4}[\s-]*){1,4}?(\d{4})\b"):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _kind(text: str) -> str:
    t = text.lower()
    if "escrow" in t or "principal balance" in t or ("loan" in t and "interest rate" in t and "amount due" in t):
        return "loan"
    if "new balance" in t and ("minimum payment" in t or "credit line" in t or "credit limit" in t):
        return "credit_card"
    if any(k in t for k in ("total account value", "net portfolio value", "portfolio summary", "securities")):
        return "investment"
    if "ending balance" in t or "beginning balance" in t:
        return "deposit"
    return "unknown"


# --- parsers per statement kind ----------------------------------------------------

def _parse_loan(s: Statement, t: str) -> None:
    low = t.lower()
    s.account_type = ("heloc" if ("home equity" in low or "heloc" in low)
                      else "auto_loan" if ("auto" in low or "vehicle" in low)
                      else "mortgage" if ("mortgage" in low or "escrow" in low)
                      else "personal_loan")
    s.balance = _money_after(t, [r"unpaid principal balance", r"outstanding principal(?: balance)?",
                                 r"current principal balance", r"principal balance"])
    s.rate = _percent_after(t, [r"interest rate", r"annual percentage rate", r"\bAPR\b"])
    pi = _money_after(t, [r"principal (?:and|&) interest(?: payment)?", r"\bP\s*&\s*I\b"], window=60)
    if pi is None:
        principal, interest = _line_money(t, "Principal"), _line_money(t, "Interest")
        if principal is not None and interest is not None:
            pi = principal + interest
    if pi is None:
        regular = _money_after(t, [r"regular monthly payment", r"monthly payment amount", r"regular payment"])
        escrow = _money_after(t, [r"escrow(?: \(for taxes and/or insurance\))?(?: payment)?"], window=60)
        if regular is not None:
            pi = regular - (escrow or 0)
            if escrow:
                s.notes.append("Payment = regular payment minus escrow")
    s.payment = pi
    s.as_of = _date_after(t, [r"statement date", r"as of", r"statement period"])


def _parse_deposit(s: Statement, t: str) -> None:
    s.account_type = "savings" if re.search(r"\bsavings\b", t, re.IGNORECASE) and not re.search(
        r"\bchecking\b", t, re.IGNORECASE) else "checking"
    m = re.search(rf"ending balance(?: on| as of)?\s+(?:{_DATE_LONG}|{_DATE_NUM})?[^\d$\n]{{0,20}}{MONEY}", t,
                  re.IGNORECASE)
    if m:
        s.balance = _money(m.group(3))
        s.as_of = _parse_date(m.group(1) or m.group(2) or "")
    else:
        s.balance = _money_after(t, [r"ending balance", r"closing balance"])
    if s.as_of is None:
        m = re.search(rf"(?:to|through|-)\s*{_DATE_LONG}", t, re.IGNORECASE) or \
            re.search(rf"(?:to|through|-)\s*{_DATE_NUM}", t, re.IGNORECASE)
        s.as_of = _parse_date(m.group(1)) if m else _date_after(t, [r"statement date", r"statement period"])


def _parse_card(s: Statement, t: str) -> None:
    s.account_type = "credit_card"
    s.balance = _money_after(t, [r"new balance total", r"new balance"], window=60)
    s.rate = _percent_after(t, [r"purchases?\s+(?:apr\s+)?", r"annual percentage rate"], window=80)
    s.as_of = _date_after(t, [r"statement closing date", r"closing date", r"statement date", r"billing period"])


def _parse_investment(s: Statement, t: str) -> None:
    s.account_type = "retirement" if re.search(r"\b(ira|401\s*\(?k\)?|roth|retirement)\b", t, re.IGNORECASE) \
        else "brokerage"
    s.balance = _money_after(t, [r"total account value", r"net portfolio value", r"total portfolio value",
                                 r"total value", r"account value"])
    s.as_of = _date_after(t, [r"as of", r"statement period", r"period ending"])


def parse_text(text: str) -> Statement:
    s = Statement(kind=_kind(text), institution=_institution(text), last4=_last4(text), text=text)
    {"loan": _parse_loan, "deposit": _parse_deposit, "credit_card": _parse_card,
     "investment": _parse_investment}.get(s.kind, lambda *_: None)(s, text)
    if s.kind == "unknown":
        s.notes.append("Couldn't tell what kind of statement this is - fill in the values below")
    missing = [n for n, v in (("date", s.as_of), ("balance", s.balance)) if v is None]
    if s.kind == "loan":
        missing += [n for n, v in (("rate", s.rate), ("payment", s.payment)) if v is None]
    if missing and s.kind != "unknown":
        s.notes.append("Not found: " + ", ".join(missing))
    return s


def parse_pdf(pdf: bytes) -> Statement:
    text = extract_text(pdf)
    if len(text.strip()) < 40:
        return Statement(kind="unknown", notes=["No text in this PDF (it may be a scan) - enter the values below"])
    return parse_text(text)
