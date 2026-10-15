"""Plaid keys and the which-banks-can-connect check. Never calls Plaid: the HTTP call is replaced."""
import io
import json
import stat
import urllib.error

import pytest

from finance import plaid


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    return tmp_path


def test_keys_are_private_to_this_user_and_removable(data):
    assert plaid.load_keys() is None
    plaid.save_keys(" cid123 ", "sekret9876\n")
    assert plaid.load_keys() == {"client_id": "cid123", "secret": "sekret9876", "env": "production"}
    assert stat.S_IMODE((data / "plaid.json").stat().st_mode) == 0o600
    plaid.forget_keys()
    assert plaid.load_keys() is None and not (data / "plaid.json").exists()


def test_check_sorts_oauth_from_direct_and_skips_hand_tracked_values(data, monkeypatch):
    sent = []
    answers = {"Chase": [{"name": "Chase", "oauth": True, "products": ["transactions", "liabilities", "auth"]}],
               "Golden 1": [{"name": "Golden 1 Credit Union", "oauth": False, "products": ["transactions"]}],
               "Nowhere Bank": []}

    def fake_post(endpoint, body, keys):
        sent.append((endpoint, body["query"], body["country_codes"]))
        return {"institutions": answers[body["query"]]}
    monkeypatch.setattr(plaid, "_post", fake_post)
    names = plaid.institutions_to_check(["Chase", "Golden 1 Credit Union", "Redfin estimate", "Physical",
                                         "Nowhere Bank", "Chase"])
    rows = plaid.check(names, {"client_id": "c", "secret": "s"})
    assert [q for _, q, _ in sent] == ["Chase", "Golden 1", "Nowhere Bank"]       # once each, searched by Plaid's name
    assert all(e == "/institutions/search" and c == ["US"] for e, _, c in sent)
    by = {r["yours"]: r for r in rows}
    assert by["Chase"]["oauth"] is True and by["Chase"]["products"] == ["transactions", "liabilities"]
    assert by["Golden 1 Credit Union"]["oauth"] is False
    assert by["Nowhere Bank"]["plaid_name"] is None
    assert plaid.last_check()["rows"] == rows                                       # kept for the screen


def test_plaid_errors_come_back_readable(monkeypatch):
    def refuse(req, timeout):
        body = json.dumps({"error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret provided"})
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body.encode()))
    monkeypatch.setattr(plaid.urllib.request, "urlopen", refuse)
    with pytest.raises(plaid.PlaidError, match="invalid client_id or secret provided"):
        plaid.search("Chase", {"client_id": "c", "secret": "s"})
