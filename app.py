"""Personal finance dashboard.  Run:  streamlit run app.py"""
from __future__ import annotations

import importlib
import os
import re
from datetime import date, datetime

import numpy as np
import pandas as pd
import streamlit as st

import finance.importers.base
import finance.importers.bofa
import finance.importers.amex
import finance.importers.apple_card
import finance.importers.barclays
import finance.importers.chase
import finance.importers.wealthfront
import finance.importers.statements
from finance import assets, categorize, charts, checkpoints, db, demo, editing, pokemon, forecast, importers, insights, paths, portfolio


@st.cache_resource
def _loaded_mtimes() -> dict:
    return {}


def _reload_changed_modules() -> None:
    """Streamlit reruns app.py on refresh but can keep stale copies of finance/* in memory
    (its polling watcher misses some edits). Reload any module whose file changed since it
    was loaded, dependencies first, so a refresh always runs current code."""
    order = [paths, categorize, db, finance.importers.base, finance.importers.amex, finance.importers.apple_card, finance.importers.barclays, finance.importers.bofa, finance.importers.chase, finance.importers.wealthfront,
             finance.importers.statements,
             importers,
             insights, forecast, charts, demo, portfolio, editing, checkpoints, assets, pokemon]
    seen = _loaded_mtimes()
    first_run = not seen
    stale = [m for m in order if seen.get(m.__name__) != os.path.getmtime(m.__file__)]
    if first_run or stale:
        for m in order:  # reload everything downstream too, so `from x import y` bindings refresh
            importlib.reload(m)
            seen[m.__name__] = os.path.getmtime(m.__file__)


_reload_changed_modules()
from finance.charts import money  # noqa: E402 - after the reload so it binds the fresh module
from finance.importers import (KIND_ACCOUNT_TYPES, UnrecognizedFile, apply, apply_statement,  # noqa: E402
                               last4_from_filename, parse_file, statements, suggest_csv_account)

st.set_page_config(page_title="Financial Analyst", page_icon="📈", layout="wide")

# Wi-Fi mode: sign-in, passkeys and HTTPS are handled by the gate (finance/gate.py) in front of
# this app. Streamlit itself must then only be reachable from this Mac, never the network.
GATED = os.environ.get("FINANCE_GATE") == "1"
if GATED and st.get_option("server.address") not in ("127.0.0.1", "localhost"):
    st.error("Behind the gate, Streamlit must listen on 127.0.0.1 only. Start with “Dashboard (Wi-Fi).command”.")
    st.stop()


def _is_phone() -> bool:
    """Phone vs. everything else, from the browser's User-Agent. iPads get the full layout.
    Override for testing with ?view=phone or ?view=desktop."""
    forced = st.query_params.get("view")
    if forced in ("phone", "desktop"):
        return forced == "phone"
    ua = (st.context.headers or {}).get("User-Agent", "")
    return bool(re.search(r"iPhone|iPod|Android.+Mobile|Windows Phone", ua))


PHONE = _is_phone()


def _device() -> str:
    ua = (st.context.headers or {}).get("User-Agent", "")
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                         ("Macintosh", "Mac"), ("Windows", "Windows")):
        if needle in ua:
            return f"{name} · " + next((b for b in ("Edg", "Chrome", "Firefox", "Safari") if b in ua), "browser")
    return "this Mac"

# Phone rules are applied directly when the phone is detected (reliable regardless of the
# browser's reported width), and via a small-screen media query as a fallback. The Mac never
# sees them.
PHONE_CSS = """
    [data-testid="stHeader"] { height: 2.25rem; min-height: 2.25rem; }
    .block-container, [data-testid="stMainBlockContainer"] { padding: .75rem .75rem 3rem !important; }
    .hero { font-size: 2rem; }
    .hero-sub { font-size: .9rem; margin-bottom: .6rem; }
    h4 { font-size: 1.05rem !important; padding-top: .6rem !important; }
    [data-testid="stMetricValue"] { font-size: 1.15rem; }
    [data-testid="stMetricLabel"] p { font-size: .75rem; }
    [data-testid="stMarkdownContainer"] p { font-size: .95rem; }
    /* metric rows become a 2-column grid instead of a tall single column */
    [class*="st-key-kpis"] [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: .5rem !important; }
    [class*="st-key-kpis"] [data-testid="stColumn"] {
      flex: 1 1 calc(50% - .5rem) !important; min-width: calc(50% - .5rem) !important;
      width: calc(50% - .5rem) !important; max-width: calc(50% - .25rem) !important;
    }
    [class*="st-key-kpis"] [data-testid="stVerticalBlockBorderWrapper"] { padding: .5rem .65rem !important; }
    /* tabs: tighter, swipeable */
    [data-baseweb="tab-list"] { gap: .25rem; overflow-x: auto; }
    [data-baseweb="tab"] { padding: .4rem .55rem; font-size: .9rem; }
"""
st.markdown(f"""
<style>
  .block-container {{ padding-top: 2rem; max-width: 1200px; }}
  [data-testid="stMetricValue"] {{ font-size: 1.6rem; }}
  .hero {{ font-size: 3rem; font-weight: 700; line-height: 1.1; margin: 0; }}
  .hero-sub {{ color: var(--text-color); opacity: .65; margin: .25rem 0 1rem; }}
  {PHONE_CSS if PHONE else "@media (max-width: 640px) {" + PHONE_CSS + "}"}
</style>
""", unsafe_allow_html=True)

TYPE_LABELS = {
    "checking": "Checking", "savings": "Savings", "brokerage": "Brokerage", "retirement": "Retirement (401k/IRA)",
    "property": "Home / real estate", "vehicle": "Vehicle", "collectible": "Collectibles (cards, art…)",
    "precious_metal": "Gold / precious metals", "other_asset": "Other asset",
    "credit_card": "Credit card", "mortgage": "Mortgage", "auto_loan": "Auto loan", "heloc": "HELOC",
    "personal_loan": "Personal loan", "student_loan": "Student loan", "other_liability": "Other debt",
}
KIND_LABELS = {"deposit": "Checking / savings statement", "credit_card": "Credit card activity",
               "holdings": "Investment holdings"}


# --- data source --------------------------------------------------------------

@st.cache_resource
def get_conn(path: str):
    conn = db.connect(path)
    if path.endswith("demo.db") and conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
        demo.seed(conn)
    return conn


with st.sidebar:
    st.markdown("### Financial Analyst")
    source = st.radio("Data", ["My data", "Demo data"], help="Demo data lives in its own file and never mixes with yours.")
    if GATED:
        # target=_self: stay in this tab (gate pages, not Streamlit pages)
        st.markdown('<a href="/security" target="_self">🔐 Security &amp; passkeys</a> &nbsp;·&nbsp; '
                    '<a href="/logout" target="_self">Sign out</a>', unsafe_allow_html=True)

DB_PATH = paths.data_dir() / ("demo.db" if source == "Demo data" else "finance.db")
conn = get_conn(str(DB_PATH))


def _data_version() -> int:
    """Changes whenever anything writes to the database - from this page or another device."""
    try:
        return os.stat(DB_PATH).st_mtime_ns
    except FileNotFoundError:
        return 0


# Other open pages (phone, PC, another tab) redraw on their own when data changes elsewhere: a tiny
# fragment checks every few seconds and reruns the page only if the database file changed.
st.session_state["data_version"] = _data_version()


@st.fragment(run_every="4s")
def _watch_for_changes() -> None:
    if _data_version() != st.session_state.get("data_version"):
        st.rerun(scope="app")


with st.sidebar:
    _watch_for_changes()
mode = "dark" if getattr(st.context, "theme", None) and st.context.theme.type == "dark" else "light"


def plot(fig):
    st.plotly_chart(fig, width="stretch", theme=None,
                    config={"displayModeBar": False, "scrollZoom": False, "responsive": True})


def signed(v) -> str:
    return "-" if v is None or pd.isna(v) else ("+" if v >= 0 else "") + money(v)


def kpi_row(items: list[tuple[str, str]]) -> None:
    """A row of bordered metric cards: one row on desktop, a 2-column grid on phones."""
    with st.container(key=f"kpis_{items[0][0]}".replace(" ", "_")):
        for col, (label, value) in zip(st.columns(len(items)), items):
            with col.container(border=True):
                st.metric(label, value)


