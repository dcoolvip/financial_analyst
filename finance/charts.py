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


def net_worth_history(nw: pd.DataFrame, mode: str, compact: bool = False, own_owe: bool = False) -> go.Figure:
    """Net worth over time; own_owe=True instead draws what you own and what you owe as two lines."""
    c = PALETTE[mode]
    if own_owe:
        fig = go.Figure()
        for col, name, color in (("assets", "What you own", c["s1"]), ("liabilities", "What you owe", c["s2"])):
            fig.add_trace(go.Scatter(x=nw["date"], y=nw[col], mode="lines", name=name,
                                     line=dict(color=color, width=2),
                                     hovertemplate=f"{name} $%{{y:,.0f}}<extra></extra>"))
        fig.update_layout(hovermode="x unified")
        return _layout(fig, c, legend=True, compact=compact)
    added = (nw["added"].fillna("") if "added" in nw else pd.Series("", index=nw.index)).astype(str)
    note = added.map(lambda a: f"<br>Added: {a}" if a else "")
    fig = go.Figure(go.Scatter(
        x=nw["date"], y=nw["net_worth"], mode="lines", line=dict(color=c["s1"], width=2),
        customdata=pd.concat([nw[["assets", "liabilities"]], note.rename("note")], axis=1).values,
        hovertemplate="<b>%{x|%b %Y}</b><br>Net worth $%{y:,.0f}"
                      "<br>Own $%{customdata[0]:,.0f} · Owe $%{customdata[1]:,.0f}%{customdata[2]}<extra></extra>",
    ))
    if (steps := nw[added != ""]).shape[0]:
        # an account's history starts here: mark it so the step isn't mistaken for a gain. The line's own
        # tooltip names it - the marker has none, so hovering never shows two boxes.
        fig.add_trace(go.Scatter(
            x=steps["date"], y=steps["net_worth"], mode="markers", showlegend=False, hoverinfo="skip",
            marker=dict(symbol="diamond", size=9, color=c["ink2"], line=dict(width=2, color=c["surface"]))))
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
    fig.update_yaxes(showgrid=False, tickprefix="", tickfont=dict(color=c["ink2"]),
                     tickmode="array", tickvals=labels, ticktext=labels, nticks=0)   # a label on every bar
    return fig


def asset_outlook(history: pd.DataFrame, projection: pd.Series, mode: str, compact: bool = False) -> go.Figure:
    """One asset: its recorded values (solid) and where its growth rate takes it (dashed)."""
    c = PALETTE[mode]
    fig = go.Figure()
    if len(history):
        fig.add_trace(go.Scatter(x=history["date"], y=history["balance"], mode="lines+markers", name="Recorded",
                                 line=dict(color=c["ink2"], width=2), marker=dict(size=5),
                                 hovertemplate="<b>%{x|%b %Y}</b><br>$%{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=projection.index, y=projection.values, mode="lines", name="Outlook",
                             line=dict(color=c["s1"], width=2, dash="dash"),
                             hovertemplate="<b>%{x|%b %Y}</b><br>$%{y:,.0f}<extra></extra>"))
    fig.update_layout(hovermode="x")
    return _layout(fig, c, height=300, legend=True, compact=compact)


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
    """Money in and out per month. With in_why / out_why columns (insights.month_over_month), hovering a bar
    also says what changed from the month before."""
    c = PALETTE[mode]
    fig = go.Figure()
    in_why = cf["in_why"].fillna("") if "in_why" in cf else pd.Series("", index=cf.index)
    out_why = cf["out_why"].fillna("") if "out_why" in cf else pd.Series("", index=cf.index)
    small = lambda s: s.map(lambda w: f"<br><span style='font-size:12px'>{w}</span>" if w else "")   # noqa: E731
    fig.add_trace(go.Bar(x=cf["month"], y=cf["money_in"], name="Money in", marker_color=c["s1"],
                         customdata=pd.concat([cf["saved"], small(in_why)], axis=1).values,
                         hovertemplate="<b>%{x|%b %Y}</b><br>In $%{y:,.0f}<br>Left over $%{customdata[0]:,.0f}"
                                       "%{customdata[1]}<extra></extra>"))
    fig.add_trace(go.Bar(x=cf["month"], y=cf["money_out"], name="Money out", marker_color=c["s2"],
                         customdata=pd.concat([cf["saved"], small(out_why)], axis=1).values,
                         hovertemplate="<b>%{x|%b %Y}</b><br>Out $%{y:,.0f}<br>Left over $%{customdata[0]:,.0f}"
                                       "%{customdata[1]}<extra></extra>"))
    fig.update_layout(barmode="group", bargroupgap=0.08)
    return _layout(fig, c, legend=True, compact=compact)


