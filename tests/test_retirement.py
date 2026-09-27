"""Retirement: pay stops, Social Security starts, healthcare by age, 401(k) taxed on the way out. Made-up people."""
import pandas as pd
import pytest

from finance import forecast, retirement as ret


def test_social_security_uses_the_ssa_estimates_between_ages():
    p = {"ss_table": {62: 2_000, 67: 3_000, 70: 3_720}, "ss_monthly": 3_000}
    assert ret.social_security(p, 62) == 2_000 and ret.social_security(p, 70) == 3_720
    assert ret.social_security(p, 64.5) == pytest.approx(2_500)
    assert ret.social_security({"ss_monthly": 3_000}, 62) == pytest.approx(2_100)     # standard 70% at 62
    assert ret.social_security({"ss_monthly": 3_000}, 70) == pytest.approx(3_720)     # +24% at 70


def test_healthcare_by_age_and_who_is_working():
    assert ret.health_cost(60, working=True, partner_working=False, private_yearly=15_000, medicare_yearly=12_000) == 0
    assert ret.health_cost(60, False, True, 15_000, 12_000) == 0                      # on the spouse's plan
    assert ret.health_cost(60, False, False, 15_000, 12_000) == 15_000                # private until 65
    assert ret.health_cost(65, False, False, 15_000, 12_000) == 12_000                # Medicare


def test_pay_owner_by_paycheck_pattern():
    people = [{"name": "A", "pay": [{"pattern": r"ACME.*ID:\s*111"}]},
              {"name": "B", "pay": [{"pattern": r"ACME.*ID:\s*222"}, {"pattern": r"ACME.*PAYROLL", "account": "Chase"}]}]
    assert ret.pay_owner(people, "ACME DES:PAYROLL ID:111 INDN:X", "BofA") == "A"
    assert ret.pay_owner(people, "ACME PAYROLL PPD", "Chase") == "B" and ret.pay_owner(people, "ACME PAYROLL", "BofA") is None


def _run(retire_a=65, claim=67, years=40, **kw):
    now = pd.Timestamp.today()
    people = [{"name": "A", "born": f"{now.year - 60}-{now.month:02d}", "retire_age": retire_a, "pay": 10_000,
               "ss_monthly": 3_000, "ss_claim_age": claim, "k401_yearly": 0},
              {"name": "B", "born": f"{now.year - 56}-{now.month:02d}", "retire_age": 65, "pay": 5_000,
               "ss_monthly": 2_000, "ss_claim_age": claim}]
    base = dict(years=years, investment_return=0.0, investment_volatility=0.0, cash_yield=0.0, inflation=0.0,
                health_extra_growth=0.0, monthly_income=15_000, monthly_living=6_000, income_growth=0.0,
                people=people, other_income=1_000)
    a = forecast.Assumptions(**{**base, **kw})
    accts = pd.DataFrame([("Brokerage", "brokerage", 1_000_000, None, None)], columns=["name", "type", "balance", "rate", "payment"])
    return forecast.run(accts, a)


def test_pay_stops_at_retirement_social_security_and_medicare_start():
    f = _run().cash_flow.set_index("period")
    assert f.loc[0, "pay"] == pytest.approx(12 * 15_000)            # both working
    assert f.loc[6, "pay"] == pytest.approx(12 * 5_000)             # A retired at 65 (in 5 years), B still working
    assert f.loc[10, "pay"] == 0                                    # both retired
    assert f.loc[0, "health"] == 0                                  # employer plans while working
    assert f.loc[6, "health"] == pytest.approx(12_000)              # A on Medicare
    assert f.loc[12, "ss"] == pytest.approx(12 * 5_000)             # both claiming at 67
    assert f.loc[3, "other"] == pytest.approx(12 * 1_000)           # rent / dividends continue


def test_early_retirement_pays_private_insurance_until_65():
    f = _run(retire_a=60).cash_flow.set_index("period")
    assert f.loc[0, "pay"] == pytest.approx(12 * 5_000)             # A retired now
    assert f.loc[1, "health"] == 0                                  # B still works: A on B's plan


def test_pretax_401k_is_taxed_when_it_has_to_be_used():
    fc = _run(use_tax_brackets=False, years=45, pretax_balance=400_000, pretax_tax_rate=0.30)
    assert fc.cash_flow["tax_401k"].sum() > 0                       # required withdrawals / spending, taxed
    plain = _run(use_tax_brackets=False, years=45, pretax_balance=0)
    assert fc.expected["net_worth"].iloc[-1] < plain.expected["net_worth"].iloc[-1]   # tax makes pre-tax worth less


