"""Gate tests, run in-process (no sockets): the real middleware + routes + handlers,
and a software passkey authenticator that produces genuine WebAuthn signatures."""
import asyncio
import base64
import hashlib
import json
import os
from unittest import mock

import cbor2
import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

HOST, PORT = "dcool.home", 8501
ORIGIN = f"https://{HOST}:{PORT}"
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_AUTH_DIR", str(tmp_path))
    salt = os.urandom(16)
    (tmp_path / "app_password").write_text(salt.hex() + ":" +
                                           hashlib.scrypt(b"correct horse", salt=salt, n=2**14, r=8, p=1).hex())
    from finance import gate as g
    monkeypatch.setattr(g.asyncio, "sleep", mock.AsyncMock())   # skip the failed-login delay
    app = g.make_app(HOST, PORT, 8502)
    app.freeze()   # what the real server does at startup; installs the middleware chain
    return g, app


def call(app, method, path, *, body=None, form=None, cookies=None, origin=ORIGIN, host=f"{HOST}:{PORT}", ua=UA_IPHONE,
         extra=None):
    async def run():
        headers = {"Host": host, "User-Agent": ua, **(extra or {})}
        if origin:
            headers["Origin"] = origin
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        data = b""
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        elif form is not None:
            from urllib.parse import urlencode
            data, headers["Content-Type"] = urlencode(form).encode(), "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(data))
        payload = streams.StreamReader(mock.Mock(_reading_paused=False), 2**16, loop=asyncio.get_running_loop())
        payload.feed_data(data)
        payload.feed_eof()
        req = make_mocked_request(method, path, headers=headers, app=app, payload=payload)
        try:
            return await app._handle(req)
        except web.HTTPException as e:
            return e
    return asyncio.run(run())


def session_cookie(resp) -> str:
    return resp.cookies["__Host-fa_session"].value


def login(app, remember=True):
    form = {"username": "finance", "password": "correct horse"}
    if remember:
        form["remember"] = "on"
    return call(app, "POST", "/login", form=form)


# --- password sign-in & sessions ---------------------------------------------------

def test_unauthenticated_requests_are_blocked(gate):
    _, app = gate
    assert call(app, "GET", "/").headers["Location"] == "/login"
    assert call(app, "GET", "/_stcore/stream").status == 401          # Streamlit endpoints never leak
    assert call(app, "POST", "/_stcore/upload_file/x", body={}).status == 401


def test_raw_ip_redirects_to_canonical_name(gate):
    _, app = gate
    r = call(app, "GET", "/login", host="192.168.50.136:8501")
    assert r.status == 308 and r.headers["Location"] == f"{ORIGIN}/login"


def test_password_login_sets_hardened_cookie(gate):
    _, app = gate
    r = login(app)
    assert r.status == 303
    c = r.cookies["__Host-fa_session"]
    assert c["secure"] and c["httponly"] and c["samesite"] == "Lax" and c["path"] == "/"
    assert int(c["max-age"]) == 90 * 24 * 3600
    assert call(app, "GET", "/security", cookies={"__Host-fa_session": c.value}).status == 200


def test_not_remembered_is_a_browser_session_cookie(gate):
    _, app = gate
    assert not login(app, remember=False).cookies["__Host-fa_session"].get("max-age")   # no expiry: ends with the browser session


def test_wrong_password_and_rate_limit(gate):
    _, app = gate
    for _ in range(5):
        r = call(app, "POST", "/login", form={"password": "nope"})
        assert "Wrong password" in r.text and "__Host-fa_session" not in r.cookies
    r = login(app)   # correct password, but locked out now
    assert "Too many attempts" in r.text and "__Host-fa_session" not in r.cookies


def test_cross_site_posts_rejected(gate):
    _, app = gate
    assert call(app, "POST", "/login", form={"password": "correct horse"}, origin="https://evil.example").status == 403
    assert call(app, "POST", "/passkey/auth/options", body={}, origin=None).status == 403


def test_browser_form_post_with_null_origin(gate):
    """What Safari/Chrome really send for a form post under strict referrer policies (the bug
    the user hit): Origin: null, but Sec-Fetch-Site proves it came from this site."""
    _, app = gate
    form = {"username": "finance", "password": "correct horse", "remember": "on"}
    for origin in ("null", None):
        r = call(app, "POST", "/login", form=form, origin=origin, extra={"Sec-Fetch-Site": "same-origin"})
        assert r.status == 303 and "__Host-fa_session" in r.cookies
    # still blocked when the browser says the post came from another site, or gives no proof
    assert call(app, "POST", "/login", form=form, origin="null", extra={"Sec-Fetch-Site": "cross-site"}).status == 403
    assert call(app, "POST", "/login", form=form, origin="null").status == 403


def test_referrer_policy_keeps_origin_header():
    from finance import gate as g
    assert g.SECURITY_HEADERS["Referrer-Policy"] == "same-origin"   # no-referrer caused Origin: null


