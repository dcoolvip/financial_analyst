"""Loans without statements follow their payment schedule (assuming on-time payments)."""
from datetime import date

from finance import db, loan_schedule


def test_due_day_comes_from_the_notes():
    assert loan_schedule.due_day("48 months, payments due the 25th, last one Sep 25, 2027") == 25
    assert loan_schedule.due_day("due on the 1st") == 1
    assert loan_schedule.due_day("no schedule here") is None


def test_hyundai_like_loan_pays_off_on_its_last_due_day():
    rows = loan_schedule.schedule(15_801, 0.0099, 1_323.78, date(2026, 9, 26), 25, date(2028, 1, 1))
    assert rows[0] == (date(2026, 10, 25), 14_490.26)
    assert rows[-1] == (date(2027, 9, 25), 0.0)                     # paid off with the Sep 25, 2027 payment
    assert len(rows) == 12


def test_roll_forward_adds_due_days_up_to_today_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    loan = db.get_or_create_account(conn, "Car loan", "Lender", "auto_loan")
    conn.execute("UPDATE accounts SET rate = 0.0099, payment = 1323.78, notes = 'payments due the 25th' WHERE id = ?",
                 (loan,))
    other = db.get_or_create_account(conn, "Mortgage", "Bank", "mortgage")          # no due day in notes: left alone
    conn.execute("UPDATE accounts SET rate = 0.06, payment = 3000 WHERE id = ?", (other,))
    db.add_balance_if_missing(conn, loan, "2026-09-26", 15_801, "manual")
    db.add_balance_if_missing(conn, other, "2026-08-03", 400_000, "statement")
    assert loan_schedule.roll_forward(conn, date(2026, 10, 24)) == 0               # Oct 25 not here yet
    assert loan_schedule.roll_forward(conn, date(2026, 11, 30)) == 2
    got = conn.execute("SELECT date, balance, source FROM balances WHERE account_id = ? ORDER BY date", (loan,)).fetchall()
    assert got[1:] == [("2026-10-25", 14_490.26, loan_schedule.SOURCE), ("2026-11-25", 13_178.43, loan_schedule.SOURCE)]
    assert loan_schedule.roll_forward(conn, date(2026, 11, 30)) == 0               # nothing twice
    assert conn.execute("SELECT COUNT(*) FROM balances WHERE account_id = ?", (other,)).fetchone()[0] == 1
