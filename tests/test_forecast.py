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


def test_loan_payoff():
    assert insights.loan_payoff(12_000, 0.0, 1_000) == (12, 0.0)
    n, interest = insights.loan_payoff(15_800.50, 0.0099, 1_323.78)
    assert n == 12 and interest == pytest.approx(12 * 1_323.78 - 15_800.50, abs=1)
    assert insights.loan_payoff(100_000, 0.12, 500) is None                # never pays off
    assert insights.loan_payoff(10_000, None, 500) is None


def test_source_labels():
    assert insights.source_label("manual") == "Typed in"
    assert insights.source_label("eStmt_2026-09-01.pdf") == "Imported from eStmt_2026-09-01.pdf"
    assert insights.is_estimate("Estimated (payment schedule)") and insights.is_estimate("Approximate gold price")
    assert not insights.is_estimate("Redfin estimate history") and insights.source_label("Redfin estimate history") == "Redfin estimate"


def test_overview_insights_are_worked_out_from_the_data():
    today = pd.Timestamp("2026-09-26")
    rows = []
    for m in range(1, 7):                                            # Mar..Aug 2026: complete months
        d = (pd.Timestamp("2026-09-01") - pd.DateOffset(months=m)) + pd.Timedelta(days=4)
        rows += [(d, "ACME DES:PAYROLL", 10_000.0), (d, "DOVENMUEHLE MTG DES:MORTG PYMT", -8_000.0),
                 (d, "SAFEWAY", -1_000.0), (d, "Online Banking transfer to SAV 1234", -5_000.0)]
        rows.append((d, "UNITED AIRLINES", -4_000.0 if m == 1 else -200.0))      # August's travel jump
    t = pd.DataFrame(rows, columns=["date", "description", "amount"]).assign(category=None)
    e = insights.enrich(t, {"UNITED AIRLINES": ("Travel", "ai"), "ACME PAYROLL": ("Income", "ai")})
    accts = pd.DataFrame([
        {"id": 1, "name": "Checking", "type": "checking", "balance": 30_000, "rate": None, "payment": None, "as_of": "2026-08-01"},
        {"id": 2, "name": "Gold", "type": "precious_metal", "balance": 40_000, "rate": None, "payment": None, "as_of": "2026-06-20"},
        {"id": 3, "name": "Car loan", "type": "auto_loan", "balance": 12_000, "rate": 0.0, "payment": 1_000, "as_of": "2026-09-20"},
    ])
    totals = {"Cash": 30_000, "Investments": 0, "Property & valuables": 40_000, "Loans": 12_000, "Credit cards": 0}
    moves = pd.DataFrame({"account_id": [1], "name": ["Checking"], "then": [10_000.0], "now": [30_000.0], "change": [20_000.0]})
    out = {i["icon"]: i for i in insights.overview(e, accts, totals, moves, (20_000.0, 50_000.0, ["Gold"]), today=today)}
    cash_flow = out["💰"]["text"]                                     # 10K in; 8K + 1K + ~0.8K travel out
    assert "**$60K came in and $59K went out: you kept $1.0K**" in cash_flow    # transfers to savings aren't spending
    assert cash_flow.split("Biggest costs: ")[1].startswith("Mortgage $48K")
    e2 = insights.enrich(t.assign(amount=t["amount"].where(t["description"] != "ACME DES:PAYROLL", 5_000.0)),
                         {"UNITED AIRLINES": ("Travel", "ai")})
    short = {i["icon"]: i for i in insights.overview(e2, accts, totals, moves, (None, None, []), today=today)}
    assert "more than came in" in short["💸"]["text"]                 # spending more than pay: said plainly
    assert "Travel" in out["🔎"]["text"] and "August" in out["🔎"]["text"]
    assert "mostly Checking (+$20K)" in out["📈"]["text"] and "1 newer account not included" in out["📈"]["text"]
    assert "Gold" in out["📈"]["help"]                                # which ones: on the ⓘ, not in the sentence
    assert "Car loan" in out["🏦"]["text"] and "Sep 2027" in out["🏦"]["text"]    # 12 payments of $1,000
    assert "Checking (Aug 1)" in out["⏰"]["text"] and "Gold" not in out["⏰"]["text"]  # gold: fine for months
    assert all(i["help"] for i in out.values())


