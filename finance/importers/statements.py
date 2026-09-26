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
    # Extras some statements carry (investment statements especially):
    history: list[tuple[date, float, bool]] = field(default_factory=list)   # (date, value, exact?)
    holdings: list[dict] = field(default_factory=list)   # symbol, description, quantity, price, value, cost_basis
    grants: list[dict] = field(default_factory=list)     # unvested RSUs: grant_date, grant_id, symbol, quantity, value

    @property
    def extras(self) -> str:
        parts = []
        if self.history:
            approx = sum(1 for *_, exact in self.history if not exact)
            span = f"back to {min(d for d, *_ in self.history):%b %Y}"
            parts.append(f"{len(self.history)} past values {span}" + (f" ({approx} approximate)" if approx else ""))
        if self.holdings:
            parts.append(f"{len(self.holdings)} positions")
        if self.grants:
            parts.append(f"{len(self.grants)} unvested grants")
        return ", ".join(parts)


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
    for name in ("E*TRADE", "Merrill", "Bank of America", "Chase", "Wells Fargo", "Citi", "Capital One", "American Express",
                 "Discover", "U.S. Bank", "Fidelity", "Schwab", "Vanguard", "Rocket Mortgage", "Mr. Cooper"):
        if name.lower() in text.lower():
            return name
    return ""


def _last4(text: str) -> str | None:
    """Last 4 digits of the account number, used only to match future statements to the same account."""
    m = re.search(r"ending in\s*(\d{4})\b", text, re.IGNORECASE)
    if m:
        return m.group(1)
    for pat in (r"(?:account|loan)\s*(?:number|no\.?|#)\s*:?\s*([\dX*•][\dA-Z*•\- ]{3,24}\d)",
                r"(?:primary account)\s*:?\s*([\dA-Z][\dA-Z\-]{3,20}\d)",
                r"\b(\d{3}-\d{6}-\d{3})\b",            # Morgan Stanley / E*TRADE style
                r"[X*•]{2,}[\s-]*(\d{4})\b"):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) >= 4:
                return digits[-4:]
    return None


def _kind(text: str) -> str:
    t = text.lower()
    if "escrow" in t or "principal balance" in t or ("loan" in t and "interest rate" in t and "amount due" in t):
        return "loan"
    if "new balance" in t and ("minimum payment" in t or "credit line" in t or "credit limit" in t):
        return "credit_card"
    if any(k in t for k in ("total account value", "net portfolio value", "portfolio summary", "ending total value",
                            "securities")):
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
    # Only the cover/summary area names the account type; disclosures mention "retirement accounts" generically
    s.account_type = "retirement" if re.search(r"\b(IRA|Roth|ROTH|401\s*\(?[kK]\)?|403\s*\(?[bB]\)?|Retirement Account)\b",
                                               t[:2500]) else "brokerage"
    s.balance = _money_after(t, [r"ending total value", r"total account value", r"net portfolio value",
                                 r"total portfolio value", r"total value", r"account value"])
    s.as_of = (_date_after(t, [r"ending total value"], window=40) or _period_end(t)
               or _date_after(t, [r"as of", r"statement period", r"period ending"]))
    if s.institution == "Merrill":
        _merrill_extras(s, t)
    elif s.institution == "E*TRADE" or "morgan stanley at work" in t.lower():
        _etrade_extras(s, t)
    # Exact values win: drop rounded chart points on dates we know exactly (incl. the statement date itself)
    exact = {d for d, _, e in s.history if e} | ({s.as_of} if s.as_of else set())
    s.history = sorted({d: (d, v, e) for d, v, e in s.history if e or d not in exact}.values())


# --- broker-specific extras: history, positions, unvested grants ---------------------

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _month_end(year: int, month: int) -> date:
    import calendar
    return date(year, month, calendar.monthrange(year, month)[1])


def _prev_month_end(d: date) -> date:
    return _month_end(d.year - (d.month == 1), 12 if d.month == 1 else d.month - 1)