accts = db.accounts(conn)
txns = db.transactions(conn)
categorize.match_loan_payments(conn, txns)          # payments at your loans' exact amount = that loan
rule_map = categorize.rules(conn)
nw = db.net_worth_series(conn)
totals = insights.group_totals(accts) if len(accts) else {}
est_savings = insights.estimate_monthly_savings(txns, rules=rule_map)


def pokemon_trend() -> dict | None:
    g = db.get_setting(conn, "pokemon_price_growth")
    return g if isinstance(g, dict) and g.get("growth") is not None else None


def trend_overrides() -> dict:
    """Same-cards price trend for the Pokemon collection (its raw value history includes cards bought)."""
    g = pokemon_trend()
    return ({g["account_id"]: (g["growth"], g["years"], g.get("vol"))}
            if g and g.get("account_id") in set(accts["id"]) else {})


def run_ai_categorize(recheck: bool = False) -> None:
    """Runs the AI and leaves the outcome in session state: the page reruns right after, which would
    wipe anything drawn here (an error must stay visible until it's read)."""
    bar = st.progress(0.0, text="Categorizing merchants…")
    try:
        n = categorize.auto_categorize(conn, db.transactions(conn), insights.is_transfer, recheck=recheck,
                                       progress=lambda f: bar.progress(f, text="Categorizing merchants…"))
        left = len(categorize.uncategorized_merchants(conn, db.transactions(conn), insights.is_transfer))
        st.session_state["ai_flash"] = (f"Categorized {n} merchants" if not left else
                                        f"Couldn't categorize {left} merchant(s) - the AI skipped them. Try again.")
    except Exception as e:  # noqa: BLE001 - surface any auth/network problem plainly
        st.session_state["ai_error"] = f"AI categorization didn't run: {e}"
    finally:
        bar.empty()

for key in ("flash", "ai_flash"):                 # confirmation from the previous action, on any tab
    if msg := st.session_state.pop(key, None):
        st.toast(msg, icon="⚠️" if msg.startswith("Couldn't") else "✅")

tab_overview, tab_future, tab_flow, tab_accounts, tab_add = st.tabs(
    ["Overview", "Future", "Money", "Accounts", "Add"] if PHONE else
    ["Overview", "Future", "Money in & out", "Accounts", "Add data"])


# --- overview -----------------------------------------------------------------

def change_since(months: int) -> float | None:
    """Like-for-like: accounts added since then don't count as a gain."""
    return db.net_worth_change(conn, months)[0]


with tab_overview:
    if accts.empty:
        st.subheader("Welcome 👋")
        st.write("Start by adding your accounts. Three quick steps:")
        st.markdown("1. **Add data** tab → drop in a Bank of America CSV (checking, savings, card, Merrill).\n"
                    "2. **Accounts** tab → enter balances for things without a CSV (mortgage, home value, car).\n"
                    "3. Come back here.  \n\nWant to look around first? Switch to **Demo data** in the sidebar.")
    else:
        net = float(nw["net_worth"].iloc[-1]) if len(nw) else 0.0
        own = sum(totals[g] for g in insights.ASSET_GROUPS)
        owe = sum(totals[g] for g in insights.DEBT_GROUPS)
        st.caption("NET WORTH")
        st.markdown(f'<p class="hero">{money(net, short=False)}</p>'
                    f'<p class="hero-sub">You own {money(own)} and owe {money(owe)}</p>', unsafe_allow_html=True)

        kpi_row([("Change, past month", signed(change_since(1))), ("Change, past year", signed(change_since(12))),
                 ("Cash", money(totals["Cash"])), ("Investments", money(totals["Investments"]))])

        # plain-language takeaways
        notes = []
        cf = insights.monthly_cash_flow(txns, rule_map)
        this_month = pd.Timestamp.today().to_period("M").to_timestamp()
        recent = cf[cf["month"] < this_month].tail(6)
        if len(recent):
            income, spend = recent["money_in"].mean(), recent["money_out"].mean()
            rate = (income - spend) / income if income else 0
            notes.append(f"💰 You keep about **{money(income - spend)} a month** "
                         f"({rate:.0%} of what comes in), based on the last {len(recent)} months.")
            if spend > 0:
                notes.append(f"🛟 Your cash would cover **{totals['Cash'] / spend:.1f} months** of spending "
                             f"(3–6 is the usual comfort zone).")
        yr, base, left_out = db.net_worth_change(conn, 12)
        if yr is not None and base:
            notes.append(f"📈 Net worth is {'up' if yr >= 0 else 'down'} **{money(abs(yr))}** over the past year "
                         f"({yr / abs(base):+.0%})"
                         + (f", not counting {', '.join(left_out)} (no history that far back yet)." if left_out else "."))
        if own:
            notes.append(f"🏦 Debt is **{owe / own:.0%}** of what you own.")
        missing = accts[accts["balance"].isna()]
        if len(missing):
            notes.append(f"💳 **{len(missing)} account(s) have no balance yet:** " + ", ".join(missing["name"])
                         + ". Card downloads don't include one - type the balance from the bank's app into the "
                         "**Accounts** table, or import a statement PDF.")
        stale = insights.stale_accounts(accts[accts["balance"].notna()])
        if len(stale):
            notes.append(f"⏰ {len(stale)} account(s) haven't been updated in 45+ days: "
                         + ", ".join(stale["name"]) + ". Update them in **Accounts**.")
        with st.container(border=True):
            for n in notes:
                st.markdown(n)

        head, pick_range, pick_view = st.columns([2, 1.3, 1.3]) if not PHONE else (st, st, st)
        head.markdown("#### Net worth over time")
        span = pick_range.segmented_control("Show", ["1Y", "3Y", "5Y", "All"], default="All", key="nw_range",
                                            label_visibility="collapsed") or "All"
        view = pick_view.segmented_control("View", ["Net worth", "Own & owe"], default="Net worth", key="nw_view",
                                           label_visibility="collapsed") or "Net worth"
        shown_nw = nw if span == "All" else nw[nw["date"] >= nw["date"].iloc[-1] - pd.DateOffset(years=int(span[0]))]
        plot(charts.net_worth_history(shown_nw, mode, compact=PHONE, own_owe=view == "Own & owe"))
        if view == "Net worth" and shown_nw["added"].astype(bool).any():
            st.caption("◆ marks where an account's history starts - hover it to see which. That step is the "
                       "account joining the chart, not a gain.")

        left, right = st.columns(2)
        with left:
            st.markdown("#### What you own")
            plot(charts.breakdown_bars({g: totals[g] for g in insights.ASSET_GROUPS}, mode, compact=PHONE))
        with right:
            st.markdown("#### What you owe")
            if owe:
                plot(charts.breakdown_bars({g: totals[g] for g in insights.DEBT_GROUPS}, mode, debt=True,
                                           compact=PHONE))
            else:
                st.success("Nothing. Debt-free! 🎉")

        moves = db.account_changes(conn, 12)
        if len(moves) and moves["change"].abs().sum() >= 1:
            st.markdown("#### What moved your net worth, past year")
            by_group = insights.change_by_group(moves, dict(zip(accts["id"], accts["type"])))
            left, right = st.columns([1, 1.2]) if not PHONE else (st.container(), st.container())
            with left:
                st.caption("By group. Paying down a loan or card counts as a gain.")
                plot(charts.change_bars({("Loans paid down" if g == "Loans" and v > 0 else
                                          "Card balances down" if g == "Credit cards" and v > 0 else g): v
                                         for g, v in by_group.items()}, mode, compact=PHONE))
            with right:
                st.caption("Biggest movers")
                top = moves.reindex(moves["change"].abs().sort_values(ascending=False).index).head(6)
                st.dataframe(top.assign(pct=np.where(top["then"].abs() > 0, top["change"] / top["then"].abs(), np.nan)),
                             hide_index=True, width="stretch", column_order=["name", "then", "now", "change", "pct"],
                             column_config={
                                 "name": "Account",
                                 "then": st.column_config.NumberColumn("A year ago", format="$%,.0f"),
                                 "now": st.column_config.NumberColumn("Now", format="$%,.0f"),
                                 "change": st.column_config.NumberColumn("Net worth effect", format="$%+,.0f"),
                                 "pct": st.column_config.NumberColumn("Change", format="percent")})
            if left_out := [n for n in accts.loc[accts["balance"].notna(), "name"] if n not in set(moves["name"])]:
                st.caption("Not counted (no value a year ago yet): " + ", ".join(left_out) + ".")

        holdings = db.latest_holdings(conn)
        if len(holdings):
            pf = portfolio.summarize(holdings)
            st.markdown("#### Investments")
            grants = db.latest_grants(conn)
            items = [("Invested", money(pf["total"]))]
            if pf["gain"] is not None:
                items.append(("Unrealized gain", ("+" if pf["gain"] >= 0 else "") + money(pf["gain"])))
            if len(grants):
                items.append(("Unvested RSUs", money(grants["value"].sum())))
            kpi_row(items)
            if len(grants):
                st.caption(f"Unvested RSUs ({len(grants)} grants, pre-tax estimate) aren't counted in net worth "
                           "until they vest.")
            for sym, share in pf["concentrated"]:
                st.warning(f"**{sym}** is **{share:.0%}** of your investments. A single company that large "
                           "adds risk; many planners suggest keeping any one stock under 10%.", icon="⚠️")
            mix_col, top_col = st.columns([1, 1.4]) if not PHONE else (st.container(), st.container())
            with mix_col:
                st.caption(f"Mix of {money(pf['total'])} in positions")
                plot(charts.breakdown_bars({c: v for c, v in pf["by_class"].items() if v}, mode, compact=PHONE))
            with top_col:
                st.caption("Top holdings")
                top = pf["top"].assign(label=lambda d: d["symbol"] + " · " + d["description"].fillna("").str[:28])
                st.dataframe(top[["label", "value", "share"] if PHONE else ["label", "accounts", "value", "gain", "share"]],
                             hide_index=True, width="stretch", column_config={
                                 "label": "Holding", "accounts": "Account",
                                 "value": st.column_config.NumberColumn("Value", format="$%,.0f"),
                                 "gain": st.column_config.NumberColumn("Unrealized gain", format="$%,.0f"),
                                 "share": st.column_config.ProgressColumn("Share", format="percent",
                                                                          min_value=0, max_value=1)})


