"""Personal finance dashboard.  Run:  streamlit run app.py"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import date

import pandas as pd
import streamlit as st

from finance import categorize, charts, db, demo, forecast, insights
from finance.charts import money
from finance.importers import KIND_ACCOUNT_TYPES, UnrecognizedFile, apply, parse_file

st.set_page_config(page_title="Financial Analyst", page_icon="📈", layout="wide")

# Wi-Fi mode (started by "Dashboard (Wi-Fi).command") is reachable from other devices, so it
# requires the password set by that launcher. Local-only mode needs no password.
if os.environ.get("FINANCE_LAN") == "1" and not st.session_state.get("authed"):
    stored = db.ROOT / "data" / "app_password"
    if not stored.exists():
        st.error("No password set. Start the dashboard with “Dashboard (Wi-Fi).command” to create one.")
        st.stop()
    salt_hex, digest_hex = stored.read_text().split(":")
    with st.form("login"):
        st.markdown("### 🔒 Financial Analyst")
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Unlock", type="primary"):
            attempt = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
            if hmac.compare_digest(attempt.hex(), digest_hex):
                st.session_state["authed"] = True
                st.rerun()
            st.error("Wrong password")
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

# Desktop rules first; everything phone-specific sits inside the small-screen media query,
# so the Mac layout is untouched.
st.markdown("""
<style>
  .block-container { padding-top: 2rem; max-width: 1200px; }
  [data-testid="stMetricValue"] { font-size: 1.6rem; }
  .hero { font-size: 3rem; font-weight: 700; line-height: 1.1; margin: 0; }
  .hero-sub { color: var(--text-color); opacity: .65; margin: .25rem 0 1rem; }

  @media (max-width: 640px) {
    .block-container { padding: 1rem .75rem 3rem; }
    .hero { font-size: 2.1rem; }
    .hero-sub { font-size: .9rem; margin-bottom: .75rem; }
    h4 { font-size: 1.05rem !important; padding-top: .75rem !important; }
    [data-testid="stMetricValue"] { font-size: 1.2rem; }
    [data-testid="stMetricLabel"] p { font-size: .75rem; }
    /* metric rows become a 2-column grid instead of a tall single column */
    [class*="st-key-kpis"] [data-testid="stHorizontalBlock"] { flex-wrap: wrap; gap: .5rem; }
    [class*="st-key-kpis"] [data-testid="stColumn"] {
      flex: 1 1 calc(50% - .5rem) !important; min-width: calc(50% - .5rem) !important;
      width: calc(50% - .5rem) !important;
    }
    [class*="st-key-kpis"] [data-testid="stVerticalBlockBorderWrapper"] { padding: .6rem .75rem; }
    /* tabs: tighter, swipeable */
    [data-baseweb="tab-list"] { gap: .25rem; overflow-x: auto; }
    [data-baseweb="tab"] { padding: .4rem .55rem; font-size: .9rem; }
  }