def test_logout_and_revocation(gate):
    g, app = gate
    a = session_cookie(login(app))
    b = session_cookie(login(app))
    assert call(app, "POST", "/sessions/revoke", body={"others": True}, cookies={"__Host-fa_session": a}).status == 200
    assert call(app, "GET", "/security", cookies={"__Host-fa_session": b}).status == 302   # b signed out
    assert call(app, "GET", "/security", cookies={"__Host-fa_session": a}).status == 200
    call(app, "GET", "/logout", cookies={"__Host-fa_session": a})
    assert call(app, "GET", "/security", cookies={"__Host-fa_session": a}).status == 302


def test_tokens_stored_only_as_hashes(gate, tmp_path):
    _, app = gate
    token = session_cookie(login(app))
    assert token.encode() not in (tmp_path / "auth.db").read_bytes()


# --- passkeys, with a software authenticator ---------------------------------------

class SoftAuthenticator:
    """Behaves like Face ID's platform authenticator: ES256 key, user verified."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0
        self.user_handle = None

    def _client_data(self, kind, options):
        return json.dumps({"type": kind, "challenge": options["challenge"], "origin": ORIGIN,
                           "crossOrigin": False}).encode()

    def create(self, options):
        self.user_handle = options["user"]["id"]
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth_data = (hashlib.sha256(options["rp"]["id"].encode()).digest() + bytes([0x45]) + (0).to_bytes(4, "big")
                     + bytes(16) + len(self.cred_id).to_bytes(2, "big") + self.cred_id + cose)
        cd = self._client_data("webauthn.create", options)
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "clientExtensionResults": {}, "authenticatorAttachment": "platform",
                "response": {"clientDataJSON": b64u(cd), "transports": ["internal", "hybrid"],
                             "attestationObject": b64u(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data}))}}

    def get(self, options, rp_id=HOST):
        self.count += 1
        auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([0x05]) + self.count.to_bytes(4, "big")
        cd = self._client_data("webauthn.get", options)
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "clientExtensionResults": {}, "authenticatorAttachment": "platform",
                "response": {"clientDataJSON": b64u(cd), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig), "userHandle": self.user_handle}}


def register(app, cookie, authr):
    r = call(app, "POST", "/passkey/register/options", body={}, cookies={"__Host-fa_session": cookie})
    opts = json.loads(r.text)
    options = json.loads(opts["options"])
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["authenticatorSelection"]["residentKey"] == "required"
    return call(app, "POST", "/passkey/register/verify", cookies={"__Host-fa_session": cookie},
                body={"flow": opts["flow"], "credential": authr.create(options), "name": "iPhone"})


def passkey_login(app, authr, **kw):
    opts = json.loads(call(app, "POST", "/passkey/auth/options", body={}).text)
    cred = authr.get(json.loads(opts["options"]), **kw)
    return call(app, "POST", "/passkey/auth/verify", body={"flow": opts["flow"], "credential": cred, "remember": True})


def test_passkey_register_then_sign_in(gate):
    _, app = gate
    cookie = session_cookie(login(app))
    authr = SoftAuthenticator()
    assert register(app, cookie, authr).status == 200
    r = passkey_login(app, authr)
    assert r.status == 200 and json.loads(r.text)["ok"]
    assert call(app, "GET", "/security", cookies={"__Host-fa_session": session_cookie(r)}).status == 200
    assert "Face ID" in call(app, "GET", "/login").text   # passkey button now offered


def test_passkey_registration_requires_sign_in(gate):
    _, app = gate
    assert call(app, "POST", "/passkey/register/options", body={}).status == 401


def test_unknown_or_forged_passkeys_rejected(gate):
    _, app = gate
    cookie = session_cookie(login(app))
    good = SoftAuthenticator()
    register(app, cookie, good)
    stranger = SoftAuthenticator()                               # never registered
    assert passkey_login(app, stranger).status == 401
    impostor = SoftAuthenticator()                               # same credential id, different key
    impostor.cred_id, impostor.user_handle = good.cred_id, good.user_handle
    assert passkey_login(app, impostor).status == 401
    assert passkey_login(app, good, rp_id="evil.example").status == 401   # signed for another site


def test_challenges_are_single_use(gate):
    _, app = gate
    cookie = session_cookie(login(app))
    authr = SoftAuthenticator()
    register(app, cookie, authr)
    opts = json.loads(call(app, "POST", "/passkey/auth/options", body={}).text)
    cred = authr.get(json.loads(opts["options"]))
    body = {"flow": opts["flow"], "credential": cred}
    assert call(app, "POST", "/passkey/auth/verify", body=body).status == 200
    assert call(app, "POST", "/passkey/auth/verify", body=body).status == 400   # replay fails


def test_security_headers_on_streamed_responses(gate):
    """Streamlit pages are streamed through; their headers are sent at prepare() time,
    so the security headers must be attached then (this is what the live self-test caught)."""
    _, app = gate

    async def run():
        req = make_mocked_request("GET", "/", headers={"Host": f"{HOST}:{PORT}"}, app=app)
        resp = web.StreamResponse()
        await resp.prepare(req)
        return resp
    resp = asyncio.run(run())
    assert resp.headers["Strict-Transport-Security"] == "max-age=31536000"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
