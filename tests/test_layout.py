"""Guards against personal data or the virtualenv drifting back into the repo folder,
which lives under ~/Documents and is iCloud-synced on this Mac."""
import os
import re
from pathlib import Path

from finance import paths

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [ROOT / "Dashboard.command", ROOT / "Dashboard (Wi-Fi).command", *ROOT.glob("tools/*.sh"),
           *ROOT.glob("tools/*.py")]


def test_default_data_dir_is_outside_documents(monkeypatch):
    monkeypatch.delenv("FINANCE_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "LEGACY_DIR", Path("/nonexistent"))
    d = paths.data_dir()
    assert "Documents" not in d.parts and d == Path.home() / "Library/Application Support/FinancialAnalyst"


def test_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    assert paths.data_dir() == tmp_path


def test_scripts_never_use_in_repo_venv_or_data():
    for f in SCRIPTS:
        text = f.read_text()
        if f.name == "env.sh":   # only mentions .venv to clean up the old one
            text = re.sub(r"(?s)# Remove the old in-repo .venv.*?\nfi\n", "", text)
        assert ".venv/bin" not in text, f"{f.name} uses the in-repo .venv"
        assert not re.search(r'(?<![\w$/])data/(tls|app_password|finance\.db|auth\.db)', text), \
            f"{f.name} hardcodes the old in-repo data/ folder"


def test_all_launchers_share_env_setup():
    for f in (ROOT / "Dashboard.command", ROOT / "Dashboard (Wi-Fi).command", ROOT / "tools/make_tls.sh"):
        assert "source tools/env.sh" in f.read_text(), f.name
    assert '$HOME/.venvs/financial-analyst' in (ROOT / "tools/env.sh").read_text()
    assert os.access(ROOT / "tools/make_tls.sh", os.X_OK)
