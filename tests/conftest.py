"""For every test: never start a real browser bank pull, never read or change the Mac's real monthly schedule."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_bank_pulls(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("FINANCE_NO_BANK_PULL", "1")
    monkeypatch.setenv("FINANCE_LAUNCH_AGENTS", str(tmp_path_factory.mktemp("LaunchAgents")))