def test_401k_withdrawals_count_as_money_in_and_their_tax_as_money_out():
    """Regression: required 401(k) withdrawals showed only their tax (as money out), so the chart looked like a
    huge shortfall - the withdrawal itself (mostly reinvested) was missing from money in."""
    from finance import charts
    fc = _run(use_tax_brackets=False, years=45, pretax_balance=2_000_000, pretax_tax_rate=0.30)
    f = fc.cash_flow
    late = f[f["tax_401k"] > 0].iloc[-1]
    assert late["out_401k"] == pytest.approx(late["tax_401k"] / 0.30, rel=0.02)          # gross vs its tax
    fig = charts.cash_flow_ahead(f, False, "light", events=[(2040, "A retires"), (2040, "A Medicare")])
    money_in = next(d for d in fig.data if d.name == "Money in")
    costs = f["living"] + f["loans"] + f["health"] + f["college"] + f["tax_401k"] + f["cg_tax"] + f["income_tax"]
    used = (costs - f["income"]).clip(lower=0).clip(upper=f["out_401k"])
    assert list(money_in.y) == pytest.approx(list(f["income"] + used))      # only what pays for spending + taxes
    assert all(money_in.y[i] <= costs[i] + f["income"][i] + 1 for i in range(len(f)))
    labels = [a.text for a in fig.layout.annotations]
    assert labels == ["A retires · A Medicare"]                                            # one label per year
    assert fig.layout.legend.y < 0                                                         # legend below the chart


def test_selling_shares_to_cover_spending_pays_capital_gains_tax():
    taxed = _run(use_tax_brackets=False, years=30, capital_gains_rate=0.33, gain_share=0.7)
    untaxed = _run(use_tax_brackets=False, years=30, capital_gains_rate=0.0)
    assert taxed.cash_flow["cg_tax"].sum() > 0 and untaxed.cash_flow["cg_tax"].sum() == 0
    assert taxed.expected["net_worth"].iloc[-1] < untaxed.expected["net_worth"].iloc[-1]


def test_college_is_paid_and_kids_costs_end():
    now = pd.Timestamp.today()
    kids = [{"name": "K1", "born": f"{now.year - 10}-{now.month:02d}"}, {"name": "K2", "born": f"{now.year - 10}-{now.month:02d}"}]
    fc = _run(years=15, kids=kids, kid_costs_monthly=2_000, college_yearly=80_000, college_extra_growth=0.0)
    f = fc.cash_flow.set_index("period")
    assert f.loc[7, "college"] == 0                                          # age 17
    assert f.loc[8, "college"] == pytest.approx(2 * 80_000)                  # both in college (no inflation here)
    assert f.loc[12, "college"] == 0                                         # graduated
    assert f.loc[7, "living"] - f.loc[9, "living"] == pytest.approx(12 * 2_000)   # their day-to-day costs ended


def test_reinvested_401k_money_has_no_gain_to_tax():
    """Regression: capital-gains tax kept being charged after required 401(k) withdrawals began, though the
    reinvested withdrawal money had no gain in it - every sale was taxed as 70% gain."""
    fc = _run(use_tax_brackets=False, years=40, pretax_balance=900_000, capital_gains_rate=0.33, gain_share=0.7)
    f = fc.cash_flow.set_index("period")
    rmd = f[f["out_401k"] > 0]
    assert len(rmd) > 5 and rmd["cg_tax"].iloc[-1] < 0.7 * rmd["cg_tax"].iloc[0]   # new money dilutes the gain


def test_retired_years_use_real_brackets_and_fill_the_24_percent_bracket_with_roth_conversions():
    kw = dict(years=40, pretax_balance=3_000_000, dividends_monthly=500, investment_return=0.06)
    fill, req = _run(withdrawal_strategy="fill24", **kw), _run(withdrawal_strategy="required", **kw)
    f, r = fill.cash_flow.set_index("period"), req.cash_flow.set_index("period")
    retired = f.index[f["pay"] == 0]
    assert f.loc[retired, "roth_conv"].sum() > 0 and r["roth_conv"].sum() == 0      # conversions only with fill
    assert f.loc[retired, "income_tax"].sum() > 0                                   # real tax, every retired year
    assert f.loc[retired[0], "tax_401k"] == 0                                       # not the flat rate any more
    late = f.index[-1]
    assert f.loc[late, "out_401k"] < r.loc[late, "out_401k"]                        # smaller required withdrawals
    after_tax = lambda fc: fc.expected["net_worth"].iloc[-1] - 0.45 * fc.expected["pretax"].iloc[-1]   # noqa: E731
    assert after_tax(fill) > after_tax(req)                    # counting the tax still owed on the 401(k)


def test_kids_supported_after_college_worst_case():
    now = pd.Timestamp.today()
    kids = [{"name": "K1", "born": f"{now.year - 10}-{now.month:02d}"}, {"name": "K2", "born": f"{now.year - 10}-{now.month:02d}"}]
    life = _run(years=30, kids=kids, kid_support_yearly=35_000, college_extra_growth=0.0).cash_flow.set_index("period")
    none = _run(years=30, kids=kids, kid_support_yearly=0.0, college_extra_growth=0.0).cash_flow.set_index("period")
    assert life.loc[11, "kid_support"] == 0                                  # still in college at 21
    assert life.loc[13, "kid_support"] == pytest.approx(2 * 35_000)          # both at home after college
    assert life.loc[29, "kid_support"] == pytest.approx(2 * 35_000)          # for life
    until26 = _run(years=30, kids=kids, kid_support_yearly=35_000, kid_support_until_age=26).cash_flow.set_index("period")
    assert until26.loc[20, "kid_support"] == 0 and none["kid_support"].sum() == 0
