"""Synthetic 18-month household, written as real BofA-format CSVs and imported
through the normal importers - so the demo exercises the same code path your
files will. Lives in its own DB (demo.db); never touches your real data.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import pandas as pd

from . import db
from .importers import apply, parse_file

BOFA = "Bank of America"
MONTHS = 18


def _month_starts(n: int) -> list[date]:
    first = date.today().replace(day=1)
    return [(pd.Timestamp(first) - pd.DateOffset(months=i)).date() for i in range(n - 1, -1, -1)]


def _q(v: float) -> str:
    return f'"{v:,.2f}"'


def _deposit_csv(rows: list[tuple[date, str, float]], opening: float) -> str:
    rows = sorted(rows, key=lambda r: r[0])
    start, end = rows[0][0], rows[-1][0]
    running, body = opening, [f"{start:%m/%d/%Y},Beginning balance as of {start:%m/%d/%Y},,{_q(opening)}"]
    credits = debits = 0.0
    for d, desc, amt in rows:
        running += amt
        credits += max(amt, 0)
        debits += min(amt, 0)
        body.append(f'{d:%m/%d/%Y},"{desc}",{_q(amt)},{_q(running)}')
    head = [
        "Description,,Summary Amt.",
        f"Beginning balance as of {start:%m/%d/%Y},,{_q(opening)}",
        f"Total credits,,{_q(credits)}",
        f"Total debits,,{_q(debits)}",
        f"Ending balance as of {end:%m/%d/%Y},,{_q(running)}",
        "",
        "Date,Description,Amount,Running Bal.",
    ]
    return "\n".join(head + body) + "\n"


def seed(conn) -> None:
    rnd = random.Random(42)
    months = _month_starts(MONTHS)
    today = date.today()
    chk, sav, cc = [], [], []

    for m in months:
        d = lambda day: m.replace(day=day)  # noqa: E731
        chk += [
            (d(1), "ACME CORP DES:PAYROLL ID:XXXXX", 4150.00),
            (d(15), "ACME CORP DES:PAYROLL ID:XXXXX", 4150.00),
            (d(3), "BANK OF AMERICA MORTGAGE DES:PAYMENT", -2850.00),
            (d(5), "BANK OF AMERICA AUTO LOAN DES:PAYMENT", -485.00),
            (d(8), "PGE DES:WEB ONLINE", -round(rnd.uniform(120, 260), 2)),
            (d(9), "COMCAST DES:BILL PAY", -89.99),
            (d(16), "Online Banking transfer to SAV 4521", -1000.00),
            (d(18), "MERRILL DES:TRANSFER", -1000.00),
        ]
        sav += [(d(16), "Online Banking transfer from CHK 7788", 1000.00),
                (d(28), "Interest Earned", round(rnd.uniform(35, 60), 2))]
        spend = 0.0
        for _ in range(rnd.randint(22, 30)):
            payee = rnd.choice(["WHOLE FOODS MKT", "TRADER JOE'S", "AMAZON MKTPLACE PMTS", "SHELL OIL",
                                "DOORDASH", "STARBUCKS", "TARGET", "NETFLIX.COM", "CVS/PHARMACY", "CHEVRON"])
            amt = round(rnd.uniform(8, 180), 2)
            spend += amt
            cc.append((d(rnd.randint(1, 28)), payee, -amt))
        cc.append((d(22), "PAYMENT - THANK YOU", round(spend, 2)))
        chk.append((d(22), "BANK OF AMERICA CREDIT CARD Bill Payment", -round(spend, 2)))

    chk = [r for r in chk if r[0] <= today]
    sav = [r for r in sav if r[0] <= today]
    cc = [r for r in cc if r[0] <= today]

    files = {
        ("BofA Checking", "checking"): _deposit_csv(chk, 8200.00),
        ("BofA Savings", "savings"): _deposit_csv(sav, 15000.00),
        ("BofA Visa Signature", "credit_card"): "Posted Date,Reference Number,Payee,Address,Amount\n" + "\n".join(
            f'{d:%m/%d/%Y},{i:023d},"{p}","",{a:.2f}' for i, (d, p, a) in enumerate(sorted(cc, reverse=True))
        ) + "\n",
    }
    for (name, kind), text in files.items():
        acct = db.get_or_create_account(conn, name, BOFA, kind)
        apply(conn, parse_file(text), acct, f"demo_{kind}.csv")
    card = db.get_or_create_account(conn, "BofA Visa Signature", BOFA, "credit_card")
    db.upsert_balance(conn, card, today, 1840.22)

    # Investments: monthly statement balances, then today's holdings file
    brk = db.get_or_create_account(conn, "Merrill Edge Brokerage", BOFA, "brokerage")
    k401 = db.get_or_create_account(conn, "Merrill 401(k)", BOFA, "retirement")
    b, k = 58_000.0, 94_000.0
    for m in months:
        b = b * (1 + rnd.gauss(0.006, 0.035)) + 1000
        k = k * (1 + rnd.gauss(0.006, 0.035)) + 1650
        db.upsert_balance(conn, brk, m, b, "demo")
        db.upsert_balance(conn, k401, m, k, "demo")
    db.upsert_balance(conn, k401, today, k, "demo")
    weights = [("VTI", "VANGUARD TOTAL STOCK MARKET ETF", 0.55, 312.40), ("VXUS", "VANGUARD TOTAL INTL STOCK ETF", 0.2, 68.15),
               ("BND", "VANGUARD TOTAL BOND MARKET ETF", 0.15, 73.02), ("AAPL", "APPLE INC", 0.08, 246.10)]
    lines = [f'"Holdings as of {today:%m/%d/%Y}"', "",
             '"Symbol","Description","Quantity","Price ($)","Value ($)"']
    for sym, desc, w, px in weights:
        qty = round(b * w / px, 3)
        lines.append(f'"{sym}","{desc}","{qty}","{px:.2f}","{qty * px:,.2f}"')
    lines.append(f'"","MERRILL LYNCH BANK DEPOSIT PROGRAM","","","{b * 0.02:,.2f}"')
    apply(conn, parse_file("\n".join(lines) + "\n"), brk, "demo_holdings.csv")

    # Loans: amortize real balances; terms are known so the forecast uses them
    for name, kind, bal, apr, pay in [("BofA Mortgage", "mortgage", 431_000.0, 0.0625, 2850.0),
                                      ("BofA Auto Loan", "auto_loan", 23_500.0, 0.069, 485.0)]:
        acct = db.get_or_create_account(conn, name, BOFA, kind)
        db.update_account_terms(conn, acct, apr, pay)
        for m in months:
            bal = bal * (1 + apr / 12) - pay
            db.upsert_balance(conn, acct, m + timedelta(days=4), bal, "demo")

    # Things you own that no bank reports - updated by hand now and then
    home = db.get_or_create_account(conn, "Home (estimate)", "Manual", "property")
    car = db.get_or_create_account(conn, "Car (KBB estimate)", "Manual", "vehicle")
    for i, m in enumerate(months[::6]):
        db.upsert_balance(conn, home, m, 640_000 * 1.035 ** (i / 2), "demo")
        db.upsert_balance(conn, car, m, 31_000 * 0.85 ** (i / 2), "demo")
