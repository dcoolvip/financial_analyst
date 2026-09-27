import pandas as pd
import pytest

from finance import gold

PRICES = b"""Date,Open,High,Low,Close,Volume
2024-01-31,2050,2080,2010,2040,0
2024-02-29,2040,2060,2000,2050,0
2024-03-31,2050,2250,2040,2230,0
2024-04-15,2230,2400,2220,2380,0
"""


def test_value_is_ounces_times_price():
    p = gold.read_prices(PRICES)
    h = gold.history(311.035, p)                      # 10 troy ounces of 24k (0.999)
    assert h["value"].tolist() == [round(2040 * 9.99), round(2050 * 9.99), round(2230 * 9.99), round(2380 * 9.99)]
    assert str(h["date"].iloc[-1]) == "2024-04-15"    # latest price on its real date, not month-end


def test_history_starts_when_you_got_it_and_purity_counts():
    h = gold.history(311.035, gold.read_prices(PRICES), since="2024-02-15", purity=gold.KARAT_PURITY[22])
    assert len(h) == 3 and h["value"].iloc[0] == pytest.approx(2050 * 9.16, abs=1)   # Feb close


def test_generic_price_csv():
    p = gold.read_prices(b'date,price\n2025-01-02,"$2,650.10"\n2025-02-03,"$2,800.00"\n')
    assert p.iloc[-1] == 2800.0 and isinstance(p.index, pd.DatetimeIndex)
