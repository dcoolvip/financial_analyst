# financial_analyst

A private, local-only dashboard for net worth, cash flow and a 10-year forecast.
It's built with Python, SQLite and Streamlit. Your data never leaves this machine and is never committed.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Open http://localhost:8501. Or just double-click **`Dashboard.command`** in Finder: it sets
things up on first run, opens the browser, and stops when you close its Terminal window. Switch to **Demo data** in the sidebar to look around with a fake household.

## Other devices on your Wi-Fi (HTTPS, passkeys)

Double-click **`Dashboard (Wi-Fi).command`** → `https://dcool.home:8501`.

```
phone/Mac ──HTTPS──▶ gate (Wi-Fi IP:8501) ──▶ Streamlit (127.0.0.1:8502, this Mac only)
```

`finance/gate.py` handles sign-in: a password form that Passwords/Face ID can fill, **passkeys**
(Face ID / Touch ID), "keep me signed in on this device" (90 days), and a **Security** page
(sidebar → 🔐) to add or remove passkeys and sign out other devices. Sessions are HttpOnly/Secure `__Host-`
cookies; only their hashes are stored (`data/auth.db`). Logins are rate-limited, and every POST is
Origin-checked.

First run: it asks for a password, creates a private certificate authority in `data/tls/`
(gitignored) and shows `Financial Analyst CA.cer` in Finder. To trust it once on the iPhone:
AirDrop it → Settings → General → VPN & Device Management → Install → Settings → General → About →
Certificate Trust Settings → turn it on. The CA is name-constrained to `dcool.home` and the home subnet,
so it can't vouch for any other site.

Check the whole chain end to end: `.venv/bin/python tools/selftest_gate.py`

The regular `Dashboard.command` stays on `http://localhost`, and that traffic never leaves the Mac.

## Getting your data in (Bank of America)

| Account | How |
|---|---|
| Checking / savings | Account → *Download* → *Microsoft Excel format* (.csv) → **Add data** tab |
| Credit card | *Download transactions* → .csv → **Add data**, then enter current balance in **Accounts** |
| Merrill brokerage / IRA | *Holdings* → download .csv → **Add data** |
| Mortgage, auto loan, HELOC | No export: type balance + rate + payment in **Accounts** |
| Home, car | Enter an estimate in **Accounts** (Zillow / KBB) every few months |

Re-importing overlapping files is safe because duplicates are skipped.

## Layout

```
app.py                  UI only (tabs: Overview, Future, Money in & out, Accounts, Add data)
finance/db.py           SQLite schema + queries (data/finance.db, gitignored)
finance/importers/      one parser per institution -> common ParsedFile shape
finance/insights.py     grouping, transfer detection, categories, savings estimate
finance/forecast.py     Monte Carlo net-worth projection with loan amortization
finance/charts.py       Plotly figures, one consistent palette
finance/demo.py         synthetic data, imported through the real parsers
tests/                  pytest; fixtures are fake data
```

## Adding a source later

Write a module in `finance/importers/` with `parse(content) -> ParsedFile` and add it to
`IMPORTERS`. An API-based source (SimpleFIN Bridge, Teller) returns the same `ParsedFile`,
so the rest of the app doesn't change.

## Tests

```bash
.venv/bin/python -m pytest -q
```
