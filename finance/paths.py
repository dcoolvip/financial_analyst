"""Where personal data lives: outside ~/Documents on purpose, because Documents is often synced
to iCloud Drive. ~/Library/Application Support is the standard Mac location for app data and is
never synced. Override with FINANCE_DATA_DIR (tests, other machines).

The legacy <repo>/data folder is used only if it still exists and the new one doesn't
(i.e. before migrating), so nothing breaks mid-move.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEGACY_DIR = ROOT / "data"
DEFAULT_DIR = Path.home() / "Library" / "Application Support" / "FinancialAnalyst"


def data_dir() -> Path:
    if os.environ.get("FINANCE_DATA_DIR"):
        return Path(os.environ["FINANCE_DATA_DIR"])
    if LEGACY_DIR.is_dir() and not DEFAULT_DIR.is_dir():
        return LEGACY_DIR
    return DEFAULT_DIR


def write_private(path: Path, data: dict) -> None:
    """Write JSON readable only by this Mac user (API keys, access tokens)."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # private from the first byte
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)