def _period_end(t: str) -> date | None:
    m = re.search(rf"{_DATE_LONG}\s*-\s*{_DATE_LONG}", t, re.IGNORECASE)      # July 01, 2026 - July 31, 2026
    if m:
        return _parse_date(m.group(2))
    m = re.search(r"for the period\s+([A-Z][a-z]+)\s+\d{1,2}\s*-\s*(\d{1,2}),\s*(\d{4})", t, re.IGNORECASE)
    if m:                                                                     # For the Period July 1-31, 2026
        return _parse_date(f"{m.group(1)} {m.group(2)} {m.group(3)}")
    return None


def _num(x: str) -> float:
    return float(x.replace("$", "").replace(",", ""))


def _merrill_extras(s: Statement, t: str) -> None:
    # Exact prior month-end: "PORTFOLIO SUMMARY July 31 June 30 ... Net Portfolio Value $A $B $C"
    m = re.search(rf"PORTFOLIO SUMMARY.*?Net Portfolio Value\s+{MONEY}\s+{MONEY}", t, re.IGNORECASE | re.DOTALL)
    if m and s.as_of:
        s.history.append((_prev_month_end(s.as_of), _money(m.group(2)), True))
    # Long-range chart on page 1: "12/2112/22...1Q262Q26 7/26" then one value (in $ millions) per label
    m = re.search(r"^\s*((?:\d{1,2}/\d{2}|[1-4]Q\d{2})(?:\s*(?:\d{1,2}/\d{2}|[1-4]Q\d{2}))+)\s*$\n((?:\s*[\d.]+(?:\s+[\d.]+)*\s*\n)+)",
                  t, re.MULTILINE)
    if m and re.search(r"in millions", t, re.IGNORECASE):
        labels = re.findall(r"\d{1,2}/\d{2}|[1-4]Q\d{2}", m.group(1))
        values = [float(v) for v in m.group(2).split()]
        if len(labels) == len(values):
            for lab, v in zip(labels, values):
                if "Q" in lab:
                    q, yy = int(lab[0]), int(lab[2:])
                    d = _month_end(2000 + yy, q * 3)
                else:
                    mm, yy = (int(x) for x in lab.split("/"))
                    d = _month_end(2000 + yy, mm)
                s.history.append((d, v * 1_000_000, False))
    # Positions: "APPLE INC AAPL 7,508.0000 219,080.28 308.9100 2,319,296.28 2,100,216.00 8,119"
    for m in re.finditer(r"(?m)^(?P<desc>[A-Z][A-Z0-9&.,'/ -]+?)\s+(?P<sym>[A-Z][A-Z.]{0,5})\s+(?P<qty>[\d,]+\.\d+)\s+"
                         r"(?P<cost>[\d,]+\.\d{2})\s+(?P<price>[\d,]+\.\d+)\s+(?P<value>[\d,]+\.\d{2})\b", t):
        s.holdings.append({"symbol": m["sym"], "description": m["desc"].strip(), "quantity": _num(m["qty"]),
                           "price": _num(m["price"]), "value": _num(m["value"]), "cost_basis": _num(m["cost"])})
    m = re.search(r"Cash/Money Accounts\s+([\d,]+\.\d{2})", t)
    if s.holdings and m and _num(m.group(1)) > 0:
        s.holdings.append({"symbol": "CASH", "description": "Cash / money accounts", "quantity": None,
                           "price": None, "value": _num(m.group(1)), "cost_basis": None})


