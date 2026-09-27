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


# --- broker layouts (synthetic: same structure as real statements, made-up people and numbers) ---

MERRILL_CMA = """+
Primary Account: 11X-45678
12/2212/2312/241Q252Q25 7/25
0.500
0.750 0.800
0.910 1.02
1.10
YOUR MERRILL EDGE REPORT July 01, 2025 -July 31, 2025
PORTFOLIO SUMMARY July 31 June 30 Month Change
Net Portfolio Value        $1,100,000.00        $1,050,000.00          $50,000.00
 Total Value (Net Portfolio Value plus Assets Not Held/Valued By MLPF&S, if any) in millions, 2021-2025
JANE SAMPLE
ASSETS July 31 June 30
Cash/Money Accounts               12.50               10.00
Equities       1,099,987.50       1,049,990.00
EQUITIES
Description Symbol Quantity
APPLE INC AAPL 4,000.0000 150,000.00 200.0000 800,000.00 650,000.00 4,000
VANGUARD TOTAL STOCK MARKET ETF VTI 1,000.0000 250,000.00 299.9875 299,987.50 49,987.50 1,500
Merrill Lynch, Pierce, Fenner & Smith Incorporated
"""

ETRADE = """Beginning Total Value (as of 7/1/25) $1,000,000.00
Ending Total Value (as of 7/31/25) $1,100,000.00
CLIENT STATEMENT     For the Period July 1-31, 2025
E*TRADE is a business of Morgan Stanley.
Important Information if You are a Margin Customer (not available for certain retirement accounts)
Morgan Stanley at Work Self-Directed Account
123-456789-012
MARKET VALUE OVER TIME
JAN FEB MAR APR MAY JUN JUL
($)  Millions
5.0%
10.0%
-10.0% 20.0%
-5.0%
5.0%
10.0%
The percentages above represent the change in dollar value from the prior period.
This Year
(1/1/25-7/31/25)
TOTAL BEGINNING VALUE $1,000,000.00 $800,000.00
APPLE INC (AAPL) 5,000.000 $200.000 $400,000.00 $1,000,000.00 $600,000.00 $5,000.00 0.50
CASH, BDP, AND MMFs 9.09% $100,000.00 $10.00
STOCK PLAN DETAILS
07/15/24 111111 RSU AAPL 100.000 $0.00 $200.00 $20,000.00
09/28/24 222222 RSU AAPL 50.000 0.00 200.00 10,000.00
"""


def test_merrill_history_positions_and_cost_basis():
    s = parse_text(MERRILL_CMA)
    assert (s.kind, s.institution, s.account_type, s.last4) == ("investment", "Merrill", "brokerage", "5678")
    assert s.as_of == date(2025, 7, 31) and s.balance == pytest.approx(1_100_000)
    hist = {d: (v, exact) for d, v, exact in s.history}
    assert hist[date(2025, 6, 30)] == (pytest.approx(1_050_000), True)          # exact beats the chart's 1.02M
    assert hist[date(2022, 12, 31)] == (pytest.approx(500_000), False)          # chart, in millions
    assert hist[date(2025, 3, 31)] == (pytest.approx(910_000), False)           # "1Q25" -> quarter end
    assert date(2025, 7, 31) not in hist                                        # the statement date itself is exact
    aapl = next(h for h in s.holdings if h["symbol"] == "AAPL")
    assert (aapl["quantity"], aapl["price"], aapl["value"], aapl["cost_basis"]) == (4000, 200, 800_000, 150_000)
    assert {h["symbol"] for h in s.holdings} == {"AAPL", "VTI", "CASH"}