def category_bars(df: pd.DataFrame, mode: str, compact: bool = False, about: dict | None = None) -> go.Figure:
    """Monthly spend per category; about = {category: what it covers}, shown when you hover a bar."""
    fig = breakdown_bars(dict(zip(df["category"], df["monthly"])), mode, debt=True, compact=compact)
    if about:
        import textwrap
        bar = fig.data[0]
        bar.customdata = ["<br>".join(textwrap.wrap(about.get(c, ""), 48)) for c in bar.y]
        bar.hovertemplate = "<b>%{y}</b> · $%{x:,.0f} a month<br><span style='font-size:12px'>%{customdata}</span><extra></extra>"
    return fig


def account_history(hist: pd.DataFrame, mode: str, debt: bool = False, compact: bool = False) -> go.Figure:
    """One account's recorded values over time. hist: date, balance, how (source in words), estimate (bool).
    Values you have on record get a dot; estimates in between are just the line."""
    c = PALETTE[mode]
    color = c["s2"] if debt else c["s1"]
    word = "Owed" if debt else "Value"
    fig = go.Figure(go.Scatter(
        x=hist["date"], y=hist["balance"], mode="lines", name="Estimated" if hist["estimate"].any() else word,
        line=dict(color=color, width=2), customdata=hist[["how"]].values,
        hovertemplate=f"<b>%{{x|%b %d, %Y}}</b><br>{word} $%{{y:,.0f}}<br>%{{customdata[0]}}<extra></extra>",
        showlegend=bool(hist["estimate"].any())))
    real = hist[~hist["estimate"]]
    if len(real):
        fig.add_trace(go.Scatter(
            x=real["date"], y=real["balance"], mode="markers", name="Recorded", hoverinfo="skip",
            showlegend=bool(hist["estimate"].any()),
            marker=dict(size=8, color=color, line=dict(width=2, color=c["surface"]))))
    fig.update_layout(hovermode="x")
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1, spikecolor=c["axis"], spikedash="solid")
    return _layout(fig, c, height=280, legend=bool(hist["estimate"].any()), compact=compact)


SCENARIO_STYLE = {   # band color, legend name - best / middle / worse for the one company's stock
    "economy": ("s1", "Apple grows with the economy"),
    "ibm": ("muted", "Drop and recovery (like IBM)"),
    "gm": ("s2", "Slow decline to $0 (like GM)"),
}


