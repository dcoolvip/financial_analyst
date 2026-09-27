import numpy as np
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
    assert v.loc["Pokemon cards", "source"] == "yours" and v.loc["Gold", "source"] == "long-run"
    assert fc.expected["property"].iloc[-1] == pytest.approx(v["end"].sum())


def test_asset_trend_from_its_own_history():
    from finance.assets import trend
    h = pd.DataFrame({"date": pd.to_datetime(["2025-07-04", "2026-07-04"]), "balance": [16_000.0, 20_000.0]})
    assert trend(h) == pytest.approx(0.25, rel=1e-2)
    assert trend(h.iloc[:1]) is None                                             # one point isn't a trend


def test_history_earns_weight_as_it_gets_longer():
    from finance.assets import outlook
    def hist(years, yearly):
        d = pd.date_range("2010-01-31", periods=int(years * 12) + 1, freq="ME")
        return pd.DataFrame({"date": d, "balance": 100 * (1 + yearly) ** (np.arange(len(d)) / 12)})
    short = outlook("collectible", None, hist(1, 0.80))          # a hot year
    long = outlook("property", None, hist(10, 0.06))             # a steady decade
    assert short["rate"] == pytest.approx((80 + 5 * 3) / 6 / 100, abs=0.005)   # ~16%, not 80%
    assert long["rate"] == pytest.approx((10 * 6 + 5 * 3.5) / 15 / 100, abs=0.002)  # mostly its own 6%
    assert outlook("collectible", 0.10, hist(1, 0.80))["rate"] == 0.10         # your rate wins
    assert outlook("precious_metal", None, hist(0.2, 0.5))["source"] == "long-run"   # too short to count


def test_volatility_from_history_widens_the_range():
    from finance.assets import volatility
    d = pd.date_range("2024-01-31", periods=25, freq="ME")
    calm = pd.DataFrame({"date": d, "balance": 100 * 1.003 ** np.arange(25)})
    wild = pd.DataFrame({"date": d, "balance": 100 * np.where(np.arange(25) % 2, 1.3, 0.8)})
    assert volatility(calm) < 0.01 < 0.5 < volatility(wild)
    base = {"id": 1, "name": "Cards", "type": "collectible", "balance": 10_000, "rate": 0.05, "payment": None}
    a = forecast.Assumptions(years=5)
    hist_rows = wild.assign(account_id=1)
    band = forecast.run(pd.DataFrame([base]), a, history=hist_rows).bands.iloc[-1]
    narrow = forecast.run(pd.DataFrame([base]), a, history=calm.assign(account_id=1)).bands.iloc[-1]
    assert (band["p90"] - band["p10"]) > 3 * (narrow["p90"] - narrow["p10"])


def test_pokemon_partial_snapshots_are_skipped(tmp_path):
    """Regression: a snapshot that priced only 25 of ~1,700 cards made the collection 'crash' to $277."""
    import sqlite3
    from finance import pokemon
    p = tmp_path / "pokemon.db"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE cards (id INTEGER PRIMARY KEY, owned INTEGER);"
                    "CREATE TABLE price_history (card_id INTEGER, snap_date TEXT, raw REAL, tcg REAL);")
    c.executemany("INSERT INTO cards VALUES (?, 1)", [(i,) for i in range(100)])
    rows = []
    for m, price in (("2025-07-31", 10.0), ("2025-08-31", 11.0), ("2025-09-30", 12.0)):
        rows += [(i, m, price, None) for i in range(100)]
    rows += [(i, "2025-10-31", 99.0, None) for i in range(3)]                  # a partial run: 3 cards
    rows += [(i, "2025-10-15", 13.0, None) for i in range(100)]                # October's complete run
    rows += [(i, "2026-01-31", 14.0, None) for i in range(100)]
    c.executemany("INSERT INTO price_history VALUES (?, ?, ?, ?)", rows)
    c.commit()
    h = pokemon.collection_history(p)
    assert h["date"].dt.strftime("%Y-%m-%d").tolist() == ["2025-07-31", "2025-08-31", "2025-09-30", "2025-10-15", "2026-01-31"]
    assert h["value"].min() == 1000                                            # never the 3-card $297
    ix = pokemon.price_index(p, list(h["date"]))
    assert ix["index"].iloc[-1] == pytest.approx(1.4)                          # 10 -> 14 per card


def test_vehicle_history_from_price_and_todays_value():
    from finance.assets import trend, vehicle_history
    h = vehicle_history(30_000, "2020-03-15", 12_000, "2026-03-15")
    assert h["value"].iloc[0] == 30_000 and h["value"].iloc[-1] == 12_000   # pinned at both ends
    assert h["value"].is_monotonic_decreasing
    first_year = 1 - h["value"].iloc[12] / 30_000
    last_year = 1 - h["value"].iloc[-1] / h["value"].iloc[-13]
    assert first_year > last_year                                            # steepest when new
    assert trend(h.rename(columns={"value": "balance"})) == pytest.approx(-0.14, abs=0.02)