# --- future -------------------------------------------------------------------

with tab_future:
    if accts.empty:
        st.info("Add some accounts first. The forecast starts from your current balances.")
    else:
        saved = forecast.Assumptions.from_dict(db.get_setting(conn, "assumptions"))
        if db.get_setting(conn, "assumptions") is None:
            saved.monthly_savings = round(est_savings or 0, -1)

        if PHONE:  # chart first, sliders tucked under it, details below
            chart_col, ctrl_col, detail_col = st.container(), st.expander("⚙️ Adjust assumptions"), st.container()
        else:
            chart_col, ctrl_col = st.columns([2.3, 1], gap="large")
            detail_col = chart_col
        with ctrl_col:
            with st.container(border=not PHONE):
                if not PHONE:
                    st.markdown("**Your assumptions**")
                a = forecast.Assumptions(
                    years=st.slider("Years ahead", 5, 30, saved.years),
                    monthly_savings=st.number_input(
                        "Saved per month ($)", value=float(saved.monthly_savings), step=100.0,
                        help=f"What's left after all bills, loan payments included. "
                             f"Your recent average: {money(est_savings or 0)}."),
                    inflation=st.slider("Inflation", 0.0, 8.0, saved.inflation * 100, 0.25, format="%.2f%%") / 100,
                    investment_return=st.slider("Investment return (per year)", 0.0, 12.0,
                                                saved.investment_return * 100, 0.25, format="%.2f%%",
                                                help="Before inflation. ~7% is a common long-run estimate for a stock-heavy mix.") / 100,
                )
                with st.expander("More assumptions"):
                    a.invest_share = st.slider("Share of savings invested", 0, 100, int(saved.invest_share * 100), 5,
                                               format="%d%%", help="The rest stays in cash.") / 100
                    a.cash_yield = st.slider("Cash interest", 0.0, 6.0, saved.cash_yield * 100, 0.25, format="%.2f%%") / 100
                    a.home_appreciation = st.slider("Home value growth", -2.0, 8.0, saved.home_appreciation * 100, 0.25,
                                                    format="%.2f%%") / 100
                    a.savings_growth = st.slider("Savings grow each year by", 0.0, 8.0, saved.savings_growth * 100, 0.25,
                                                 format="%.2f%%", help="Raises. Matching inflation keeps savings steady in real terms.") / 100
                    a.investment_volatility = saved.investment_volatility
                    a.vehicle_depreciation = saved.vehicle_depreciation
                c1, c2 = st.columns(2)
                if c1.button("Save", width="stretch"):
                    db.set_setting(conn, "assumptions", a.to_dict())
                    st.toast("Assumptions saved")
                if c2.button("Reset", width="stretch"):
                    conn.execute("DELETE FROM settings WHERE key = 'assumptions'")
                    conn.commit()
                    st.rerun()
                real = st.toggle("Show in today's dollars", value=True,
                                 help="Removes inflation so future numbers feel like today's money.")

        fc = forecast.run(accts, a, history=db.balance_history(conn), trend_overrides=trend_overrides())
        sfx = "_real" if real else ""
        end = fc.bands.iloc[-1]
        with chart_col:
            st.caption(f"IN {a.years} YEARS ({end['date']:%Y})" + (" · TODAY'S DOLLARS" if real else ""))
            st.markdown(f'<p class="hero">{money(end[f"p50{sfx}"])}</p>'
                        f'<p class="hero-sub">Most likely outcome. 8 in 10 simulations land between '
                        f'{money(end[f"p10{sfx}"])} and {money(end[f"p90{sfx}"])}.</p>', unsafe_allow_html=True)
            plot(charts.forecast_fan(nw.tail(24), fc.bands, real, mode, compact=PHONE))

        with detail_col:
            exp = fc.expected.iloc[-1]
            kpi_row([("Investments", money(exp[f"investments{sfx}"])),
                     ("Cash + property", money(exp[f"cash{sfx}"] + exp[f"property{sfx}"])),
                     ("Debt left", money(exp[f"debt{sfx}"]))])

            if len(fc.valuables):
                st.markdown("#### Property & valuables")
                v = fc.valuables
                hist_all = db.balance_history(conn)
                shown = pd.DataFrame({
                    "Asset": v["name"], "Today": v["value"], "Growth / yr": v["rate"] * 100,
                    "Swings / yr": v["vol"] * 100, "Based on": v["why"],
                    f"In {a.years} years": v["end_real"] if real else v["end"]})
                st.dataframe(shown, hide_index=True, width="stretch", column_config={
                    "Today": st.column_config.NumberColumn(format="$%,.0f"),
                    "Growth / yr": st.column_config.NumberColumn(format="%+.1f%%"),
                    "Swings / yr": st.column_config.NumberColumn(
                        format="±%.0f%%", help="How much the value typically moves in a year - from its own history "
                                               "when there's enough, else typical for its kind. Widens the range."),
                    f"In {a.years} years": st.column_config.NumberColumn(
                        format="$%,.0f", help="In today's dollars" if real else "Future dollars")})
                st.caption("Each rate blends the asset's own history with the long-run rate for its kind - the "
                           "longer the history, the more it counts (1 yr ≈ 17%, 5 yrs = 50%, 10 yrs ≈ 67%). "
                           "Type your own in **Accounts → Your growth %** to override.")
                pick = st.selectbox("See an asset's history and outlook", list(v["name"]), key="asset_outlook")
                aid = int(v.loc[v["name"] == pick, "id"].iloc[0])
                deflate = (1 + a.inflation) ** (np.arange(len(fc.valuable_paths)) / 12) if real else 1
                plot(charts.asset_outlook(hist_all[hist_all["account_id"] == aid], fc.valuable_paths[pick] / deflate,
                                          mode, compact=PHONE))

            st.markdown("#### Milestones")
            if fc.milestones:
                for when, what in fc.milestones:
                    st.markdown(f"- **{when:%b %Y}**: {what}")
            else:
                st.caption("No milestones within this horizon. Try more years.")

            if fc.loans:
                st.markdown("#### Loans")
                est = [l.name for l in fc.loans if l.estimated]
                st.dataframe(pd.DataFrame([{"Loan": l.name, "Balance": l.balance, "Rate": l.apr * 100,
                                            "Monthly payment": l.payment} for l in fc.loans]),
                             hide_index=True, width="stretch", column_config={
                                 "Balance": st.column_config.NumberColumn(format="$%,.0f"),
                                 "Rate": st.column_config.NumberColumn(format="%.2f%%"),
                                 "Monthly payment": st.column_config.NumberColumn(format="$%,.0f")})
                if est:
                    st.caption(f"⚠️ Rate/payment guessed for {', '.join(est)}. Set the real ones in **Accounts** "
                               "for a better forecast.")
            with st.expander("How this forecast works"):
                st.markdown(
                    f"- Starts from today's balances in every account.\n"
                    f"- Investments are simulated {a.simulations:,} times with realistic ups and downs "
                    f"({a.investment_volatility:.0%} yearly swings). The band shows the middle 80% of outcomes.\n"
                    f"- Each loan is paid down month by month. When one is paid off, that payment becomes extra savings.\n"
                    f"- Credit cards are assumed paid in full each month.\n"
                    f"- Taxes, big one-off purchases and job changes aren't modeled.")