def test_etrade_history_from_percent_chart_with_anchor():
    s = parse_text(ETRADE)
    assert (s.institution, s.account_type) == ("E*TRADE", "brokerage")          # not fooled by "retirement accounts"
    assert s.as_of == date(2025, 7, 31) and s.balance == pytest.approx(1_100_000)
    hist = {d: (v, exact) for d, v, exact in s.history}
    assert hist[date(2025, 6, 30)] == (pytest.approx(1_000_000), True)          # beginning value = prior month-end
    assert hist[date(2024, 12, 31)] == (pytest.approx(800_000), True)           # year-start anchor
    # Jun = 1.1M / 1.10; May = Jun / 1.05; Apr = May / 0.95 (chronological percentages)
    assert hist[date(2025, 5, 31)][0] == pytest.approx(1_000_000 / 1.05)
    assert hist[date(2025, 4, 30)][0] == pytest.approx(1_000_000 / 1.05 / 0.95)
    assert {h["symbol"]: h["cost_basis"] for h in s.holdings} == {"AAPL": 400_000, "CASH": None}
    assert [(g["grant_id"], g["quantity"], g["value"]) for g in s.grants] == [("111111", 100, 20_000), ("222222", 50, 10_000)]


def test_statement_extras_saved_and_summarized(tmp_path):
    from finance import db, portfolio
    from finance.importers import apply_statement
    conn = db.connect(tmp_path / "t.db")
    acct = db.add_account(conn, "E*TRADE", "E*TRADE", "brokerage")
    db.upsert_balance(conn, acct, "2025-05-31", 123.45, "manual")               # an existing exact value...
    s = parse_text(ETRADE)
    apply_statement(conn, s, acct, "etrade.pdf")
    bal = dict(zip(db.balance_history(conn)["date"].dt.strftime("%Y-%m-%d"), db.balance_history(conn)["balance"]))
    assert bal["2025-05-31"] == pytest.approx(123.45)                           # ...is never overwritten by the chart
    assert bal["2025-06-30"] == pytest.approx(1_000_000) and bal["2025-07-31"] == pytest.approx(1_100_000)
    pf = portfolio.summarize(db.latest_holdings(conn))
    assert pf["total"] == pytest.approx(1_100_000) and pf["gain"] == pytest.approx(600_000)
    assert pf["concentrated"][0][0] == "AAPL"
    assert db.latest_grants(conn)["value"].sum() == pytest.approx(30_000)


WEALTHFRONT = """ACCOUNT INFORMATION
Jane Sample and John Sample
Joint Automated Investing Account
ACCOUNT NUMBERS
Wealthfront: 8X123456
Monthly Statement for August 1 - 31, 2025
Joint Investment Account
August 1, 2025 Starting Balance $10,000.00
August 31, 2025 Ending Balance $10,550.00
I. Holdings as of August 31, 2025
Security Symbol/CUSIP Shares Share Price Value
Vanguard Total Stock Market ETF VTI 20 $300.0000 $6,000.00
ISHARES TR CALIF MUN BD ETF CMF 50.5 $50.0000 $2,525.00
Schwab International Equity ETF SCHF 67.5 $30.0000 $2,025.00
Total $10,550.00
DIVIDENDS
Date Type Security Symbol/ CUSIP Shares Taxable Value Tax-Exempt Value3 Total Value
7/31/2025 Cash RBC US Government Money Market Fund TIMXX -- $0.16 $0.00 $0.16
Securities held at Wealthfront Brokerage are not FDIC-insured.
"""


def test_wealthfront_statement_uses_ending_balance_not_dividend_total():
    s = parse_text(WEALTHFRONT)
    assert (s.kind, s.institution, s.account_type, s.last4) == ("investment", "Wealthfront", "brokerage", "3456")
    assert s.balance == pytest.approx(10_550) and s.as_of == date(2025, 8, 31)   # not the $0.16 dividend total
    assert s.history == [(date(2025, 7, 31), pytest.approx(10_000), True)]
    assert [h["symbol"] for h in s.holdings] == ["VTI", "CMF", "SCHF"] and not s.notes


def test_value_that_disagrees_with_positions_is_flagged():
    s = parse_text(WEALTHFRONT.replace("August 31, 2025 Ending Balance $10,550.00", "August 31, 2025 Ending Balance $0.16"))
    assert any("doesn't match the positions" in n for n in s.notes)


