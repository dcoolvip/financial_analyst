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


def test_each_valuable_grows_at_its_own_rate():
    accts = pd.DataFrame([
        {"id": 1, "name": "House", "type": "property", "balance": 1_000_000, "rate": None, "payment": None},
        {"id": 2, "name": "Car", "type": "vehicle", "balance": 40_000, "rate": None, "payment": None},
        {"id": 3, "name": "Gold", "type": "precious_metal", "balance": 10_000, "rate": None, "payment": None},
        {"id": 4, "name": "Pokemon cards", "type": "collectible", "balance": 40_000, "rate": 0.10, "payment": None},
    ])
    a = forecast.Assumptions(years=1, investment_volatility=0.0, home_appreciation=0.035, vehicle_depreciation=0.15)
    fc = forecast.run(accts, a)
    v = fc.valuables.set_index("name")
    assert v.loc["House", "end"] == pytest.approx(1_035_000, rel=1e-3)          # Home slider
    assert v.loc["Car", "end"] == pytest.approx(34_000, rel=1e-3)               # -15%/yr
    assert v.loc["Gold", "end"] == pytest.approx(10_400, rel=1e-3)             # precious-metal default +4%
    assert v.loc["Pokemon cards", "end"] == pytest.approx(44_000, rel=1e-3)    # your own +10% wins
    assert v.loc["Pokemon cards", "source"] == "yours" and v.loc["Gold", "source"] == "default"
    assert fc.expected["property"].iloc[-1] == pytest.approx(v["end"].sum())


def test_asset_trend_from_its_own_history():
    from finance.assets import trend
    h = pd.DataFrame({"date": pd.to_datetime(["2025-07-04", "2026-07-04"]), "balance": [16_000.0, 20_000.0]})
    assert trend(h) == pytest.approx(0.25, rel=1e-2)
    assert trend(h.iloc[:1]) is None                                             # one point isn't a trend