# --- money in & out -----------------------------------------------------------

with tab_flow:
    if txns.empty:
        st.info("Import a checking or credit card CSV in **Add data** to see where money goes.")
    else:
        if err := st.session_state.pop("ai_error", None):
            st.error(err, icon="⚠️")
        todo = categorize.uncategorized_merchants(conn, txns, insights.is_transfer)
        recheck = categorize.needs_recheck(conn)
        if len(todo) or recheck:
            with st.container(border=True):
                a1, a2 = st.columns([3, 1], vertical_alignment="center")
                if recheck:
                    n_all = len(categorize.uncategorized_merchants(conn, txns, insights.is_transfer, recheck=True))
                    a1.markdown(f"✨ **Categories were improved** (new: Mortgage, Loan payments; clearer definitions "
                                f"for all). Re-check **{n_all} merchants** with AI? Categories you set yourself are "
                                "kept. Only cleaned merchant names and rough amounts are sent (no account numbers, "
                                "names or dates).")
                else:
                    a1.markdown(f"✨ **{len(todo)} merchants** ({int(todo['n'].sum())} transactions) haven't been "
                                "categorized by AI yet. Only cleaned merchant names and rough amounts are sent "
                                "(no account numbers, names or dates).")
                if a2.button("Re-check with AI" if recheck else "Categorize with AI", type="primary", width="stretch",
                             disabled=not categorize.available(),
                             help=None if categorize.available() else "Needs AppleConnect installed and signed in"):
                    run_ai_categorize(recheck=recheck)
                    st.rerun()

        cf = insights.monthly_cash_flow(txns, rule_map)
        this_month = pd.Timestamp.today().to_period("M").to_timestamp()
        recent = cf[cf["month"] < this_month].tail(6)
        income, spend = recent["money_in"].mean(), recent["money_out"].mean()
        kpi_row([("Avg. money in", money(income)), ("Avg. money out", money(spend)),
                 ("Avg. left over", money(income - spend)),
                 ("Savings rate", f"{(income - spend) / income:.0%}" if income else "-")])
        st.caption("Monthly averages over the last 6 complete months. Transfers between your own accounts "
                   "and card payments are left out so nothing is counted twice.")

        st.markdown("#### Each month")
        plot(charts.cash_flow_bars(cf.tail(6 if PHONE else 12), mode, compact=PHONE))

        cats = insights.spending_by_category(txns, rules=rule_map)
        if len(cats):
            st.markdown("#### Where it goes" + ("" if PHONE else " (monthly average, last 3 months)"))
            if PHONE:
                st.caption("Monthly average, last 3 months")
            plot(charts.category_bars(cats, mode, compact=PHONE))

        with st.expander("ℹ️ What each category covers"):
            st.markdown("\n".join(f"- **{c}** - {d}" for c, d in categorize.CATEGORY_HELP.items()))

        st.markdown("#### Transactions")
        view = insights.enrich(txns, rule_map)
        cat_options = sorted(set(categorize.CATEGORIES) | set(view["category"].dropna()))
        periods = {"All time": None, "This month": 0, "Last 3 months": 3, "Last 12 months": 12}

        # Phone: filters stack inside a collapsible panel instead of a 4-wide row
        filter_box = st.expander("🔍 Filter") if PHONE else st.container()
        with filter_box:
            f1, f2, f3, f4 = [st.container()] * 4 if PHONE else st.columns([2, 1.3, 1.3, 1])
            q = f1.text_input("Search", placeholder="Search descriptions, e.g. amazon",
                              label_visibility="collapsed")
            pick_cats = f2.multiselect("Category", cat_options, placeholder="All categories",
                                       label_visibility="collapsed")
            pick = f3.multiselect("Accounts", sorted(txns["account"].unique()), placeholder="All accounts",
                                  label_visibility="collapsed")
            period = f4.selectbox("Period", list(periods), label_visibility="collapsed")

        if q:
            view = view[view["description"].str.contains(q, case=False, regex=False)]
        if pick_cats:
            view = view[view["category"].isin(pick_cats)]
        if pick:
            view = view[view["account"].isin(pick)]
        if (months := periods[period]) is not None:
            start = pd.Timestamp.today().to_period("M").to_timestamp() - pd.DateOffset(months=months)
            view = view[view["date"] >= start]
        has_who = bool(view["purchaser"].notna().any()) if "purchaser" in view else False
        view = view[["date", "account", "description", "merchant", "category", "amount"]
                    + (["purchaser"] if has_who else [])].reset_index(drop=True)

        spent, got = -view.loc[view["amount"] < 0, "amount"].sum(), view.loc[view["amount"] > 0, "amount"].sum()
        st.caption(f"**{len(view)}** transactions · in {money(got)} · out {money(spent)}  —  "
                   + ("✏️ **Tap a category twice to change it.** " if PHONE else
                      "✏️ **Double-click a category to change it.** ")
                   + "Every transaction from that merchant follows, including future imports.")

        # Keyed by the filters so pending edits never get applied to a differently-filtered table
        editor_key = f"txn_editor_{hash((q, tuple(pick_cats), tuple(pick), period))}"
        edited = st.data_editor(
            view, hide_index=True, width="stretch", height=420 if PHONE else 460, key=editor_key,
            disabled=["date", "account", "description", "merchant", "amount", "purchaser"],
            column_order=(["date", "description", "category", "amount"] if PHONE else
                          ["date", "account", "description", "category", "amount"] + (["purchaser"] if has_who else [])),
            column_config={
                "date": st.column_config.DateColumn("Date", format="MMM D" if PHONE else "MMM D, YYYY"),
                "account": "Account", "description": "Description",
                "purchaser": st.column_config.TextColumn("Who", help="Who made the purchase, on a shared card"),
                "category": st.column_config.SelectboxColumn(
                    "Category ✏️", options=cat_options, required=True,
                    help="What each category covers: see ℹ️ above the table"),
                "amount": st.column_config.NumberColumn("Amount", format="$%,.0f" if PHONE else "$%,.2f")})
        changed = edited[edited["category"] != view["category"]]
        if len(changed):
            for r in changed.drop_duplicates("merchant", keep="last").itertuples():
                categorize.set_rule(conn, r.merchant, r.category, source="user")
            n = int(view["merchant"].isin(changed["merchant"]).sum())
            st.session_state["flash"] = f"Updated {n} transaction(s) from {', '.join(changed['merchant'].unique())}"
            del st.session_state[editor_key]
            st.rerun()


# --- accounts -----------------------------------------------------------------

LABEL_TO_TYPE = {v: k for k, v in TYPE_LABELS.items()}