def _etrade_extras(s: Statement, t: str) -> None:
    anchors: dict[date, float] = {}
    # "Beginning Total Value (as of 7/1/26) $X" = prior month-end
    m = re.search(rf"Beginning Total Value\s*\(as of\s*{_DATE_NUM}\)\s*{MONEY}", t, re.IGNORECASE)
    if m and s.as_of:
        anchors[_prev_month_end(s.as_of)] = _money(m.group(2))
    # "TOTAL BEGINNING VALUE $this-period $this-year" with "This Year (1/1/26-..." = prior Dec 31
    m = re.search(rf"TOTAL BEGINNING VALUE\s+{MONEY}\s+{MONEY}", t, re.IGNORECASE)
    y = re.search(r"This Year\s*\(\s*1/1/(\d{2,4})", t, re.IGNORECASE)
    if m and y:
        yr = int(y.group(1)) % 100 + 2000
        anchors[date(yr - 1, 12, 31)] = _money(m.group(2))
    # 13-month chart: month labels, then "% change from prior month" per month (chronological)
    sec = re.search(r"MARKET VALUE OVER TIME(.*?)The percentages above", t, re.IGNORECASE | re.DOTALL)
    if sec and s.as_of and s.balance:
        body = sec.group(1)
        labels = re.search(r"\b((?:%s)(?:\s+(?:%s))+)\b" % ("|".join(_MONTHS), "|".join(_MONTHS)), body)
        tail = body[body.lower().find("millions"):] if "millions" in body.lower() else body
        pcts = [float(p) / 100 for p in re.findall(r"(-?\d+(?:\.\d+)?)%", tail)]
        if labels and len(labels.group(1).split()) == len(pcts) >= 2:
            n = len(pcts)
            dates, d = [], date(s.as_of.year, s.as_of.month, 1)
            for _ in range(n):
                dates.insert(0, _month_end(d.year, d.month))
                d = date(d.year - (d.month == 1), 12 if d.month == 1 else d.month - 1, 1)
            vals = [0.0] * n
            vals[-1] = s.balance
            for i in range(n - 1, 0, -1):
                vals[i - 1] = anchors.get(dates[i - 1]) or vals[i] / (1 + pcts[i])
            for d_, v in zip(dates[:-1], vals[:-1]):
                s.history.append((d_, round(v, 2), d_ in anchors))
    for d_, v in anchors.items():
        if not any(h[0] == d_ for h in s.history):
            s.history.append((d_, v, True))
    s.history.sort()
    # Positions: "APPLE INC (AAPL) 8,948.840 $308.910 $984,170.64 $2,764,386.16 $1,780,215.52 ..."
    for m in re.finditer(r"(?m)^(?P<desc>[A-Z][A-Z0-9&.,' -]+?)\s*\((?P<sym>[A-Z.]{1,6})\)\s+(?P<qty>[\d,]+\.\d+)\s+"
                         r"\$?(?P<price>[\d,]+\.\d+)\s+\$?(?P<cost>[\d,]+\.\d{2})\s+\$?(?P<value>[\d,]+\.\d{2})", t):
        s.holdings.append({"symbol": m["sym"], "description": m["desc"].strip(), "quantity": _num(m["qty"]),
                           "price": _num(m["price"]), "value": _num(m["value"]), "cost_basis": _num(m["cost"])})
    m = re.search(r"CASH, BDP, AND MMFs\s+[\d.]+%\s+\$?([\d,]+\.\d{2})", t)
    if s.holdings and m:
        s.holdings.append({"symbol": "CASH", "description": "Cash / bank deposit program", "quantity": None,
                           "price": None, "value": _num(m.group(1)), "cost_basis": None})
    # Unvested RSUs: "07/15/24 903789 RSU AAPL 533.000 $0.00 $308.91 $165,747.01"
    for m in re.finditer(r"(?m)^(?P<gd>\d{2}/\d{2}/\d{2,4})\s+(?P<gid>\w+)\s+(?P<type>RSU|PSU|RS)\s+(?P<sym>[A-Z.]{1,6})\s+"
                         r"(?P<qty>[\d,]+\.\d+)\s+\$?[\d,.]+\s+\$?[\d,.]+\s+\$?(?P<value>[\d,]+\.\d{2})", t):
        s.grants.append({"grant_date": _parse_date(m["gd"]), "grant_id": m["gid"], "type": m["type"],
                         "symbol": m["sym"], "quantity": _num(m["qty"]), "value": _num(m["value"])})


def parse_text(text: str) -> Statement:
    s = Statement(kind=_kind(text), institution=_institution(text), last4=_last4(text), text=text)
    {"loan": _parse_loan, "deposit": _parse_deposit, "credit_card": _parse_card,
     "investment": _parse_investment}.get(s.kind, lambda *_: None)(s, text)
    if s.kind == "unknown":
        s.notes.append("Couldn't tell what kind of statement this is - pick the account and fill in the values")
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
