"""Plotly figures with one consistent look. Colors follow the validated
reference palette (light + dark steps); assets are always slot 1 (blue) and
debts slot 2 (orange), so identity never changes between charts.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

PALETTE = {
    "light": dict(s1="#2a78d6", s2="#eb6834", ink="#0b0b0b", ink2="#52514e", muted="#898781",
                  grid="#e1e0d9", axis="#c3c2b7", band="rgba(42,120,214,0.16)", surface="#fcfcfb"),
    "dark": dict(s1="#3987e5", s2="#d95926", ink="#ffffff", ink2="#c3c2b7", muted="#898781",
                 grid="#2c2c2a", axis="#383835", band="rgba(57,135,229,0.22)", surface="#1a1a19"),
}
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def money(v: float, short: bool = True) -> str:
    if v is None or pd.isna(v):
        return "-"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if short and v >= 1e6:
        return f"{sign}${v / 1e6:.2f}M"
    if short and v >= 1e4:
        return f"{sign}${v / 1e3:.0f}K"
    if short and v >= 1e3:
        return f"{sign}${v / 1e3:.1f}K"
    return f"{sign}${v:,.0f}"


def _layout(fig: go.Figure, c: dict, height: int = 320, legend: bool = False,
            compact: bool = False) -> go.Figure:
    """compact = phone: shorter, fewer ticks, and axes locked so a swipe scrolls the page
    instead of panning the chart. Desktop output is unchanged."""
    fig.update_layout(
        height=int(height * 0.72) if compact else height,
        margin=dict(l=44, r=8, t=8, b=32) if compact else dict(l=72, r=24, t=16, b=48),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=c["ink2"], size=12 if compact else 13),
        hoverlabel=dict(font=dict(family=FONT, size=13), bgcolor=c["surface"], bordercolor=c["axis"],
                        font_color=c["ink"]),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(color=c["ink2"])),
        barcornerradius=4, bargap=0.35,
    )
    fig.update_xaxes(showgrid=False, linecolor=c["axis"], tickfont=dict(color=c["muted"]), zeroline=False, automargin=True)
    fig.update_yaxes(gridcolor=c["grid"], gridwidth=1, tickfont=dict(color=c["muted"]), zeroline=False,
                     tickprefix="$", tickformat="~s", automargin=True)
    if compact:
        fig.update_layout(dragmode=False)
        fig.update_xaxes(nticks=4, fixedrange=True)
        fig.update_yaxes(nticks=5, fixedrange=True)
    return fig


def net_worth_history(nw: pd.DataFrame, mode: str, compact: bool = False) -> go.Figure:
    c = PALETTE[mode]
    fig = go.Figure(go.Scatter(
        x=nw["date"], y=nw["net_worth"], mode="lines", line=dict(color=c["s1"], width=2),
        customdata=nw[["assets", "liabilities"]].values,
        hovertemplate="<b>%{x|%b %Y}</b><br>Net worth $%{y:,.0f}"
                      "<br>Own $%{customdata[0]:,.0f} · Owe $%{customdata[1]:,.0f}<extra></extra>",
    ))
    if "added" in nw and (steps := nw[nw["added"].astype(bool)]).shape[0]:
        # an account's history starts here: mark it so the step isn't mistaken for a gain
        fig.add_trace(go.Scatter(
            x=steps["date"], y=steps["net_worth"], mode="markers", showlegend=False,
            marker=dict(symbol="diamond", size=9, color=c["ink2"], line=dict(width=2, color=c["surface"])),
            customdata=steps[["added"]].values,
            hovertemplate="<b>%{x|%b %Y}</b><br>Added: %{customdata[0]}<extra></extra>"))
    fig.update_layout(hovermode="x")
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1, spikecolor=c["axis"], spikedash="solid")
    return _layout(fig, c, compact=compact)


def breakdown_bars(items: dict[str, float], mode: str, debt: bool = False, compact: bool = False) -> go.Figure:
    c = PALETTE[mode]
    items = {k: v for k, v in items.items() if v}
    labels, values = list(items)[::-1], list(items.values())[::-1]
    fig = go.Figure(go.Bar(
        y=labels, x=values, orientation="h", marker=dict(color=c["s2"] if debt else c["s1"]),
        text=[money(v) for v in values], textposition="outside", cliponaxis=False,
        textfont=dict(color=c["ink2"]),
        hovertemplate="<b>%{y}</b><br>$%{x:,.0f}<extra></extra>",
    ))
    fig = _layout(fig, c, height=max(160, 56 * len(labels) + 40), compact=compact)
    fig.update_layout(margin=dict(l=8, r=8, t=4, b=4) if compact else dict(l=16, r=16, t=8, b=8))
    fig.update_xaxes(visible=False, range=[0, max(values or [1]) * 1.25])
    fig.update_yaxes(showgrid=False, tickprefix="", tickfont=dict(color=c["ink2"]))
    return fig


def forecast_fan(history: pd.DataFrame, bands: pd.DataFrame, real: bool, mode: str,
                 compact: bool = False) -> go.Figure:
    c = PALETTE[mode]
    sfx = "_real" if real else ""
    lo, mid, hi = bands[f"p10{sfx}"], bands[f"p50{sfx}"], bands[f"p90{sfx}"]
    fig = go.Figure()
    if len(history):
        fig.add_trace(go.Scatter(
            x=history["date"], y=history["net_worth"], mode="lines", name="So far",
            line=dict(color=c["ink2"], width=2),
            hovertemplate="<b>%{x|%b %Y}</b><br>Actual $%{y:,.0f}<extra></extra>",
        ))
    fig.add_trace(go.Scatter(x=bands["date"], y=hi, mode="lines", line=dict(width=0), hoverinfo="skip",
                             showlegend=False))
    fig.add_trace(go.Scatter(x=bands["date"], y=lo, mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor=c["band"], name="Likely range (80%)", hoverinfo="skip"))
    fig.add_trace(go.Scatter(
        x=bands["date"], y=mid, mode="lines", name="Most likely", line=dict(color=c["s1"], width=2),
        customdata=pd.concat([lo, hi], axis=1).values,
        hovertemplate="<b>%{x|%b %Y}</b><br>Most likely $%{y:,.0f}"
                      "<br>Range $%{customdata[0]:,.0f} – $%{customdata[1]:,.0f}<extra></extra>",
    ))
    fig.update_layout(hovermode="x")
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1, spikecolor=c["axis"], spikedash="solid")
    return _layout(fig, c, height=380, legend=True, compact=compact)


def cash_flow_bars(cf: pd.DataFrame, mode: str, compact: bool = False) -> go.Figure:
    c = PALETTE[mode]
    fig = go.Figure()
    common = dict(x=cf["month"], customdata=cf[["saved"]].values)
    fig.add_trace(go.Bar(**common, y=cf["money_in"], name="Money in", marker_color=c["s1"],
                         hovertemplate="<b>%{x|%b %Y}</b><br>In $%{y:,.0f}<br>Left over $%{customdata[0]:,.0f}<extra></extra>"))
    fig.add_trace(go.Bar(**common, y=cf["money_out"], name="Money out", marker_color=c["s2"],
                         hovertemplate="<b>%{x|%b %Y}</b><br>Out $%{y:,.0f}<br>Left over $%{customdata[0]:,.0f}<extra></extra>"))
    fig.update_layout(barmode="group", bargroupgap=0.08)
    return _layout(fig, c, legend=True, compact=compact)


def category_bars(df: pd.DataFrame, mode: str, compact: bool = False) -> go.Figure:
    return breakdown_bars(dict(zip(df["category"], df["monthly"])), mode, debt=True, compact=compact)
