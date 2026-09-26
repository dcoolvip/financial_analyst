"""Checkpoints: a full snapshot of the database taken automatically before every import (and other
risky changes), so a wrong import - say, a Chase file saved into a BofA account - can be undone.

Snapshots use SQLite's online backup, so they're consistent even while the dashboard is running.
Restoring first snapshots the current state, so a restore can itself be undone.
Stored next to the data (never in the repo): <data folder>/checkpoints/<database name>/.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

KEEP = 30


def _dir(conn) -> Path:
    db_file = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    d = db_file.parent / "checkpoints" / db_file.stem
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d.parent, 0o700)
    return d


def create(conn, label: str) -> str:
    """Snapshot the whole database now. Returns the checkpoint id."""
    d = _dir(conn)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:50]
    cid = f"{stamp}__{slug}"
    dest = sqlite3.connect(d / f"{cid}.db")
    try:
        conn.backup(dest)
    finally:
        dest.close()
    os.chmod(d / f"{cid}.db", 0o600)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("accounts", "transactions", "balances")}
    (d / f"{cid}.json").write_text(json.dumps({"label": label, "created": time.time(), "counts": counts}))
    for old in list_all(conn)[KEEP:]:
        (d / f"{old['id']}.db").unlink(missing_ok=True)
        (d / f"{old['id']}.json").unlink(missing_ok=True)
    return cid


def list_all(conn) -> list[dict]:
    """Newest first: [{id, label, created, counts}]"""
    d = _dir(conn)
    out = []
    for meta in d.glob("*.json"):
        if (d / f"{meta.stem}.db").exists():
            out.append({"id": meta.stem, **json.loads(meta.read_text())})
    return sorted(out, key=lambda c: c["created"], reverse=True)


def restore(conn, cid: str) -> str:
    """Put the database back exactly as it was at checkpoint `cid` (in place, so every open page sees it).
    Snapshots the current state first and returns that new checkpoint's id."""
    d = _dir(conn)
    src_path = d / f"{cid}.db"
    if not src_path.exists():
        raise FileNotFoundError("That checkpoint no longer exists")
    label = json.loads((d / f"{cid}.json").read_text())["label"]
    undo_id = create(conn, f"Before restoring: {label}")
    src = sqlite3.connect(src_path)
    try:
        src.backup(conn)
    finally:
        src.close()
    return undo_id