def test_muni_bond_funds_count_as_bonds():
    from finance import portfolio
    assert portfolio.asset_class("CMF", "ISHARES TR CALIF MUN BD ETF") == "Bonds"
    assert portfolio.asset_class("PWZ", "INVESCO EXCHANGE-TRADED FD TR CALIF AMT MUN") == "Bonds"
    assert portfolio.asset_class("SCHB", "Schwab U.S. Broad Market ETF") == "Stocks"


CREDIT_UNION_MORTGAGE = """Make Check Payable To:
The Sample 7 Credit Union
MORTGAGE STATEMENT
Statement Date: 08/03/2025
Property Address: 12  MAPLE CT
SPRINGFIELD CA 95000
Account Number 1234567890
Amount Due $2,000.00
Account Information
Outstanding Principal Balance $300,000.00
Interest Rate (Until Jul 2028) 6.500%
Explanation of Amount Due
Principal $375.00
Interest $1,625.00
Escrow (for Taxes and Insurance) $0.00
Regular Monthly Payment $2,000.00
Please note: If you have enrolled in our automatic payment service, your payment will process as scheduled.
TO THE EXTENT YOUR ORIGINAL OBLIGATION IS SUBJECT TO AN AUTOMATIC STAY OF BANKRUPTCY
"""


def test_credit_union_mortgage_not_mistaken_for_auto_loan():
    s = parse_text(CREDIT_UNION_MORTGAGE)
    assert (s.kind, s.account_type) == ("loan", "mortgage")            # "automatic payment" is not an auto loan
    assert s.institution == "Sample 7 Credit Union" and s.name_hint == "12 Maple Ct"
    assert s.balance == 300_000 and s.rate == pytest.approx(0.065) and s.payment == pytest.approx(2_000)
    assert s.as_of == date(2025, 8, 3) and s.last4 == "7890"


def test_real_auto_loan_still_detected():
    s = parse_text("Some Bank\nAuto Loan Statement\nVehicle: 2022 Honda\nPrincipal Balance $12,000.00\n"
                   "Interest Rate 5.0%\nAmount Due $400.00\nloan")
    assert s.account_type == "auto_loan"


ROBINHOOD_PAGE = """Robinhood Securities, LLC carries your account as the clearing broker.
Page  of {page} 9
08/01/2025 to 08/31/2025
Jane Sample
{label} Account #:{acct}
Account Summary Opening Balance Closing Balance
Net Account Balance $0.00 $0.00
Total Securities * ${open} ${close}
Portfolio Value ${open} ${close}
Portfolio Summary
Securities Held in Account Sym/Cusip Acct Type Qty Price Mkt Value Est. Dividend Yield % of Total Portfolio
SPDR S&P 500 ETF Trust
Estimated Yield: 1.02% SPY Cash 2 $700.00000 $1,400.00 $14.00 {spy_pct}%
{extra}Brokerage Cash Balance ${cash} 0.00%
"""


def test_robinhood_multi_account_pdf_splits_into_accounts():
    from finance.importers.statements import parse_pdf_all
    text = (ROBINHOOD_PAGE.format(page=1, label="Individual", acct="111114688", open="1,300.00", close="1,400.00",
                                  spy_pct=100, extra="", cash="0.00")
            + ROBINHOOD_PAGE.format(page=5, label="Roth IRA", acct="222222941", open="2,000.00", close="2,450.00",
                                    spy_pct=57, cash="50.00",
                                    extra="NVIDIA\nEstimated Yield: 0.13% NVDA Cash 5 $200.00000 $1,000.00 $1.30 41%\n"))
    got = parse_pdf_all(_pdf(text.splitlines()))
    assert [(s.institution, s.name_hint, s.account_type, s.balance) for s in got] == [
        ("Robinhood", "Individual …4688", "brokerage", 1_400.0),
        ("Robinhood", "Roth IRA …2941", "retirement", 2_450.0)]
    assert got[1].history == [(date(2025, 7, 31), 2_000.0, True)]
    assert {h["symbol"]: h["value"] for h in got[1].holdings} == {"SPY": 1_400.0, "NVDA": 1_000.0, "CASH": 50.0}
    assert not any(s.notes for s in got)                                    # values match their positions