def test_volatility_not_inflated_by_yearly_points():
    """Regression: gold with yearly values 2021-2025 then monthly 2026 showed ±55%/yr swings - each year's move
    was treated as one month's."""
    from finance.assets import volatility
    yearly = pd.DataFrame({"date": pd.to_datetime([f"{y}-12-31" for y in range(2020, 2026)]),
                           "balance": 100 * 1.15 ** np.arange(6)})                     # steady +15%/yr
    monthly = pd.DataFrame({"date": pd.date_range("2026-01-31", periods=9, freq="ME"),
                            "balance": 100 * 1.15 ** 5 * np.where(np.arange(9) % 2, 1.03, 0.97)})   # ±3% a month
    v = volatility(pd.concat([yearly, monthly]))
    assert 0.05 < v < 0.25, v


def _year(today, accounts):
    """12 complete months of steady pay and spending, split across accounts as given: {name: (from, share)}."""
    rows = []
    for m in range(12, 0, -1):
        d = (today.to_period("M").to_timestamp() - pd.DateOffset(months=m)) + pd.Timedelta(days=1)
        for name, (begins, share) in accounts.items():
            if d >= pd.Timestamp(begins):
                rows += [(d, name, "ACME DES:PAYROLL", 10_000.0 * share), (d, name, "SAFEWAY", -8_000.0 * share)]
    return pd.DataFrame(rows, columns=["date", "account", "description", "amount"]).assign(category=None)


def test_once_a_year_tax_counts_once_over_twelve_months():
    today = pd.Timestamp("2026-09-26")
    t = _year(today, {"Checking": ("2000-01-01", 1.0)})
    t.loc[len(t)] = [pd.Timestamp("2026-04-10"), "Checking", "IRS DES:USATAXPYMT", -24_000.0, None]
    accts = pd.DataFrame([{"id": 1, "name": "Checking", "type": "checking", "balance": 1, "rate": None,
                           "payment": None, "as_of": "2026-09-20"}])
    first = insights.overview(insights.enrich(t, {"IRS USATAXPYMT": ("Income tax", "ai")}), accts, {"Cash": 1}, pd.DataFrame(
        columns=["account_id", "name", "then", "now", "change"]), (None, None, []), today=today)[0]
    assert "Over the last 12 months" in first["text"] and "Income tax $24K" in first["text"]   # counted once
    assert "Income tax $24K (mostly one IRS payment of $24K, Apr 2026)" in first["text"]


def test_new_card_taking_over_keeps_the_year_but_missing_history_shortens_it():
    today = pd.Timestamp("2026-09-26")
    none = pd.DataFrame(columns=["account_id", "name", "then", "now", "change"])
    accts = pd.DataFrame([{"id": 1, "name": "x", "type": "checking", "balance": 1, "rate": None, "payment": None,
                           "as_of": "2026-09-20"}])
    # spending moved from the old card to a new one in January: totals steady -> the whole year counts
    moved = _year(today, {"Old card": ("2000-01-01", 1.0)})
    moved = moved[~((moved["account"] == "Old card") & (moved["date"] >= "2026-01-01"))]
    moved = pd.concat([moved, _year(today, {"New card": ("2026-01-01", 1.0)})], ignore_index=True)
    i = insights.overview(insights.enrich(moved), accts, {"Cash": 1}, none, (None, None, []), today=today)[0]
    assert "Over the last 12 months" in i["text"] and "took over from another account" in i["help"]
    # a second account only imported from January (it existed before): totals jump -> start in January
    partial = pd.concat([_year(today, {"Checking": ("2000-01-01", 0.5)}), _year(today, {"Card": ("2026-01-01", 0.5)})])
    i = insights.overview(insights.enrich(partial), accts, {"Cash": 1}, none, (None, None, []), today=today)[0]
    assert "Over the last 8 months" in i["text"] and "no history before Jan 2026" in i["help"]


