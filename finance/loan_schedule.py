"""Loans with no statement to pull (e.g. the Hyundai loan): their balance follows the payment schedule.

Assuming every payment is made on time: on each due day, a month of interest is added and the monthly payment taken
off. Runs for any loan whose notes say when payments are due ("payments due the 25th") and that has a rate and
payment. Each due day since the latest balance gets a value marked "Estimated (payment schedule)"; a real balance
(a statement, or one you type in) is never overwritten, and the estimate carries on from the newest one.
"""
from __future__ import annotations

import re
from datetime import date

from finance import db

LOAN_TYPES = ("auto_loan", "personal_loan", "student_loan", "other_liability", "mortgage", "heloc")
SOURCE = "Estimated (payment schedule)"
_DUE = re.compile(r"due (?:on )?the (\d{1,2})(?:st|nd|rd|th)?\b", re.I)


def due_day(notes) -> int | None:
    m = _DUE.search(notes or "")
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 28 else None


def due_dates(after: date, day: int, until: date):
    """Every due day strictly after `after`, up to and including `until`."""
    y, m = after.year, after.month
    while True:
        d = date(y, m, day)
        if d > until:
            return
        if d > after:
            yield d
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)


def schedule(balance: float, rate: float, payment: float, after: date, day: int, until: date) -> list[tuple[date, float]]:
    """(due day, balance after that payment) for each payment from `after` to `until`; stops once paid off."""
    out = []
    for d in due_dates(after, day, until):
        if balance <= 0:
            break
        balance = max(0.0, round(balance * (1 + rate / 12) - payment, 2))
        balance = 0.0 if balance < 5 else balance          # the last payment covers the few cents of rounding
        out.append((d, balance))
    return out


def roll_forward(conn, today: date | None = None) -> int:
    """Add the scheduled balances up to today for every loan that qualifies. Returns how many were added."""
    today = today or date.today()
    added = 0
    for r in db.accounts(conn).itertuples():
        day = due_day(r.notes)
        if r.type not in LOAN_TYPES or not r.active or not day or not r.payment or r.rate is None or r.rate != r.rate:
            continue
        last = conn.execute("SELECT date, balance FROM balances WHERE account_id = ? ORDER BY date DESC LIMIT 1",
                            (r.id,)).fetchone()
        if not last:
            continue
        for d, bal in schedule(float(last[1]), float(r.rate), float(r.payment), date.fromisoformat(last[0]), day, today):
            db.add_balance_if_missing(conn, int(r.id), d, bal, SOURCE)
            added += 1
    return added