APPLE_SAVINGS = """Statement
Savings Customer:
Jane Sample, jane@example.com Aug 1, 2025 - Aug 31, 2025
Account 910100011111
Routing 124085082
Deposit products provided by Goldman Sachs Bank USA, Salt Lake City Branch. Member FDIC.
Account Activity
Date Description Amount Balance
08/01/2025 Opening Balance $300.00
08/02/2025 Daily Cash Deposit $20.00 $320.00
Closing Balance $320.00
Account Summary
Beginning Balance (as of Aug 1) $300.00
Ending Balance (as of Aug 31) $320.00
"""


def test_apple_savings_statement():
    s = parse_text(APPLE_SAVINGS)
    assert (s.kind, s.account_type, s.institution, s.last4) == ("deposit", "savings", "Apple Savings", "1111")
    assert s.balance == 320 and s.as_of == date(2025, 8, 31)
    assert s.history == [(date(2025, 7, 31), 300.0, True)]                     # opening balance = July 31


FIDELITY_401K = """Statement Details
Acme 401(k) Plan Retirement Savings Statement
JANE SAMPLE
Your Account Summary Statement Period: 01/01/2021 to 09/25/2026
Beginning Balance $0.00
Employee Contributions $10,000.00
Employer Contributions $5,000.00
Balance Forward $100,000.00
Fees -$5.00
Change in Market Value $45,005.00
Ending Balance $160,000.00
LIFEPATH INDEX CORE FUNDS
Fidelity NetBenefits - Statement Details
Blended Fund Investments* $0.00$160,000.00
LifePath Idx
2040 A 0.000 1,000.000 $0.00 $40.00 $0.00 $40,000.00
LifePath Idx
2045 A 0.0004,000.000 $0.00 $30.00 $0.00 $120,000.00
Account Totals $0.00$160,000.00
ContributionsPeriod to
date
Inception To
Date
Vested
Percent
Total Account
Balance
Total Vested
Balance
Base Pay
Traditional $9,000.00 $9,000.00 100%$100,000.00$100,000.00
After-Tax $1,000.00 $1,000.00 100% $0.00 $0.00
Acme
Match $5,000.00 $5,000.00 100%$55,000.00$55,000.00
Roth in-
Plan
Conversion
$0.00 $0.00 100% $5,000.00 $5,000.00
Blended Investment Stocks Bonds Short-Term/Other
LifePath Idx 2040 A 73% 22% 5%
LifePath Idx 2045 A 82% 13% 5%
"""


def test_fidelity_401k_statement():
    s = parse_text(FIDELITY_401K)
    assert (s.kind, s.institution, s.account_type, s.name_hint) == ("investment", "Fidelity", "retirement", "Acme 401(k)")
    assert s.balance == 160_000 and s.as_of == date(2026, 9, 25)
    assert s.history == [(date(2020, 12, 31), 100_000.0, True)]                   # value when the period began
    assert [h["symbol"] for h in s.holdings] == ["LIFEPATH2040", "LIFEPATH2045"]
    assert s.holdings[1]["value"] == 120_000 and "82% stocks" in s.holdings[1]["description"]
    assert s.tax_sources == {"pre_tax": 100_000, "after_tax": 0.0, "employer_match": 55_000, "roth": 5_000}
    assert not s.notes                                         # nothing to warn about: it's shown as a detail
    assert "pre-tax $155,000 + Roth $5,000" in s.extras
    from finance import portfolio
    import pandas as pd
    pf = portfolio.summarize(pd.DataFrame(s.holdings).assign(account="401k"))
    assert pf["by_class"]["Stocks"] == pytest.approx(40_000 * 0.73 + 120_000 * 0.82)   # split by the fund's mix
    assert not pf["concentrated"]                                                   # a fund isn't one company