def test_money_between_the_two_of_you_is_a_transfer_and_refunds_reduce_spending():
    t = pd.DataFrame({"date": pd.to_datetime(["2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]),
                      "description": ["TRANSFER DEEPALI DOMBA SALIAN", "Old Navy", "CL CHASE TRAVEL", "UNITED AIRLINES"],
                      "amount": [2_000.0, -50.0, 300.0, -1_000.0], "purchaser": [None, "Deepali Salian", None, None],
                      "category": None})
    e = insights.enrich(t, {"CL CHASE TRAVEL": ("Travel", "ai"), "UNITED AIRLINES": ("Travel", "ai")})
    assert e.set_index("description").loc["TRANSFER DEEPALI DOMBA SALIAN", "category"] == "Transfer"
    cf = insights.monthly_cash_flow(t, {"CL CHASE TRAVEL": ("Travel", "ai"), "UNITED AIRLINES": ("Travel", "ai")})
    assert cf["money_in"].iloc[0] == 0 and cf["money_out"].iloc[0] == pytest.approx(750)   # 50 + 1000 - 300


def test_property_tax_named_by_home_without_repeating_itself():
    today = pd.Timestamp("2026-09-26")
    t = _year(today, {"Savings": ("2000-01-01", 1.0)})
    for d in ("2025-11-10", "2026-03-10"):
        t.loc[len(t)] = [pd.Timestamp(d), "Savings", "SANTA CLARA DTAC SANTACLARA", -25_000.0, None]
    accts = pd.DataFrame([{"id": 1, "name": "x", "type": "savings", "balance": 1, "rate": None, "payment": None,
                           "as_of": "2026-09-20"}])
    args = (insights.enrich(t), accts, {"Cash": 1}, pd.DataFrame(columns=["account_id", "name", "then", "now", "change"]),
            (None, None, []))
    i = insights.overview(*args, today=today, property_tax_shares={"Bellgrove Home": 19_692.80, "Schott Home": 5_437.34})[0]
    assert "Property tax $50K (Bellgrove $39K, Schott $11K)" in i["text"]
    assert i["text"].count("$50K") == 1                                    # said once, not again as a big payment


def test_living_costs_grow_with_inflation_loan_payments_stay_fixed_and_stop():
    a = forecast.Assumptions(years=3, investment_return=0.0, investment_volatility=0.0, cash_yield=0.0,
                             inflation=0.03, monthly_income=10_000, monthly_living=6_000, income_growth=0.0)
    fc = forecast.run(accts([("Checking", "checking", 0, None, None),
                             ("Car loan", "auto_loan", 12_000, 0.0, 1_000)]), a)
    f = fc.cash_flow.set_index("year")
    y0, y1 = f.index[0], f.index[0] + 1
    assert f.loc[y1, "living"] / f.loc[y1, "months"] > f.loc[y0, "living"] / f.loc[y0, "months"]   # inflates
    assert f["loans"].sum() == pytest.approx(12_000)                         # fixed payments, then they stop
    assert f["income"].sum() == pytest.approx(10_000 * 36)                   # no raises assumed here
    assert (f["saved"] == f["income"] - f["living"] - f["loans"]).all()
    # 3 years: living 6,000 a month rising 3%/yr; the saved total lands in cash
    living = sum(6_000 * 1.03 ** (m / 12) for m in range(36))
    end = fc.expected.iloc[-1]                                                # saved -> 80% invested, rest cash
    assert end["cash"] + end["investments"] == pytest.approx(10_000 * 36 - living - 12_000, rel=1e-6)


def test_cash_flow_baseline_splits_loans_from_living_costs():
    today = pd.Timestamp("2026-09-26")
    t = _year(today, {"Checking": ("2000-01-01", 1.0)})              # 10K pay, 8K Safeway each month
    for m in range(12, 0, -1):
        d = today.to_period("M").to_timestamp() - pd.DateOffset(months=m) + pd.Timedelta(days=2)
        t.loc[len(t)] = [d, "Checking", "DOVENMUEHLE MTG DES:MORTG PYMT", -3_000.0, None]
    b = insights.cash_flow_baseline(insights.enrich(t), today=today)
    assert b["months"] == 12 and b["income"] == pytest.approx(10_000)
    assert b["living"] == pytest.approx(8_000) and b["loans_seen"] == pytest.approx(3_000)


def test_planned_refinance_lowers_the_payment_from_that_month():
    base = dict(years=2, investment_return=0.0, investment_volatility=0.0, cash_yield=0.0, inflation=0.0,
                monthly_income=20_000, monthly_living=5_000, income_growth=0.0)
    loan = accts([("Checking", "checking", 0, None, None), ("Bellgrove", "mortgage", 1_700_000, 0.06625, 12_051.47)])
    now = forecast.run(loan, forecast.Assumptions(**base))
    refi = forecast.run(loan, forecast.Assumptions(**base, loan_changes=[
        {"loan": "Bellgrove", "payment": 9_000, "month": 3, "rate": 0.055}]))
    f0, f1 = now.cash_flow.set_index("period"), refi.cash_flow.set_index("period")
    assert f0.loc[0, "loans"] == pytest.approx(12 * 12_051.47)
    assert f1.loc[0, "loans"] == pytest.approx(2 * 12_051.47 + 10 * 9_000)         # months 1-2 old, then new
    assert f1.loc[1, "saved"] - f0.loc[1, "saved"] == pytest.approx(12 * (12_051.47 - 9_000))
    assert forecast.Assumptions.from_dict(forecast.Assumptions(**base, loan_changes=[{"loan": "x", "payment": 1}]).to_dict()).loan_changes


def test_history_averages_match_the_published_long_run_figures():
    from finance import history
    h = history.summary(30)
    assert 0.02 < h["inflation"] < 0.03 and 0.095 < h["stocks"] < 0.115 and 0.15 < h["stock_volatility"] < 0.2
    assert history.summary(10)["from"] == history.YEARLY.index[-10]


def test_range_replays_real_market_years():
    """The forecast's range comes from real stock years (crashes included), scaled to the chosen return."""
    a = forecast.Assumptions(years=10, investment_return=0.07, inflation=0.0, simulations=2000)
    fc = forecast.run(accts([("Brokerage", "brokerage", 100_000, None, None)]), a)
    end = fc.bands.iloc[-1]
    assert end["p50"] == pytest.approx(100_000 * 1.07 ** 10, rel=0.12)       # typical outcome at the chosen return
    assert end["p10"] < 0.75 * end["p50"] < end["p50"] < end["p90"]            # real years' ups and downs
    flat = forecast.run(accts([("Brokerage", "brokerage", 100_000, None, None)]),
                        forecast.Assumptions(years=10, investment_return=0.07, replay_history=False, simulations=2000))
    assert flat.bands.iloc[-1]["p90"] != end["p90"]                           # a different engine was used


def test_money_paid_back_is_not_income_but_dividends_are():
    t = pd.DataFrame({"date": pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]),
                      "description": ["ACME DES:PAYROLL", "UNITED HEALTHCAR INS PAYMNT", "ZELLE PAYMENT FROM A FRIEND",
                                      "BOFA FIN CTR DEPOSIT", "KAISER"],
                      "amount": [10_000.0, 600.0, 200.0, 1_000.0, -900.0], "category": None})
    rules = {"UNITED HEALTHCAR INS PAYMNT": ("Other income", "ai"), "KAISER": ("Health", "ai"),
             "ZELLE PAYMENT FROM A FRIEND": ("Payments to people", "ai")}
    cf = insights.monthly_cash_flow(t, rules)
    assert cf["money_in"].iloc[0] == 10_000                      # only the pay
    assert cf["money_out"].iloc[0] == pytest.approx(900 - 600 - 200 - 1_000)   # paid back reduces spending
    h = pd.DataFrame({"account": ["DS", "DS", "Merrill"], "symbol": ["AAPL", "CASH", "AAPL"],
                      "quantity": [100.0, 0.0, 50.0], "value": [1.0, 1.0, 1.0]})
    assert insights.yearly_dividends(h, {"AAPL": 1.06}) == {"DS": pytest.approx(106.0), "Merrill": pytest.approx(53.0)}
