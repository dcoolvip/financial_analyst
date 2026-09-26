import time

import pytest

from finance import checkpoints, db
from finance.importers import apply, parse_file
from tests.test_bofa import read


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "finance.db")


def test_restore_undoes_a_wrong_import(conn):
    good = db.add_account(conn, "BofA Checking", "Bank of America", "checking")
    apply(conn, parse_file(read("bofa_checking.csv")), good, "bofa.csv")
    before = (len(db.transactions(conn)), db.accounts(conn)["name"].tolist())

    cid = checkpoints.create(conn, "Before importing chase.csv into BofA Checking")
    apply(conn, parse_file(read("chase_checking.csv")), good, "chase.csv")          # the mistake
    assert len(db.transactions(conn)) > before[0]

    checkpoints.restore(conn, cid)
    assert (len(db.transactions(conn)), db.accounts(conn)["name"].tolist()) == before


def test_restore_can_itself_be_undone(conn):
    db.add_account(conn, "A", "Bank of America", "checking")
    first = checkpoints.create(conn, "Before adding B")
    db.add_account(conn, "B", "Bank of America", "checking")
    undo = checkpoints.restore(conn, first)
    assert db.accounts(conn)["name"].tolist() == ["A"]
    checkpoints.restore(conn, undo)                                                  # changed my mind
    assert sorted(db.accounts(conn)["name"]) == ["A", "B"]


def test_list_is_newest_first_and_labelled(conn):
    checkpoints.create(conn, "Before importing one.csv")
    time.sleep(0.01)
    checkpoints.create(conn, "Before importing two.csv")
    cps = checkpoints.list_all(conn)
    assert [c["label"] for c in cps] == ["Before importing two.csv", "Before importing one.csv"]
    assert set(cps[0]["counts"]) == {"accounts", "transactions", "balances"}


def test_old_checkpoints_are_pruned(conn, monkeypatch):
    monkeypatch.setattr(checkpoints, "KEEP", 3)
    for i in range(5):
        checkpoints.create(conn, f"cp {i}")
        time.sleep(0.005)
    assert [c["label"] for c in checkpoints.list_all(conn)] == ["cp 4", "cp 3", "cp 2"]


def test_checkpoints_live_next_to_the_data_not_in_the_repo(conn, tmp_path):
    checkpoints.create(conn, "x")
    assert list((tmp_path / "checkpoints" / "finance").glob("*.db"))
