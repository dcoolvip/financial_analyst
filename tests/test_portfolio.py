import pandas as pd
import pytest

from finance import portfolio


def h(rows):
    return pd.DataFrame(rows, columns=["account", "symbol", "description", "value"])


def test_asset_classes():
    assert portfolio.asset_class("AAPL", "APPLE INC") == "Stocks"
    assert portfolio.asset_class("VTI", "VANGUARD TOTAL STOCK MARKET ETF") == "Stocks"
    assert portfolio.asset_class("BND", "VANGUARD TOTAL BOND MARKET ETF") == "Bonds"
    assert portfolio.asset_class("CASH", "MERRILL LYNCH BANK DEPOSIT PROGRAM") == "Cash"
    assert portfolio.asset_class("SPAXX", "FIDELITY GOVERNMENT MONEY MARKET") == "Cash"
    assert portfolio.asset_class("XYZXX", "") == "Cash"                   # money-market ticker shape


def test_summary_mix_and_concentration():
    s = portfolio.summarize(h([
        ("Merrill", "VTI", "VANGUARD TOTAL STOCK MARKET ETF", 50_000),
        ("Merrill", "AAPL", "APPLE INC", 20_000),
        ("Morgan Stanley", "AAPL", "APPLE INC", 15_000),     # same stock, second account
        ("Merrill", "BND", "VANGUARD TOTAL BOND MARKET ETF", 10_000),
        ("Merrill", "CASH", "BANK DEPOSIT PROGRAM", 5_000),
    ]))
    assert s["total"] == 100_000
    assert s["by_class"] == {"Stocks": 85_000, "Bonds": 10_000, "Cash": 5_000}
    assert s["concentrated"] == [("AAPL", pytest.approx(0.35))]         # funds never flagged, even at 50%
    top = s["top"].iloc[0]
    assert top.symbol == "VTI"
    aapl = s["top"][s["top"]["symbol"] == "AAPL"].iloc[0]
    assert aapl.value == 35_000 and aapl.accounts == "Merrill, Morgan Stanley"


def test_empty():
    assert portfolio.summarize(h([]))["total"] == 0