with tab_accounts:
    if len(accts):
        history = db.balance_history(conn)
        past_year = db.account_changes(conn, 12).set_index("account_id")["change"]
        outlooks = forecast.valuables_table(accts, forecast.Assumptions.from_dict(db.get_setting(conn, "assumptions")),
                                            history, trend_overrides()).set_index("id")
        same_items = {i: o[0] for i, o in trend_overrides().items()}       # e.g. Pokemon: same-cards prices

        def _trend(i):
            t = same_items.get(i, assets.trend(history[history["account_id"] == i]))
            return "" if t is None else f"{t:+.1%}/yr"

        def _paid_off(r):
            p = insights.loan_payoff(r.balance, r.rate, r.payment)
            return "" if p is None else f"{pd.Timestamp.today() + pd.DateOffset(months=p[0]):%b %Y}"

        table = accts.assign(
            Group=accts["type"].map(insights.GROUPS), Type=accts["type"].map(TYPE_LABELS),
            Updated=pd.to_datetime(accts["as_of"]),
            bank=[f"{i} ···{l}" if isinstance(l, str) and l else i for i, l in zip(accts["institution"], accts["last4"])],
            rate_pct=(accts["rate"] * 100).round(3), payment=accts["payment"],
            shown_balance=[editing.to_display(b, l) for b, l in zip(accts["balance"], accts["is_liability"])],
            status=["⚠️ needs a balance" if pd.isna(b) else "" for b in accts["balance"]],
            new_balance=np.nan,
            year=[past_year.get(i, np.nan) for i in accts["id"]],
            trend=[_trend(i) if t in db.VALUABLE_TYPES else "" for i, t in zip(accts["id"], accts["type"])],
            future=[f"{outlooks.at[i, 'rate']:+.1%}/yr" if i in outlooks.index else "" for i in accts["id"]],
            payoff=[_paid_off(r) if r.type != "credit_card" else "" for r in accts.itertuples()],
        ).sort_values(["is_liability", "Group", "name"]).reset_index(drop=True)
        table["new_balance"] = table["new_balance"].astype("float64")
        ver = st.session_state.get("acct_table_ver", 0)
        good, bad = ("#0ca30c", "#e66767") if mode == "dark" else ("#006300", "#d03b3b")

        def _balance_color(row):
            v = row["shown_balance"]
            color = "" if pd.isna(v) or v == 0 else (bad if row["is_liability"] and v > 0 else good)
            return [f"color: {color}" if (c == "shown_balance" and color) else "" for c in row.index]

        st.caption("To update an account, type today's number from the bank's app or site into its **Type new …** "
                   "cell and press Enter. It's saved as of today and becomes the balance. Any ✏️ column can be "
                   "edited the same way (double-click the cell).")
        with st.expander("ℹ️ What the columns mean"):
            st.markdown(
                "- **Balance / Value / Owed** - the latest number on record, green for money you have and red for "
                "what you owe on cards and loans. Enter debts the way the bank shows them (what you owe); a card "
                "in credit is a negative number.\n"
                "- **Type new balance / value / amount owed ✏️** - where you type a new number. It's saved as of "
                "today; the cell then clears and the Balance column shows it. For a past date, use **Update a "
                "balance** below.\n"
                "- **Past year** - how much this account changed your net worth in the last 12 months (a loan paid "
                "down shows as +). Blank: no value from a year ago.\n"
                "- **Past trend** - how the asset's own value changed per year, fitted across all its history.\n"
                "- **Future uses** - the yearly rate the Future tab grows it at: its history blended with the "
                "long-run rate for its kind. **Your growth % ✏️** overrides it.\n"
                "- **APR % / Interest % ✏️** and **Monthly payment ✏️** - loan terms, from statements or typed in. "
                "They drive **Paid off by** and the Future tab.\n"
                "- **Updated** - the date of the latest value. Pick an account under **Account details** to see "
                "every value on record and where it came from.")

        balance_word = {"Property & valuables": ("Value", "Type new value ✏️"),
                        "Credit cards": ("Owed", "Type amount owed ✏️"), "Loans": ("Owed", "Type amount owed ✏️")}
        extras = {"Property & valuables": ["rate_pct", "trend", "future"], "Credit cards": ["rate_pct"],
                  "Loans": ["rate_pct", "payment", "payoff"]}
        rate_label = {"Property & valuables": ("Your growth % ✏️", "Expected yearly change you want the Future tab to "
                                               "use (+ appreciates, − depreciates). Blank = built from its history."),
                      "Credit cards": ("APR % ✏️", "The card's interest rate"),
                      "Loans": ("Interest % ✏️", "The loan's interest rate")}
        all_edits, all_balances = {}, {}
        for heading, groups in (("What you own", insights.ASSET_GROUPS), ("What you owe", insights.DEBT_GROUPS)):
            if not table["Group"].isin(groups).any():
                continue
            st.markdown(f"### {heading} · {money(sum(totals[g] for g in groups))}")
            for g in groups:
                rows = table[table["Group"] == g].reset_index(drop=True)
                if rows.empty:
                    continue
                st.markdown(f"**{g}** · {money(totals[g])}")
                shown_word, new_word = balance_word.get(g, ("Balance", "Type new balance ✏️"))
                cols = (["name", "shown_balance", "new_balance", "Updated"] if PHONE else
                        ["name", "bank", "Type", "shown_balance", "new_balance", "year"]
                        + [c for c in extras.get(g, []) if c not in ("trend", "future", "payoff") or rows[c].astype(bool).any()]
                        + ["Updated"] + (["status"] if rows["status"].any() else []))
                rate_name, rate_help = rate_label.get(g, ("Rate %", ""))
                edited = st.data_editor(
                    rows.style.apply(_balance_color, axis=1), hide_index=True, width="stretch", column_order=cols,
                    key=f"acct_{g}_{ver}",
                    disabled=["bank", "Group", "shown_balance", "Updated", "status", "year", "trend", "future", "payoff"],
                    column_config={   # read-only columns can show colors
                        "name": st.column_config.TextColumn("Account ✏️", required=True),
                        "bank": st.column_config.TextColumn("Bank / source", help="Institution and the account's "
                                                            "last 4 digits, used to match future statements"),
                        "Type": st.column_config.SelectboxColumn("Type ✏️", options=list(TYPE_LABELS.values()),
                                                                 required=True),
                        "shown_balance": st.column_config.NumberColumn(
                            shown_word, format="$%,.0f" if PHONE else "$%,.2f",
                            help="Green: money you have. Red: what you owe on cards and loans."),
                        "new_balance": st.column_config.NumberColumn(
                            new_word, format="$%,.2f",
                            help="Type today's number as the bank shows it (cards and loans: what you owe; a card in "
                                 "credit: negative) and press Enter. Saved as of today."),
                        "year": st.column_config.NumberColumn(
                            "Past year", format="$%+,.0f",
                            help="Effect on your net worth over the last 12 months (a loan paid down shows +)"),
                        "status": st.column_config.TextColumn(""),
                        "rate_pct": st.column_config.NumberColumn(rate_name, format="%.2f", min_value=-100,
                                                                  max_value=100, help=rate_help),
                        "trend": st.column_config.TextColumn("Past trend", help="How this asset's own value changed "
                                                                                "per year, from its history"),
                        "future": st.column_config.TextColumn("Future uses", help="Yearly rate the Future tab uses"),
                        "payment": st.column_config.NumberColumn("Monthly payment ✏️", format="$%,.2f", min_value=0,
                                                                 help="Monthly principal + interest"),
                        "payoff": st.column_config.TextColumn("Paid off by", help="At this rate and payment"),
                        "Updated": st.column_config.DateColumn(format="MMM D" if PHONE else "MMM D, YYYY")})
                all_edits.update(editing.account_edits(rows, edited, LABEL_TO_TYPE, terms="rate_pct" in cols))
                all_balances.update(editing.balance_edits(rows, edited))

        by_id = table.set_index("id")
        for aid, value in all_balances.items():
            db.upsert_balance(conn, aid, date.today(), value, "manual")
            db.log_edit(conn, by_id.at[aid, "name"], "balance", by_id.at[aid, "balance"], value, _device())
        for aid, fields in all_edits.items():
            for k, v in fields.items():
                db.log_edit(conn, by_id.at[aid, "name"], k, by_id.at[aid, k], v, _device())
        if all_balances and not all_edits:
            st.session_state["flash"] = "Saved balance for " + ", ".join(by_id.at[i, "name"] for i in all_balances)
            st.session_state["acct_table_ver"] = ver + 1
            st.rerun()
        if all_edits:
            try:
                db.apply_account_edits(conn, all_edits)
                st.session_state["flash"] = "Saved changes to " + ", ".join(by_id.at[i, "name"] for i in all_edits)
            except Exception as e:  # noqa: BLE001 - e.g. two accounts with the same name
                st.session_state["flash"] = f"Couldn't save: {e}"
            st.session_state["acct_table_ver"] = ver + 1      # fresh editors showing the saved values
            st.rerun()

        # --- one account, in depth: every value on record, where it came from, and what's behind it
        st.markdown("### Account details")
        pick = st.selectbox("Show details for", table["name"].tolist(), key="acct_detail")
        r = table.loc[table["name"] == pick].iloc[0]
        aid, debt = int(r["id"]), bool(r["is_liability"])
        h = history[history["account_id"] == aid].sort_values("date").assign(
            how=lambda d: d["source"].map(insights.source_label), estimate=lambda d: d["source"].map(insights.is_estimate))
        st.caption(" · ".join(x for x in (TYPE_LABELS.get(r["type"], r["type"]), r["bank"], r["notes"]) if x))
        if h.empty:
            st.info("No values on record yet. Type one into the table above, or import a statement.")
        else:
            first = h.iloc[0]
            since = (first["balance"] - r["balance"]) if debt else (r["balance"] - first["balance"])
            n_est = int(h["estimate"].sum())
            kpi_row([("Owed now" if debt else "Value now", money(r["balance"])),
                     ("Past year", signed(past_year.get(aid))),
                     (f"Since {first['date']:%b %Y}", signed(since)),
                     ("Values on record", f"{len(h):,}" + (f" ({n_est} est.)" if n_est else ""))])
            plot(charts.account_history(h, mode, debt=debt, compact=PHONE))
            if n_est:
                st.caption("Dots are values on record (statements, imports, what you typed). The line between them "
                           "is estimated; a value you enter always takes priority over an estimate.")
        if debt and r["type"] != "credit_card":
            if p := insights.loan_payoff(r["balance"], r["rate"], r["payment"]):
                st.markdown(f"At **{money(r['payment'], short=False)}/month** and **{r['rate']:.2%}** interest, it's paid "
                            f"off around **{r['payoff']}** ({p[0]} payments left), with about **{money(p[1])}** of "
                            "interest still to pay.")
            else:
                st.caption("Add the interest rate and monthly payment in the Loans table to see when it's paid off.")
        if aid in outlooks.index:
            o = outlooks.loc[aid]
            st.markdown(f"**Future tab:** grows at **{o['rate']:+.1%}/yr** ({o['why']}).")
        held = db.latest_holdings(conn)
        held = held[held["account"] == pick]
        if len(held):
            st.markdown(f"**Holdings** · {len(held)} positions as of {pd.Timestamp(held['as_of'].iloc[0]):%b %-d, %Y}")
            st.dataframe(held.assign(gain=held["value"] - held["cost_basis"]), hide_index=True, width="stretch",
                         column_order=["symbol", "description", "quantity", "price", "value", "gain"],
                         column_config={"symbol": "Symbol", "description": "Name",
                                        "quantity": st.column_config.NumberColumn("Shares", format="%,.3f"),
                                        "price": st.column_config.NumberColumn("Price", format="$%,.2f"),
                                        "value": st.column_config.NumberColumn("Value", format="$%,.0f"),
                                        "gain": st.column_config.NumberColumn("Unrealized gain", format="$%+,.0f")})
        grants = db.latest_grants(conn)
        if len(grants := grants[grants["account"] == pick]):
            st.markdown(f"**Unvested grants** · {money(grants['value'].sum())} (not counted in net worth until they vest)")
            st.dataframe(grants, hide_index=True, width="stretch",
                         column_order=["grant_date", "type", "symbol", "quantity", "value"],
                         column_config={"grant_date": "Granted", "type": "Type", "symbol": "Symbol",
                                        "quantity": st.column_config.NumberColumn("Shares", format="%,.0f"),
                                        "value": st.column_config.NumberColumn("Value", format="$%,.0f")})
        acct_txns = txns[txns["account"] == pick]
        if len(acct_txns):
            with st.expander(f"Transactions ({len(acct_txns):,}, {acct_txns['date'].min():%b %Y} – "
                             f"{acct_txns['date'].max():%b %Y})"):
                st.dataframe(acct_txns, hide_index=True, width="stretch",
                             column_order=["date", "description", "amount", "category"],
                             column_config={"date": st.column_config.DateColumn("Date", format="MMM D, YYYY"),
                                            "description": "Description", "category": "Category",
                                            "amount": st.column_config.NumberColumn("Amount", format="$%,.2f")})
        if len(h):
            with st.expander(f"All {len(h):,} values on record"):
                st.dataframe(h.sort_values("date", ascending=False), hide_index=True, width="stretch",
                             column_order=["date", "balance", "how"],
                             column_config={"date": st.column_config.DateColumn("Date", format="MMM D, YYYY"),
                                            "balance": st.column_config.NumberColumn("Owed" if debt else "Value",
                                                                                     format="$%,.2f"),
                                            "how": "Where it came from"})

    if pokemon.available():
        with st.expander("🃏 Pokemon collection (from your Pokemon dashboard)"):
            st.caption("Values your owned cards at their latest raw prices, with month-end history from the "
                       "dashboard's price snapshots. Your Pokemon database is only read, never changed.")
            if (g := pokemon_trend()) is not None:
                st.caption(f"Cards you've held since the first snapshot changed **{g['growth']:+.0%}/yr** in price "
                           f"over {g['years']:.1f} years. The Future tab blends that with the long-run rate for "
                           "collectibles, giving history more weight as it gets longer.")
            if st.button("Refresh from Pokemon dashboard"):
                with st.spinner("Reading your Pokemon dashboard (about 30 seconds)…"):
                    try:
                        r = pokemon.sync(conn)
                        db.set_setting(conn, "pokemon_price_growth", {"growth": r["growth"], "years": r["years"],
                                                                      "vol": r["vol"], "account_id": r["account_id"]})
                        st.session_state["flash"] = (f"Pokemon collection: {money(r['value'])} "
                                                     f"({r['cards']:,} cards, {r['points']} month-end values)")
                        st.session_state["acct_table_ver"] = st.session_state.get("acct_table_ver", 0) + 1
                    except Exception as e:  # noqa: BLE001
                        st.session_state["flash"] = f"Couldn't read the Pokemon dashboard: {e}"
                st.rerun()

    names = dict(zip(accts["name"], accts["id"]))
    c1, c2 = st.columns(2, gap="large")
    with c1:
        if names:
            with st.form("balance", clear_on_submit=True, border=True):
                st.markdown("**Update a balance**")
                st.caption("For anything without a statement or CSV: home value, car, 401k…")
                acct = st.selectbox("Account", list(names))
                when = st.date_input("As of", value=date.today())
                amt = st.number_input("Balance ($)", step=100.0,
                                      help="For cards and loans, enter what you owe, as your bank shows it. "
                                           "A card in credit: a negative number.")
                if st.form_submit_button("Save balance", type="primary"):
                    row = accts.loc[accts["name"] == acct].iloc[0]
                    db.upsert_balance(conn, names[acct], when, editing.to_stored(amt, bool(row["is_liability"])))
                    db.log_edit(conn, acct, f"balance ({when:%b %-d})", row["balance"], amt, _device())
                    st.rerun()
    with c2:
        with st.form("new_account", clear_on_submit=True, border=True):
            st.markdown("**Add an account**")
            name = st.text_input("Name", placeholder="e.g. BofA Checking")
            inst = st.text_input("Institution", value="Bank of America")
            typ = st.selectbox("Type", list(TYPE_LABELS), format_func=TYPE_LABELS.get)
            if st.form_submit_button("Add account", type="primary") and name:
                try:
                    db.add_account(conn, name, inst, typ)
                    st.rerun()
                except Exception as e:  # noqa: BLE001 - duplicate name etc.
                    st.error(f"Couldn't add account: {e}")
        if names:
            with st.expander("Remove an account"):
                gone = st.selectbox("Account to remove", list(names), key="rm")
                sure = st.checkbox(f"Yes, delete {gone} and all its history")
                if st.button("Remove", disabled=not sure):
                    checkpoints.create(conn, f"Before removing {gone}")
                    db.delete_account(conn, names[gone])
                    db.log_edit(conn, gone, "removed", None, None, _device())
                    st.rerun()

    changes = db.recent_edits(conn)
    if len(changes):
        with st.expander("🕘 Recent changes"):
            st.dataframe(changes.assign(at=pd.to_datetime(changes["at"])), hide_index=True, width="stretch",
                         column_config={"at": st.column_config.DatetimeColumn("When", format="MMM D, h:mm a"),
                                        "account": "Account", "what": "Changed", "old": "From", "new": "To",
                                        "device": "Device"})