def forecast_scenarios(history: pd.DataFrame, scenarios: dict, blend: pd.DataFrame, real: bool, mode: str,
                       compact: bool = False) -> go.Figure:
    """Net worth ahead under each scenario for the one company's stock - a band per scenario (its middle half
    of outcomes) - and the dark line: the middle of all simulations with the scenarios mixed by weight."""
    c = PALETTE[mode]
    sfx = "_real" if real else ""
    alpha = {"light": 0.18, "dark": 0.26}[mode]

    def rgba(hex_, a):
        h = hex_.lstrip("#")
        return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"
    fig = go.Figure()
    if len(history):
        fig.add_trace(go.Scatter(x=history["date"], y=history["net_worth"], mode="lines", name="So far",
                                 line=dict(color=c["ink2"], width=2),
                                 hovertemplate="<b>%{x|%b %Y}</b><br>Actual $%{y:,.0f}<extra></extra>"))
    for kind, b in scenarios.items():
        key, name = SCENARIO_STYLE.get(kind, ("muted", kind))
        fig.add_trace(go.Scatter(x=b["date"], y=b[f"p75{sfx}"], mode="lines", line=dict(width=0), showlegend=False,
                                 hoverinfo="skip", legendgroup=kind))
        fig.add_trace(go.Scatter(x=b["date"], y=b[f"p25{sfx}"], mode="lines", line=dict(width=1, color=rgba(c[key], 0.5)),
                                 fill="tonexty", fillcolor=rgba(c[key], alpha), name=name, legendgroup=kind,
                                 customdata=b[[f"p50{sfx}"]].values,
                                 hovertemplate=f"{name}: typically $%{{customdata[0]:,.0f}}<extra></extra>"))
    fig.add_trace(go.Scatter(x=blend["date"], y=blend[f"p50{sfx}"], mode="lines", name="Weighted blend (most likely)",
                             line=dict(color=c["s1"], width=3),
                             hovertemplate="<b>%{x|%b %Y}</b><br>Most likely (blend) $%{y:,.0f}<extra></extra>"))
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1, spikecolor=c["axis"], spikedash="solid")
    return _layout(fig, c, height=400, legend=True, compact=compact)


def forecast_own_owe(history: pd.DataFrame, expected: pd.DataFrame, real: bool, mode: str,
                     compact: bool = False) -> go.Figure:
    """What you own and what you owe: recorded so far (solid) and the most likely path ahead (dashed)."""
    c = PALETTE[mode]
    sfx = "_real" if real else ""
    own = expected[f"cash{sfx}"] + expected[f"investments{sfx}"] + expected[f"property{sfx}"]
    fig = go.Figure()
    for name, past, ahead, color in (("What you own", "assets", own, c["s1"]),
                                     ("What you owe", "liabilities", expected[f"debt{sfx}"], c["s2"])):
        if len(history):
            fig.add_trace(go.Scatter(x=history["date"], y=history[past], mode="lines", name=name,
                                     line=dict(color=color, width=2), legendgroup=name,
                                     hovertemplate=f"{name} $%{{y:,.0f}}<extra></extra>"))
        fig.add_trace(go.Scatter(x=expected["date"], y=ahead, mode="lines", name=f"{name} (ahead)",
                                 line=dict(color=color, width=2, dash="dash"), legendgroup=name, showlegend=False,
                                 hovertemplate=f"{name}, most likely $%{{y:,.0f}}<extra></extra>"))
    fig.update_layout(hovermode="x unified")
    return _layout(fig, c, height=380, legend=True, compact=compact)


