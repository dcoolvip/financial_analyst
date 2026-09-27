"""Value history read off a chart screenshot (Redfin / Zillow estimate history, KBB value charts...).

Neither site offers a history download, but the chart on the page IS the history. This reads it locally
(no AI, nothing sent anywhere):
  * gridlines  -> the rows where a light grey line spans the chart; you type the top and bottom labels
  * the curve  -> the coloured / dark line between them, one reading per pixel column
  * time axis  -> the curve's left and right ends = the chart's start and end (e.g. 5 years to today)
  * optional   -> today's exact value (the big number above the chart) pins the last point exactly
Accuracy on a Redfin screenshot: within ~0.5% of the values Redfin prints.
"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd


class ChartNotFound(ValueError):
    pass


def _gridlines(a: np.ndarray) -> tuple[list[float], int, int]:
    """Centres of horizontal light-grey gridlines, and their left/right extent."""
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    grey = (np.abs(r - g) < 14) & (np.abs(g - b) < 14) & (r > 170) & (r < 245)
    H, W = grey.shape
    frac = grey.mean(axis=1)
    rows = [y for y in range(H) if frac[y] > 0.18]
    centres, run = [], []
    for y in rows:
        if run and y - run[-1] > 2:
            centres.append(sum(run) / len(run))
            run = []
        run.append(y)
    if run:
        centres.append(sum(run) / len(run))
    # A real gridline has its value label ("$4.5M") just past its right end; page dividers don't.
    dark = (r + g + b) < 400

    def labelled(c):
        y = int(round(c))
        cols = np.flatnonzero(grey[y])
        if not len(cols):
            return False
        groups, start = [], cols[0]                     # the dashed line = the longest run of dashes
        for c0, c1 in zip(cols, cols[1:]):
            if c1 - c0 > 20:
                groups.append((start, c0))
                start = c1
        groups.append((start, cols[-1]))
        end = max(groups, key=lambda gr: gr[1] - gr[0])[1]
        return dark[max(0, y - 12):y + 13, end + 1:min(W, end + 220)].sum() > 8
    tagged = [c for c in centres if labelled(c)]
    centres = _evenly_spaced(tagged if len(tagged) >= 2 else centres)
    if len(centres) < 2:
        raise ChartNotFound("Couldn't find the chart's gridlines")
    ys = [int(round(c)) for c in centres]
    hits = grey[ys].sum(axis=0)
    cols = np.flatnonzero(hits >= max(1, len(ys) // 2))   # where most gridlines run (not page dividers)
    return centres, int(cols.min()), int(cols.max())


def _evenly_spaced(centres: list[float], tol: float = 2.5) -> list[float]:
    """Chart gridlines are evenly spaced; page dividers and borders aren't. Keep the longest evenly spaced run."""
    best = centres[:1]
    for i in range(len(centres)):
        for j in range(i + 1, len(centres)):
            step = centres[j] - centres[i]
            if step < 8:
                continue
            run = [centres[i], centres[j]]
            for c in centres[j + 1:]:
                if abs(c - run[-1] - step) <= tol:
                    run.append(c)
            if len(run) > len(best):
                best = run
    return best


def read_curve(image: bytes) -> pd.DataFrame:
    """Pixel-level curve: columns x (0..1 across the curve) and y_frac (0 = top gridline, 1 = bottom)."""
    from PIL import Image
    a = np.asarray(Image.open(io.BytesIO(image)).convert("RGB")).astype(int)
    centres, x_min, x_max = _gridlines(a)
    top, bottom = centres[0], centres[-1]
    band = a[int(top) - 4:int(bottom) + 5, x_min:x_max + 1]
    r, g, b = band[..., 0], band[..., 1], band[..., 2]
    spread = band.max(axis=2) - band.min(axis=2)
    line = ((r + g + b) < 360) | ((spread > 60) & ((r + g + b) < 600))      # dark or clearly coloured
    # The value line is one continuous stroke; axis labels and side text are separate clusters. Keep the
    # longest run of consecutive columns that contain line pixels (gaps of a few px allowed).
    present = np.flatnonzero(line.any(axis=0))
    if len(present) < 20:
        raise ChartNotFound("Couldn't find a value line between the gridlines")
    runs, start = [], present[0]
    for p0, p1 in zip(present, present[1:]):
        if p1 - p0 > 4:
            runs.append((start, p0))
            start = p1
    runs.append((start, present[-1]))
    lo, hi = max(runs, key=lambda r: r[1] - r[0])
    xs, ys = [], []
    for x in range(lo, hi + 1):
        hit = np.flatnonzero(line[:, x])
        if len(hit):
            xs.append(x)
            ys.append(np.median(hit) + int(top) - 4)
    xs = np.array(xs, dtype=float)
    return pd.DataFrame({"x": (xs - xs[0]) / (xs[-1] - xs[0]),
                         "y_frac": (np.array(ys) - top) / (bottom - top)})


def history_from_chart(image: bytes, top_value: float, bottom_value: float, start: pd.Timestamp,
                       end: pd.Timestamp, exact_end_value: float | None = None) -> pd.DataFrame:
    """Month-end values (date, value) read from the chart. With exact_end_value, the whole curve is scaled
    so its last point matches it (the chart's line is drawn slightly smoothed)."""
    c = read_curve(image)
    values = top_value + c["y_frac"] * (bottom_value - top_value)
    dates = start + (end - start) * c["x"]
    s = pd.Series(values.to_numpy(), index=pd.DatetimeIndex(dates))
    monthly = s.resample("ME").mean().dropna()
    monthly.index = [min(d, end) for d in monthly.index]
    read_end = float(s.iloc[-max(3, len(s) // 200):].mean())
    if exact_end_value:
        monthly = monthly * (exact_end_value / read_end)
        monthly.iloc[-1] = exact_end_value
    out = pd.DataFrame({"date": [d.date() for d in monthly.index], "value": monthly.round(0).to_numpy()})
    out.attrs["read_end"] = read_end
    return out