# --- add data -----------------------------------------------------------------

NEW_ACCOUNT = "➕ New account"
SHORT_INST = {"Bank of America": "BofA", "American Express": "Amex", "Golden 1 Credit Union": "Golden 1"}


@st.cache_data(show_spinner=False, max_entries=200)
def _read_pdf_cached(data: bytes, reader_version: float) -> list[statements.Statement]:
    """Every account in the PDF. Cached by file contents AND reader version: when the reading code changes,
    files are re-read (keying on contents alone kept serving results from the old code after a fix)."""
    try:
        return statements.parse_pdf_all(data)
    except Exception as e:  # noqa: BLE001 - damaged/protected PDFs become a row you fill in by hand
        return [statements.Statement(kind="unknown", notes=[f"Couldn't read this PDF: {e}"])]


def _read_pdf(data: bytes) -> list[statements.Statement]:
    return _read_pdf_cached(data, os.path.getmtime(statements.__file__))


def _suggest_account(s: statements.Statement) -> str:
    """Only suggest an existing account when it's clearly the same one: same account-number digits, or
    the only account of this type at the same institution (and not already tied to other digits).
    Otherwise default to a new account - people often have several brokerage/checking accounts."""
    if s.last4:
        hit = accts[accts["last4"] == s.last4]
        if len(hit) == 1:
            return hit["name"].iloc[0]
    if s.institution and s.account_type:
        same = accts[(accts["institution"] == s.institution) & (accts["type"] == s.account_type)
                     & (accts["last4"].isna() | (accts["last4"] == s.last4))]
        if len(same) == 1:
            return same["name"].iloc[0]
    return NEW_ACCOUNT