def cash_flow_ahead(cf: pd.DataFrame, real: bool, mode: str, compact: bool = False,
                    events: list | None = None) -> go.Figure:
    """Each coming year: living costs, loan payments, healthcare and tax on 401(k) withdrawals (stacked)
    against money in (pay + Social Security + rent/dividends + 401(k) withdrawals). events: [(year, label)]
    drawn as dotted lines, labels written along them (one per year, so they never pile up)."""
    c = PALETTE[mode]
    sfx = "_real" if real else ""
    span = [f"{a:%b %Y} – {b:%b %Y}" for a, b in zip(cf["first"], cf["last"])]
    col = lambda name: cf[f"{name}{sfx}"] if f"{name}{sfx}" in cf else pd.Series(0.0, index=cf.index)   # noqa: E731
    fig = go.Figure()
    taxes = col("tax_401k") + col("cg_tax")               # 401(k) withdrawals + capital gains on shares sold
    for label, values, color in (("Living costs", col("living"), c["s2"]), ("Loan payments", col("loans"), c["muted"]),
                                 ("Healthcare", col("health"), c["ink2"]), ("College", col("college"), c["ink"]),
                                 ("Taxes on selling & 401(k) withdrawals", taxes, c["axis"])):
        if values.abs().sum() > 0:
            fig.add_trace(go.Bar(x=cf["year"], y=values, name=label, marker=dict(color=color),
                                 hovertemplate=f"{label} $%{{y:,.0f}}<extra></extra>"))
    money_in = col("income") + col("out_401k")
    parts = pd.concat([pd.Series(span, index=cf.index), col("pay"), col("ss"), col("other"), col("out_401k")], axis=1).values
    has_parts = col("pay").abs().sum() + col("ss").abs().sum() > 0
    fig.add_trace(go.Scatter(x=cf["year"], y=money_in, name="Money in", mode="lines+markers",
                             line=dict(color=c["s1"], width=2), marker=dict(size=6 if len(cf) > 20 else 8),
                             customdata=parts,
                             hovertemplate="<b>%{customdata[0]}</b><br>Money in $%{y:,.0f}"
                                           + ("<br>  pay $%{customdata[1]:,.0f} · Social Security $%{customdata[2]:,.0f}"
                                              "<br>  rent & dividends $%{customdata[3]:,.0f} · 401(k) withdrawals "
                                              "$%{customdata[4]:,.0f}" if has_parts else "")
                                           + "<extra></extra>"))
    by_year: dict = {}
    for year, label in events or []:
        by_year.setdefault(year, []).append(label)
    for year, labels in sorted(by_year.items()):
        fig.add_vline(x=year, line=dict(color=c["axis"], width=1, dash="dot"))
        fig.add_annotation(x=year, y=0.98, yref="paper", xanchor="left", yanchor="top", textangle=-90,
                           text=" · ".join(labels), showarrow=False, font=dict(size=10, color=c["ink2"]),
                           bgcolor=c["surface"], opacity=0.9, xshift=2)
    fig.update_layout(barmode="stack", hovermode="x unified", bargap=0.3)
    fig.update_xaxes(dtick=5 if len(cf) > 20 else 1, tickformat="d")
    fig = _layout(fig, c, height=420 if events else 360, legend=True, compact=compact)
    fig.update_layout(legend=dict(orientation="h", yanchor="top", y=-0.12, x=0))      # below, out of the way
    return fig


def change_bars(items: dict[str, float], mode: str, compact: bool = False) -> go.Figure:
    """Signed changes as horizontal bars: gains in the asset color, losses in the debt color."""
    c = PALETTE[mode]
    labels, values = list(items)[::-1], list(items.values())[::-1]
    fig = go.Figure(go.Bar(
        y=labels, x=values, orientation="h", marker=dict(color=[c["s1"] if v >= 0 else c["s2"] for v in values]),
        text=[("+" if v >= 0 else "") + money(v) for v in values], textposition="outside", cliponaxis=False,
        textfont=dict(color=c["ink2"]),
        hovertemplate="<b>%{y}</b><br>%{x:+$,.0f}<extra></extra>"))
    fig = _layout(fig, c, height=max(160, 56 * len(labels) + 40), compact=compact)
    fig.update_layout(margin=dict(l=8, r=8, t=4, b=4) if compact else dict(l=16, r=16, t=8, b=8))
    lo, hi = min([0.0, *values]), max([0.0, *values])
    pad = (hi - lo) * 0.35 or 1.0                       # room for the outside labels
    fig.update_xaxes(visible=False, range=[lo - (pad if lo < 0 else 0), hi + (pad if hi > 0 else 0)])
    fig.update_yaxes(showgrid=False, tickprefix="", tickfont=dict(color=c["ink2"]),
                     tickmode="array", tickvals=labels, ticktext=labels, nticks=0)
    return fig
