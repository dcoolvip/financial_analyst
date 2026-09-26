import pandas as pd
import pytest

from finance import forecast, insights


def accts(rows):
    return pd.DataFrame(rows, columns=["name", "type", "balance", "rate", "payment"])


def test_growth_without_volatility_matches_compound_interest():
    a = forecast.Assumptions(years=10, investment_return=0.07, investment_volatility=0.0, inflation=0.03)
    fc = forecast.run(accts([("Brokerage", "brokerage", 100_000, None, None)]), a)
    end = fc.bands.iloc[-1]
    assert end["p50"] == pytest.approx(100_000 * 1.07 ** 10, rel=1e-3)
    assert end["p50_real"] == pytest.approx(end["p50"] / 1.03 ** 10, rel=1e-6)
    assert end["p10"] == pytest.approx(end["p90"])        # no volatility, no spread


def test_paid_off_loan_payment_becomes_savings():
    a = forecast.Assumptions(years=3, investment_return=0.0, investment_volatility=0.0, cash_yield=0.0,
                             invest_share=1.0, savings_growth=0.0)
    fc = forecast.run(accts([("Car loan", "auto_loan", 1_000, 0.0, 500.0)]), a)
    assert [m[1] for m in fc.milestones][:2] == ["Car loan paid off", "Debt-free (all loans paid off)"]
    # paid off after 2 months, then 34 months of the freed $500 get invested
    assert fc.expected["investments"].iloc[-1] == pytest.approx(34 * 500)


def test_missing_loan_terms_are_estimated_and_flagged():
    loans, revolving = forecast.build_loans(accts([
        ("Mortgage", "mortgage", 300_000, None, None),
        ("Visa", "credit_card", 2_000, None, None),
    ]))
    assert revolving == 2_000                               # card assumed paid in full monthly
    assert len(loans) == 1 and loans[0].estimated
    assert loans[0].payment == pytest.approx(forecast.payment_for(300_000, 0.065, 25))


def test_transfers_are_not_spending():
    t = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-20"]),
        "description": ["ACME DES:PAYROLL", "Online Banking transfer to SAV 1234",
                        "BANK OF AMERICA CREDIT CARD Bill Payment", "WHOLE FOODS"],
        "amount": [5000.0, -1000.0, -800.0, -200.0],
        "category": [None] * 4,
    })
    cf = insights.monthly_cash_flow(t)
    assert cf["money_in"].iloc[0] == 5000 and cf["money_out"].iloc[0] == 200