# Per statement kind: table title, the value columns that apply, and the account types a new account may be
STATEMENT_VIEWS = {
    "loan": ("Loans", [("balance", "Principal owed"), ("rate", "Interest rate %"), ("payment", "Monthly payment (P&I)")],
             ["mortgage", "heloc", "auto_loan", "student_loan", "personal_loan", "other_liability"]),
    "deposit": ("Bank accounts", [("balance", "Ending balance")], ["checking", "savings"]),
    "credit_card": ("Credit cards", [("balance", "Statement balance"), ("rate", "Purchase APR %")], ["credit_card"]),
    "investment": ("Investments", [("balance", "Account value"), ("extras", "Also found")], ["brokerage", "retirement"]),
    "unknown": ("Not recognized", [("balance", "Balance")], list(TYPE_LABELS)),
}
MONEY_COLS = {"balance", "payment"}


class _Named:
    """An uploaded file seen as one account's statement (file name + which account, for multi-account PDFs)."""
    def __init__(self, f, label: str):
        self.name, self.file_id, self._f = label, f"{f.file_id}:{label}", f

    def getvalue(self):
        return self._f.getvalue()


def render_statement_review(pdf_files) -> None:
    """One small table per kind of statement, showing only the values that apply to it. Nothing is
    saved until 'Save statements', so a misread value can be fixed first."""
    st.markdown("#### Statements")
    # one row per ACCOUNT: a Robinhood PDF can hold several; label them "file · Individual …4688"
    everything = []
    for f in pdf_files:
        found = _read_pdf(f.getvalue())
        for s in found:
            label = f"{f.name} · {s.name_hint}" if len(found) > 1 and s.name_hint else f.name
            everything.append((_Named(f, label), s))
    for f, s in everything:
        if not s.importable:                       # nothing to save: explain, no table row
            st.info(f"**{f.name}** - " + " ".join(s.notes), icon="ℹ️")
    parsed = [(f, s) for f, s in everything if s.importable]
    if not parsed:
        return
    # Suggested account per row - but never the same existing account for statements with different
    # account numbers (e.g. three Robinhood accounts vs. one manual "Robinhood" account): those default to new.
    suggest = {i: _suggest_account(s) for i, (_, s) in enumerate(parsed)}
    for name in {v for v in suggest.values() if v != NEW_ACCOUNT}:
        rows_for = [i for i, v in suggest.items() if v == name]
        if len({parsed[i][1].last4 for i in rows_for}) > 1:
            for i in rows_for:
                if accts.loc[accts["name"] == name, "last4"].iloc[0] != parsed[i][1].last4:
                    suggest[i] = NEW_ACCOUNT
    edited_tables = []
    for kind, (title, fields, types) in STATEMENT_VIEWS.items():
        idx = [i for i, (_, s) in enumerate(parsed) if s.kind == kind]
        if not idx:
            continue
        rows = []
        for i in idx:
            f, s = parsed[i]
            acct_type = s.account_type if s.account_type in types else types[0]
            rows.append({"row_id": i, "save": kind != "unknown" or s.balance is not None, "file": f.name,
                         "date": s.as_of, "balance": s.balance,
                         "rate": round(s.rate * 100, 3) if s.rate is not None else None, "payment": s.payment,
                         "extras": s.extras, "account": suggest[i],
                         "new_name": (f"{SHORT_INST.get(s.institution, s.institution)} {TYPE_LABELS[acct_type]}".strip()
                                      + (f" – {s.name_hint}" if s.name_hint else "")),
                         "type": acct_type})
        cols = ["save"] + ([] if PHONE else ["file"]) + ["date"] + [c for c, _ in fields] + ["account"] \
            + ([] if PHONE else ["new_name"] + (["type"] if len(types) > 1 else []))
        config = {
            "save": st.column_config.CheckboxColumn("Save", width="small"), "file": "File",
            "date": st.column_config.DateColumn("Statement date", format="MMM D, YYYY"),
            "account": st.column_config.SelectboxColumn("Account", options=list(accts["name"]) + [NEW_ACCOUNT],
                                                        required=True),
            "new_name": st.column_config.TextColumn("Name if new"),
            "type": st.column_config.SelectboxColumn("Type if new", options=types,
                                                     help=", ".join(TYPE_LABELS[t] for t in types)),
        }
        for c, label in fields:
            config[c] = (st.column_config.NumberColumn(label, format="$%,.2f") if c in MONEY_COLS
                         else st.column_config.NumberColumn(label, format="%.3f") if c == "rate"
                         else st.column_config.TextColumn(label))
        st.markdown(f"**{title}**")
        edited = st.data_editor(pd.DataFrame(rows), hide_index=True, width="stretch", column_order=cols,
                                column_config=config, disabled=["file", "extras"],
                                key=f"stmts_{kind}_{hash(tuple(parsed[i][0].file_id for i in idx))}")
        edited_tables.append((kind, edited))
        for i in idx:
            if parsed[i][1].notes:
                st.caption(f"⚠️ **{parsed[i][0].name}**: " + " · ".join(parsed[i][1].notes))
    with st.expander("Show the text read from each PDF"):
        for f, s in parsed:
            st.markdown(f"**{f.name}**")
            st.code(s.text[:4000] or "(no text)", language=None)

    if st.button("Save statements", type="primary"):
        chosen = pd.concat([t[t["save"]] for _, t in edited_tables]) if edited_tables else pd.DataFrame()
        if chosen.empty:
            st.info("Nothing selected to save.")
            return
        problems = [r.file for r in chosen.itertuples() if pd.isna(r.date) or pd.isna(r.balance)
                    or (r.account == NEW_ACCOUNT and not str(r.new_name).strip())]
        if problems:
            st.error("Fill in the statement date, balance (and a name for new accounts) for: " + ", ".join(problems))
            return
        mismatched = []
        for r in chosen.itertuples():
            if r.account != NEW_ACCOUNT:
                known = accts.loc[accts["name"] == r.account, "last4"].iloc[0]
                got = parsed[r.row_id][1].last4
                if pd.notna(known) and got and known != got:
                    mismatched.append(f"{r.file} is for an account ending {got}, but {r.account} ends {known}")
        for acct_name, grp in chosen[chosen["account"] != NEW_ACCOUNT].groupby("account"):
            digits = {parsed[i][1].last4 for i in grp["row_id"]} - {None}
            if len(digits) > 1:
                mismatched.append(f"{acct_name} is chosen for {len(digits)} different accounts "
                                  f"(ending {', '.join(sorted(digits))})")
        if mismatched:
            st.error("These look like different accounts - pick ➕ New account or the right one: "
                     + "; ".join(mismatched))
            return
        checkpoints.create(conn, f"Before saving {len(chosen)} statement(s): " + ", ".join(chosen["file"])[:80])
        ids = {}
        for r in chosen.itertuples():
            if r.account == NEW_ACCOUNT:
                ids[r.row_id] = db.get_or_create_account(conn, str(r.new_name).strip(),
                                                     parsed[r.row_id][1].institution or "Other", r.type)
            else:
                ids[r.row_id] = int(accts.loc[accts["name"] == r.account, "id"].iloc[0])
        chosen = chosen.assign(acct=chosen["row_id"].map(ids))
        newest = set(chosen.sort_values("date").groupby("acct").tail(1)["row_id"])
        for r in chosen.itertuples():
            orig = parsed[r.row_id][1]
            s = statements.Statement(
                kind=orig.kind, institution=orig.institution, last4=orig.last4,
                as_of=pd.Timestamp(r.date).date(), balance=float(r.balance),
                rate=float(r.rate) / 100 if pd.notna(r.rate) else None,
                payment=float(r.payment) if pd.notna(r.payment) else None,
                history=orig.history, holdings=orig.holdings, grants=orig.grants)
            apply_statement(conn, s, ids[r.row_id], r.file, latest=r.row_id in newest)
        st.session_state["flash"] = f"Saved {len(chosen)} statement(s)"
        st.session_state["uploads_done"] = st.session_state.get("uploads_done", 0) + 1   # empty the uploader
        st.rerun()