</style>
""", unsafe_allow_html=True)

TYPE_LABELS = {
    "checking": "Checking", "savings": "Savings", "brokerage": "Brokerage", "retirement": "Retirement (401k/IRA)",
    "property": "Home / real estate", "vehicle": "Vehicle", "other_asset": "Other asset",
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
    st.caption("Everything stays on this computer in `data/`.")

conn = get_conn(str(db.ROOT / "data" / ("demo.db" if source == "Demo data" else "finance.db")))
mode = "dark" if getattr(st.context, "theme", None) and st.context.theme.type == "dark" else "light"


def plot(fig):
    st.plotly_chart(fig, width="stretch", theme=None,
                    config={"displayModeBar": False, "scrollZoom": False, "responsive": True})


def kpi_row(items: list[tuple[str, str]]) -> None:
    """A row of bordered metric cards: one row on desktop, a 2-column grid on phones."""
    with st.container(key=f"kpis_{items[0][0]}".replace(" ", "_")):
        for col, (label, value) in zip(st.columns(len(items)), items):
            with col.container(border=True):
                st.metric(label, value)


accts = db.accounts(conn)
txns = db.transactions(conn)
rule_map = categorize.rules(conn)
nw = db.net_worth_series(conn)
totals = insights.group_totals(accts) if len(accts) else {}
est_savings = insights.estimate_monthly_savings(txns, rules=rule_map)


def run_ai_categorize() -> None:
    bar = st.progress(0.0, text="Categorizing merchants…")
    try:
        n = categorize.auto_categorize(conn, txns, insights.is_transfer,
                                       progress=lambda f: bar.progress(f, text="Categorizing merchants…"))
        st.toast(f"Categorized {n} merchants")
    except Exception as e:  # noqa: BLE001 - surface any auth/network problem plainly
        st.error(f"AI categorization didn't run: {e}")
    finally:
        bar.empty()

tab_overview, tab_future, tab_flow, tab_accounts, tab_add = st.tabs(
    ["Overview", "Future", "Money", "Accounts", "Add"] if PHONE else
    ["Overview", "Future", "Money in & out", "Accounts", "Add data"])


# --- overview -----------------------------------------------------------------

def change_since(months: int) -> float | None:
    if len(nw) < 2:
        return None
    then = nw[nw["date"] <= nw["date"].iloc[-1] - pd.DateOffset(months=months)]
    return None if then.empty else float(nw["net_worth"].iloc[-1] - then["net_worth"].iloc[-1])


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

        def signed(v):
            return "-" if v is None else ("+" if v >= 0 else "") + money(v)

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
        if (yr := change_since(12)) is not None and (base := float(nw["net_worth"].iloc[-1]) - yr):
            notes.append(f"📈 Net worth is {'up' if yr >= 0 else 'down'} **{money(abs(yr))}** over the past year "
                         f"({yr / abs(base):+.0%}).")
        if own:
            notes.append(f"🏦 Debt is **{owe / own:.0%}** of what you own.")
        stale = insights.stale_accounts(accts)
        if len(stale):
            notes.append(f"⏰ {len(stale)} account(s) haven't been updated in 45+ days: "
                         + ", ".join(stale["name"]) + ". Update them in **Accounts**.")
        with st.container(border=True):
            for n in notes:
                st.markdown(n)

        st.markdown("#### Net worth over time")
        plot(charts.net_worth_history(nw, mode, compact=PHONE))

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

        fc = forecast.run(accts, a)
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
        todo = categorize.uncategorized_merchants(conn, txns, insights.is_transfer)
        if len(todo):
            with st.container(border=True):
                a1, a2 = st.columns([3, 1], vertical_alignment="center")
                a1.markdown(f"✨ **{len(todo)} merchants** ({int(todo['n'].sum())} transactions) haven't been "
                            "categorized by AI yet. Only cleaned merchant names and rough amounts are sent "
                            "(no account numbers, names or dates).")
                if a2.button("Categorize with AI", type="primary", width="stretch",
                             disabled=not categorize.available(),
                             help=None if categorize.available() else "Needs AppleConnect installed and signed in"):
                    run_ai_categorize()
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
        view = view[["date", "account", "description", "merchant", "category", "amount"]].reset_index(drop=True)

        spent, got = -view.loc[view["amount"] < 0, "amount"].sum(), view.loc[view["amount"] > 0, "amount"].sum()
        st.caption(f"**{len(view)}** transactions · in {money(got)} · out {money(spent)}  —  "
                   + ("✏️ **Tap a category twice to change it.** " if PHONE else
                      "✏️ **Double-click a category to change it.** ")
                   + "Every transaction from that merchant follows, including future imports.")

        # Keyed by the filters so pending edits never get applied to a differently-filtered table
        editor_key = f"txn_editor_{hash((q, tuple(pick_cats), tuple(pick), period))}"
        edited = st.data_editor(
            view, hide_index=True, width="stretch", height=420 if PHONE else 460, key=editor_key,
            disabled=["date", "account", "description", "merchant", "amount"],
            column_order=(["date", "description", "category", "amount"] if PHONE else
                          ["date", "account", "description", "category", "amount"]),
            column_config={
                "date": st.column_config.DateColumn("Date", format="MMM D" if PHONE else "MMM D, YYYY"),
                "account": "Account", "description": "Description",
                "category": st.column_config.SelectboxColumn("Category ✏️", options=cat_options, required=True),
                "amount": st.column_config.NumberColumn("Amount", format="$%,.0f" if PHONE else "$%,.2f")})
        changed = edited[edited["category"] != view["category"]]
        if len(changed):
            for r in changed.drop_duplicates("merchant", keep="last").itertuples():
                categorize.set_rule(conn, r.merchant, r.category, source="user")
            n = int(view["merchant"].isin(changed["merchant"]).sum())
            st.session_state["flash"] = f"Updated {n} transaction(s) from {', '.join(changed['merchant'].unique())}"
            del st.session_state[editor_key]
            st.rerun()
        if msg := st.session_state.pop("flash", None):
            st.toast(msg, icon="✅")


# --- accounts -----------------------------------------------------------------

with tab_accounts:
    if len(accts):
        table = accts.assign(
            Group=accts["type"].map(insights.GROUPS), Type=accts["type"].map(TYPE_LABELS),
            Updated=pd.to_datetime(accts["as_of"]),
        ).sort_values(["is_liability", "Group", "name"])
        st.dataframe(table[["name", "balance", "Updated"] if PHONE else ["name", "Group", "Type", "balance", "Updated"]],
                     hide_index=True, width="stretch", column_config={
                         "name": "Account",
                         "balance": st.column_config.NumberColumn("Balance", format="$%,.0f" if PHONE else "$%,.2f"),
                         "Updated": st.column_config.DateColumn(format="MMM D" if PHONE else "MMM D, YYYY")})
        st.caption("Debts show what you owe as a positive number.")

    names = dict(zip(accts["name"], accts["id"]))
    c1, c2 = st.columns(2, gap="large")
    with c1:
        if names:
            with st.form("balance", clear_on_submit=True, border=True):
                st.markdown("**Update a balance**")
                st.caption("For anything without a CSV: mortgage, home value, car, 401k…")
                acct = st.selectbox("Account", list(names))
                when = st.date_input("As of", value=date.today())
                amt = st.number_input("Balance ($)", min_value=0.0, step=100.0)
                if st.form_submit_button("Save balance", type="primary"):
                    db.upsert_balance(conn, names[acct], when, amt)
                    st.rerun()

        loans = accts[accts["is_liability"]]
        if len(loans):
            with st.form("terms", border=True):
                st.markdown("**Loan details** (makes the forecast accurate)")
                ln = st.selectbox("Loan", list(loans["name"]))
                row = loans[loans["name"] == ln].iloc[0]
                apr = st.number_input("Interest rate (APR %)", min_value=0.0, max_value=40.0, step=0.125,
                                      value=float(row["rate"] * 100) if pd.notna(row["rate"]) else 0.0)
                pay = st.number_input("Monthly payment ($)", min_value=0.0, step=10.0,
                                      value=float(row["payment"]) if pd.notna(row["payment"]) else 0.0)
                if st.form_submit_button("Save loan details"):
                    db.update_account_terms(conn, int(row["id"]), apr / 100, pay or None)
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
                    db.delete_account(conn, names[gone])
                    st.rerun()


# --- add data -----------------------------------------------------------------

with tab_add:
    st.markdown("#### Import a file")
    with st.expander("How to download from Bank of America"):
        st.markdown(
            "- **Checking / savings**: open the account → *Download* → pick a date range → "
            "*Microsoft Excel format*. Saves a `.csv`.\n"
            "- **Credit card**: open the card → *Download transactions* → pick a statement → `.csv`.\n"
            "- **Merrill**: *Portfolio* → *Holdings* → the download icon → `.csv`.\n"
            "- **Mortgage / auto loan / HELOC**: no download. Type the balance into **Accounts**.\n\n"
            "Overlapping date ranges are fine. Duplicates are skipped automatically.")
    files = st.file_uploader("Drop CSV files here", type=["csv"], accept_multiple_files=True)
    for f in files or []:
        with st.container(border=True):
            try:
                parsed = parse_file(f.getvalue())
            except UnrecognizedFile:
                st.error(f"**{f.name}**: this doesn't look like a Bank of America or Merrill export. "
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
            target = st.selectbox("Import into", options, key=f"t{k}")
            new_name = new_type = None
            if target == options[-1]:
                n1, n2 = st.columns(2)
                new_name = n1.text_input("Account name", key=f"n{k}", placeholder="e.g. BofA Checking")
                new_type = n2.selectbox("Type", allowed, format_func=TYPE_LABELS.get, key=f"y{k}")
            if parsed.kind == "credit_card":
                st.caption("Card files don't include a balance. Add the current balance in **Accounts** afterwards.")
            if st.button("Import", key=f"b{k}", type="primary", disabled=target == options[-1] and not new_name):
                acct_id = (db.get_or_create_account(conn, new_name, parsed.institution, new_type) if new_name
                           else int(matches.loc[matches["name"] == target, "id"].iloc[0]))
                r = apply(conn, parsed, acct_id, f.name)
                st.success(f"Added {r['transactions_added']} transactions"
                           + (f" ({r['transactions_skipped']} already there)" if r["transactions_skipped"] else "")
                           + (f", {r['positions']} positions" if r["positions"] else "")
                           + (f", {r['balances']} balance points" if r["balances"] else "") + ".")
                if r["transactions_added"] and categorize.available():
                    txns = db.transactions(conn)   # include what was just imported
                    run_ai_categorize()
