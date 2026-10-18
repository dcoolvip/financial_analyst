"""The monthly round: a re-run after a failure only does what didn't finish; cars and the Amex balance."""
import importlib.util
from datetime import date
from pathlib import Path

from finance import assets, bank_pull, db


def test_round_starts_on_the_schedule_day():
    assert bank_pull.round_start(date(2026, 10, 28)) == date(2026, 10, 28)
    assert bank_pull.round_start(date(2026, 11, 3)) == date(2026, 10, 28)
    assert bank_pull.round_start(date(2026, 1, 5)) == date(2025, 12, 28)


def test_rerun_picks_up_only_what_failed_or_did_not_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bank_pull, "set_up", lambda b: b in {"bofa", "chase", "amex"})
    monkeypatch.setattr(bank_pull, "extra_available", lambda s: False)
    day = date(2026, 10, 28)
    assert bank_pull.left_this_round(day) == ["bofa", "chase", "amex"]
    bank_pull.mark_round("bofa", True, today=day)
    bank_pull.mark_round("chase", False, "couldn't sign in", today=day)
    assert bank_pull.left_this_round(day) == ["chase", "amex"]                 # BofA isn't signed in to again
    assert bank_pull.round_state(day)["failed"] == {"chase": "couldn't sign in"}
    bank_pull.mark_round("chase", True, today=date(2026, 10, 29))              # fixed, run again the next day
    assert bank_pull.left_this_round(date(2026, 10, 30)) == ["amex"]
    assert bank_pull.left_this_round(date(2026, 11, 28)) == ["bofa", "chase", "amex"]   # next month: a new round


def test_monthly_runner_skips_what_is_done_and_remembers_failures(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("monthly_pull", Path(__file__).parent.parent / "tools/monthly_pull.py")
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bank_pull, "set_up", lambda b: b in {"bofa", "chase", "amex"})
    monkeypatch.setattr(bank_pull, "extra_available", lambda s: False)
    ran = []

    class Done:
        def __init__(self, rc):
            self.returncode = rc

    def fake_run(cmd, cwd=None):
        bank = next(b for b in ("bofa", "chase", "amex") if f"{b}_fetch.py" in cmd[1])
        ran.append(bank)
        return Done(2 if bank == "chase" and ran.count("chase") == 1 else 0)    # Chase fails the first time
    monkeypatch.setattr(mp.subprocess, "run", fake_run)
    mp.main([])
    assert ran == ["bofa", "chase", "amex"]
    mp.main([])                                                                # after the fix: only Chase
    assert ran == ["bofa", "chase", "amex", "chase"]
    mp.main([])                                                                # all done: nothing runs
    assert len(ran) == 4
    mp.main(["--dry-run"])                                                     # a test run does everything
    assert len(ran) == 7 and bank_pull.left_this_round() == []


def test_cars_carry_on_from_your_last_value_at_their_own_pace(tmp_path, monkeypatch):
    assert assets.vehicle_purchase("2023 IONIQ 6, bought new Sep 6, 2023 for $60,000") == (date(2023, 9, 6), 60_000.0)
    assert assets.vehicle_purchase("no purchase here") is None
    ev = assets.vehicle_forward(26_940, date(2026, 9, 26), date(2023, 9, 6), date(2027, 9, 30), 60_000)
    usual = assets.vehicle_forward(26_940, date(2026, 9, 26), date(2023, 9, 6), date(2027, 9, 30))
    assert ev[0][0] == date(2026, 9, 30) and ev[-1][0] == date(2027, 9, 30)
    assert ev[-1][1] < usual[-1][1] < 26_940                                   # it has lost value faster than usual
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    car = db.add_account(conn, "Car", "Maker", "vehicle", "bought new Nov 15, 2019 for $27,000")
    db.upsert_balance(conn, car, "2026-09-26", 8_910)                          # the value you typed
    assert assets.roll_forward_vehicles(conn, date(2026, 11, 2)) == 2          # Sep 30, Oct 31
    db.upsert_balance(conn, car, "2026-11-05", 9_500)                          # a new typed value wins
    assert assets.roll_forward_vehicles(conn, date(2026, 12, 1)) == 1
    got = conn.execute("SELECT date, balance FROM balances WHERE account_id = ? ORDER BY date", (car,)).fetchall()
    assert got[-1][0] == "2026-11-30" and 9_300 < got[-1][1] < 9_500


def test_amex_balance_is_read_off_the_dashboard(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("amex_fetch_b", Path(__file__).parent.parent / "tools/amex_fetch.py")
    af = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(af)
    assert af.balance_from("Welcome\nTotal Balance\n$1,234.56\nStatement Balance\n$900.00") == (1_234.56, "Total Balance")
    assert af.balance_from("Current Balance  -$25.00 (credit)") == (-25.0, "Current Balance")
    assert af.balance_from("Nothing to see") is None
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    card = db.get_or_create_account(conn, "Amex", "American Express", "credit_card")
    assert af.save_balance(conn, None, date(2026, 10, 28)).endswith("nothing saved")
    assert af.save_balance(conn, (1_234.56, "Total Balance"), date(2026, 10, 28)) == \
        "Amex: balance saved (Oct 28, the dashboard's Total Balance)"
    assert conn.execute("SELECT balance FROM balances WHERE account_id = ?", (card,)).fetchone()[0] == 1_234.56