with tab_add:
    st.markdown("#### Import a file")
    with st.expander("How to download your files"):
        st.markdown(
            "**Bank of America / Merrill**\n"
            "- **Checking / savings**: open the account → *Download* → pick a date range → "
            "*Microsoft Excel format*. Saves a `.csv` with every transaction.\n"
            "- **Credit card**: open the card → *Download transactions* → pick a statement → `.csv`.\n"
            "- **Merrill**: *Portfolio* → *Holdings* → the download icon → `.csv`.\n"
            "- **Mortgage / auto loan / HELOC**: download the monthly **statement PDF** - it fills in the "
            "balance, interest rate and payment for you.\n"
            "- **Any statement PDF** (checking, savings, card, Merrill) adds that month's balance - handy for "
            "history older than the CSV download allows.\n\n"
            "\n**American Express**\n"
            "- **Card**: *Statements & Activity* → *Download* → **CSV** (tick *Include all additional details*).\n"
            "\n**Chase**\n"
            "- **Checking / savings / credit card**: open the account → *Download account activity* → pick the "
            "dates → *Spreadsheet (Excel, CSV)*.\n"
            "- **Mortgage / auto loan**: download the monthly **statement PDF**.\n\n"
            "Overlapping files are fine. Duplicates are skipped automatically.")
    files = st.file_uploader("Drop CSV or statement PDF files here", type=["csv", "pdf"], accept_multiple_files=True,
                             key=f"uploader_{st.session_state.get('uploads_done', 0)}")
    pdf_files = [f for f in files or [] if f.name.lower().endswith(".pdf")]
    files = [f for f in files or [] if not f.name.lower().endswith(".pdf")]
    if pdf_files:
        render_statement_review(pdf_files)
    for f in files:
        if f.file_id in st.session_state.get("imported_files", set()):
            st.caption(f"✅ {f.name} imported")
            continue
        with st.container(border=True):
            try:
                parsed = parse_file(f.getvalue(), filename=f.name)
            except UnrecognizedFile:
                st.error(f"**{f.name}**: this isn't a format I recognize yet (Bank of America, Merrill, Chase, Wealthfront, American Express, Barclays and Apple Card CSVs are supported). "
                         "Share the column headers and I'll add support.")
                continue
            st.markdown(f"**{f.name}** · {KIND_LABELS[parsed.kind]} · {parsed.summary}"
                        + (f" · as of {parsed.as_of:%b %d, %Y}" if parsed.as_of else ""))
            preview = parsed.holdings if len(parsed.holdings) else parsed.transactions.drop(columns="fingerprint")
            st.dataframe(preview.head(8), hide_index=True, width="stretch")

            allowed = KIND_ACCOUNT_TYPES[parsed.kind]
            matches = accts[accts["type"].isin(allowed)]
            options = list(matches["name"]) + ["➕ New account…"]
            k = f.file_id
            guess, why = suggest_csv_account(conn, parsed, f.name, matches)
            if guess is None and not (matches["institution"] == parsed.institution).any():
                guess = options[-1]      # nothing at this bank yet: it's a new account
            target = st.selectbox("Import into", options, key=f"t{k}_{guess}",   # follows the current best match
                                  index=options.index(guess) if guess else None, placeholder="Choose the account…")
            if why and target == guess:
                st.caption(f"Matched automatically: {why}.")
            elif guess is None and not matches.empty:
                st.caption("Couldn't tell which account this file is from - please choose.")
            new_name = new_type = None
            if target == options[-1]:
                n1, n2 = st.columns(2)
                new_name = n1.text_input("Account name", key=f"n{k}", placeholder="e.g. BofA Checking")
                new_type = n2.selectbox("Type", allowed, format_func=TYPE_LABELS.get, key=f"y{k}")
            if parsed.kind == "credit_card" and parsed.balances.empty:
                st.caption("Card files don't include a balance. Add the current balance in **Accounts** afterwards.")
            if parsed.note:
                st.caption(f"⚠️ {parsed.note}")
            if st.button("Import", key=f"b{k}", type="primary",
                         disabled=target is None or (target == options[-1] and not new_name)):
                checkpoints.create(conn, f"Before importing {f.name} into {new_name or target}")
                acct_id = (db.get_or_create_account(conn, new_name, parsed.institution, new_type) if new_name
                           else int(matches.loc[matches["name"] == target, "id"].iloc[0]))
                r = apply(conn, parsed, acct_id, f.name)
                db.update_account_details(conn, acct_id, last4=last4_from_filename(f.name) or parsed.account_hint or None)
                if r["transactions_added"] and categorize.available():
                    run_ai_categorize()                 # reads the transactions just imported
                done = st.session_state.setdefault("imported_files", set())
                done.add(f.file_id)
                st.session_state["flash"] = (
                    f"{f.name}: added {r['transactions_added']} transactions"
                    + (f" ({r['transactions_skipped']} already there)" if r["transactions_skipped"] else "")
                    + (f", {r['positions']} positions" if r["positions"] else "")
                    + (f", {r['balances']} balance points" if r["balances"] else ""))
                if all(x.file_id in done for x in files):          # every CSV imported: clear the uploader
                    st.session_state["uploads_done"] = st.session_state.get("uploads_done", 0) + 1
                st.rerun()                                          # every tab reflects the import right away

    cps = checkpoints.list_all(conn)
    if cps:
        with st.expander("↩️ Undo an import"):
            st.caption("A snapshot is saved automatically before every import and before removing an account. "
                       "Restoring puts everything back exactly as it was then - and is itself saved first, so it "
                       "can be undone too.")
            fmt = {c["id"]: f"{datetime.fromtimestamp(c['created']):%b %-d, %-I:%M %p} · {c['label']}" for c in cps}
            pick = st.selectbox("Go back to", [c["id"] for c in cps], format_func=fmt.get, key="cp_pick")
            chosen_cp = next(c for c in cps if c["id"] == pick)
            n = chosen_cp["counts"]
            st.caption(f"At that point: {n['accounts']} accounts, {n['transactions']:,} transactions, "
                       f"{n['balances']:,} balance points.")
            ok = st.checkbox("Yes, put my data back to this point", key=f"cp_ok_{pick}")
            if st.button("Restore", disabled=not ok):
                checkpoints.restore(conn, pick)
                st.session_state["flash"] = f"Restored: {chosen_cp['label']}. (This was saved too, so you can undo it.)"
                st.session_state["uploads_done"] = st.session_state.get("uploads_done", 0) + 1
                st.rerun()
