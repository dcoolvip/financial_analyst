from datetime import date

import pytest

from finance.importers.statements import parse_pdf, parse_text

MORTGAGE = """Bank of America
Home Loans
Mortgage Statement
Statement Date 09/16/2026
Loan Number XXXXXXX4821
Amount Due
Explanation of Amount Due
Principal $612.40
Interest $2,237.60
Escrow (for Taxes and/or Insurance) $742.18
Regular Monthly Payment $3,592.18
Total Amount Due $3,592.18
Account Information
Outstanding Principal $429,387.55
Interest Rate (Until 10/2031) 6.250%
Prepayment Penalty No
"""

MORTGAGE_PI_LINE = """Rocket Mortgage
Statement date: September 16, 2026
Unpaid Principal Balance $301,220.10
Interest Rate 5.125%
Principal and Interest $1,834.55
Escrow Payment $610.00
"""

CHECKING = """Bank of America Advantage Plus Banking
Your checking account
for September 1, 2026 to September 30, 2026
Account number: 0000 1234 7788
Account summary
Beginning balance on September 1, 2026 $11,425.34
Deposits and other additions 8,341.69
Withdrawals and other subtractions -5,658.82
Ending balance on September 30, 2026 $14,108.21
"""

CARD = """Bank of America Customized Cash Rewards Visa Signature
Account Number: XXXX XXXX XXXX 3310
Statement Closing Date 09/22/2026
Previous Balance $1,210.44
Payments and Other Credits -$1,210.44
Purchases and Adjustments $1,840.22
New Balance Total $1,840.22
Minimum Payment Due $35.00
Credit Line $18,000
Interest Charge Calculation
Purchases 24.49% V $0.00
"""

MERRILL = """Merrill Edge Self-Directed
Account Number: 5WX-01234
Portfolio Summary
September 01, 2026 - September 30, 2026
Net Portfolio Value $75,383.65
Securities you own
"""


def test_mortgage_components_exclude_escrow():
    s = parse_text(MORTGAGE)
    assert (s.kind, s.account_type, s.institution) == ("loan", "mortgage", "Bank of America")
    assert s.balance == pytest.approx(429_387.55)
    assert s.rate == pytest.approx(0.0625)
    assert s.payment == pytest.approx(612.40 + 2_237.60)     # P&I only, escrow excluded
    assert s.as_of == date(2026, 9, 16) and s.last4 == "4821"


def test_mortgage_with_explicit_pi_line():
    s = parse_text(MORTGAGE_PI_LINE)
    assert s.balance == pytest.approx(301_220.10) and s.rate == pytest.approx(0.05125)
    assert s.payment == pytest.approx(1_834.55) and s.as_of == date(2026, 9, 16)


def test_mortgage_payment_falls_back_to_regular_minus_escrow():
    text = MORTGAGE.replace("Principal $612.40\n", "").replace("Interest $2,237.60\n", "")
    assert parse_text(text).payment == pytest.approx(3_592.18 - 742.18)


def test_checking_statement():
    s = parse_text(CHECKING)
    assert (s.kind, s.account_type) == ("deposit", "checking")
    assert s.balance == pytest.approx(14_108.21) and s.as_of == date(2026, 9, 30)
    assert not s.notes


def test_credit_card_statement():
    s = parse_text(CARD)
    assert s.kind == "credit_card"
    assert s.balance == pytest.approx(1_840.22) and s.rate == pytest.approx(0.2449)
    assert s.as_of == date(2026, 9, 22) and s.last4 == "3310"


def test_investment_statement():
    s = parse_text(MERRILL)
    assert (s.kind, s.institution) == ("investment", "Merrill")
    assert s.balance == pytest.approx(75_383.65)


def test_missing_values_are_reported_not_guessed():
    s = parse_text("Some Bank\nMortgage statement\nEscrow account\nPrincipal Balance $100,000.00\n")
    assert s.kind == "loan" and s.balance == 100_000.00
    assert s.rate is None and s.payment is None and any("rate" in n for n in s.notes)


def _pdf(lines: list[str]) -> bytes:
    """A minimal, valid single-page PDF with real text objects (what bank PDFs contain)."""
    esc = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines]
    stream = "BT /F1 10 Tf 14 TL 50 760 Td " + " ".join(f"({t}) Tj T*" for t in esc) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
            "/Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"]
    out, offsets = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


def test_real_pdf_end_to_end():
    s = parse_pdf(_pdf(MORTGAGE.strip().splitlines()))
    assert s.kind == "loan" and s.balance == pytest.approx(429_387.55)
    assert s.rate == pytest.approx(0.0625) and s.payment == pytest.approx(2_850.00)


def test_scanned_pdf_without_text():
    s = parse_pdf(_pdf([]))
    assert s.kind == "unknown" and "scan" in s.notes[0]


def test_apply_statement_updates_balance_terms_and_last4(tmp_path):
    from finance import db
    from finance.importers import apply_statement
    conn = db.connect(tmp_path / "t.db")
    mtg = db.add_account(conn, "BofA Mortgage", "Bank of America", "mortgage")
    older = parse_text(MORTGAGE.replace("09/16/2026", "08/16/2026").replace("6.250%", "6.000%"))
    newer = parse_text(MORTGAGE)
    apply_statement(conn, older, mtg, "aug.pdf", latest=False)
    apply_statement(conn, newer, mtg, "sep.pdf", latest=True)
    a = db.accounts(conn).iloc[0]
    assert a["balance"] == pytest.approx(429_387.55) and a["as_of"] == "2026-09-16"
    assert a["rate"] == pytest.approx(0.0625) and a["payment"] == pytest.approx(2_850.00)   # from newest only
    assert a["last4"] == "4821"
    assert len(db.balance_history(conn)) == 2                                              # both months kept


def test_card_statement_keeps_card_revolving(tmp_path):
    from finance import db, forecast
    from finance.importers import apply_statement
    conn = db.connect(tmp_path / "t.db")
    card = db.add_account(conn, "BofA Visa", "Bank of America", "credit_card")
    apply_statement(conn, parse_text(CARD), card, "card.pdf")
    loans, revolving = forecast.build_loans(db.accounts(conn))
    assert not loans and revolving == pytest.approx(1_840.22)   # APR saved, but no payment -> not amortized
