"""The chart reader recovers a known curve from a Redfin-style screenshot (drawn here, so values are exact)."""
import io

import numpy as np
import pandas as pd
import pytest
from PIL import Image, ImageDraw

from finance.importers.chart import history_from_chart


def redfin_like(values):
    """White page: a divider line, dashed gridlines $5.0M..$2.5M, a dark curve, labels and a side panel."""
    W, H = 1600, 700
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.line([(0, 90), (W, 90)], fill=(220, 220, 220), width=2)                 # page divider (not a gridline)
    rows = {5.0: 150, 4.5: 210, 4.0: 270, 3.5: 330, 3.0: 390, 2.5: 450}
    for v, y in rows.items():
        for x in range(60, 1100, 12):                                         # dashed gridlines
            d.line([(x, y), (x + 6, y)], fill=(222, 222, 222), width=2)
        d.text((1115, y - 6), f"${v:.1f}M", fill=(90, 90, 90))                # axis labels (not the curve)
    d.rectangle([1250, 100, 1580, 600], outline=(200, 200, 200))
    d.text((1270, 300), "Schedule a consultation", fill=(20, 20, 20))         # side-panel text
    xs = np.linspace(80, 1080, len(values))
    ys = [150 + (5.0e6 - v) / 2.5e6 * 300 for v in values]
    d.line(list(zip(xs, ys)), fill=(30, 60, 70), width=3)                      # the value line
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_reads_known_curve_and_ignores_labels_and_dividers():
    months = pd.date_range("2021-09-30", periods=61, freq="ME")
    truth = 3.8e6 + 0.4e6 * np.sin(np.arange(61) / 6)                         # wiggly, like a real estimate
    h = history_from_chart(redfin_like(truth), 5_000_000, 2_500_000, pd.Timestamp("2021-09-30"),
                           pd.Timestamp("2026-09-30"))
    got = pd.Series(h["value"].to_numpy(), index=pd.to_datetime(h["date"]))
    assert len(got) == 61
    err = np.abs(got.to_numpy() - truth) / truth
    assert np.median(err) < 0.01 and err.max() < 0.03


def test_exact_end_value_pins_the_last_point():
    truth = np.full(61, 4.0e6)
    h = history_from_chart(redfin_like(truth), 5_000_000, 2_500_000, pd.Timestamp("2021-09-30"),
                           pd.Timestamp("2026-09-30"), exact_end_value=4_191_090)
    assert h["value"].iloc[-1] == 4_191_090 and h["value"].iloc[0] == pytest.approx(4_191_090, rel=0.02)
