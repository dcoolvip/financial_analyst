"""HTTPS sign-in gate in front of the Streamlit dashboard (Wi-Fi mode).

    phone/Mac ──HTTPS──▶ gate (Wi-Fi IP:8501) ──▶ Streamlit (127.0.0.1:8502, not reachable from the network)

The gate owns everything security-related, because Streamlit can't do it properly itself:
  * Password sign-in on a real HTML form, so iOS/macOS Passwords can save it and Face ID can fill it.
  * Passkeys (Face ID / Touch ID) via WebAuthn, verified server-side with the `webauthn` library.
  * "Keep me signed in on this device": an HttpOnly, Secure, SameSite=Lax `__Host-` cookie holding a
    random token. Page scripts can't read it; other sites can't send it; only its hash is stored.
  * A Security page to add/remove passkeys and sign out other devices.
  * Login rate limiting, Origin checks on every POST, HSTS and anti-framing headers.
Everything else - including Streamlit's WebSocket - is passed through only with a valid session.

Run by "Dashboard (Wi-Fi).command":  python -m finance.gate --host dcool.home --ip <wifi ip>
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import secrets
import ssl
import time
import urllib.parse
from collections import defaultdict, deque

import aiohttp
from aiohttp import web
from webauthn import (base64url_to_bytes, generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response, verify_registration_response)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from . import auth_store as store

COOKIE = "__Host-fa_session"
PUBLIC = {"/login", "/passkey/auth/options", "/passkey/auth/verify", "/favicon.ico"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}
MAX_FAILS, FAIL_WINDOW_S = 5, 300
FLOW_TTL_S = 300

cfg = web.AppKey("cfg", dict)
db = web.AppKey("db", object)
client_key = web.AppKey("client", aiohttp.ClientSession)
flows_key = web.AppKey("flows", dict)
fails_key = web.AppKey("fails", defaultdict)


# --- helpers ----------------------------------------------------------------------

def _token(request) -> str | None:
    return request.cookies.get(COOKIE)


def _authed(request) -> dict | None:
    return store.valid_session(request.app[db], _token(request))


def _same_origin(request) -> bool:
    """CSRF check for every POST. Browsers send `Origin: null` on plain form posts in some cases
    (privacy settings, referrer policies), so a null/missing Origin is accepted only when the
    browser's own Sec-Fetch-Site says the request came from this site - pages can't forge it."""
    origin = request.headers.get("Origin")
    if origin == request.app[cfg]["origin"]:
        return True
    return origin in (None, "null") and request.headers.get("Sec-Fetch-Site") == "same-origin"


def _set_cookie(resp: web.StreamResponse, token: str, max_age: int | None) -> None:
    # Lax (not Strict) so opening the dashboard from a link in Messages/Notes keeps you signed in.
    # Cross-site POSTs and WebSockets still never carry it, and every POST is Origin-checked too.
    resp.set_cookie(COOKIE, token, max_age=max_age, path="/", secure=True, httponly=True, samesite="Lax")


def _redirect(location: str) -> web.Response:
    return web.Response(status=303, headers={"Location": location})


def _device(request) -> str:
    ua = request.headers.get("User-Agent", "")
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                         ("Macintosh", "Mac"), ("Windows", "Windows")):
        if needle in ua:
            browser = next((b for b in ("Edg", "Chrome", "Firefox", "Safari") if b in ua), "Browser")
            return f"{name} · {browser.replace('Edg', 'Edge')}"
    return ua[:60] or "Unknown device"


def _new_flow(request, kind: str, challenge: bytes) -> str:
    flows = request.app[flows_key]
    now = time.time()
    for k in [k for k, v in flows.items() if v[2] < now]:
        flows.pop(k, None)
    fid = secrets.token_urlsafe(16)
    flows[fid] = (kind, challenge, now + FLOW_TTL_S)
    return fid


def _take_flow(request, fid: str, kind: str) -> bytes | None:
    entry = request.app[flows_key].pop(fid or "", None)
    if not entry or entry[0] != kind or entry[2] < time.time():
        return None
    return entry[1]


def _rate_limited(request) -> bool:
    q = request.app[fails_key][request.remote]
    while q and q[0] < time.time() - FAIL_WINDOW_S:
        q.popleft()
    return len(q) >= MAX_FAILS


def _fail(request) -> None:
    request.app[fails_key][request.remote].append(time.time())


# --- pages ------------------------------------------------------------------------

STYLE = """
:root { color-scheme: light dark; --bg:#f9f9f7; --card:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --line:rgba(11,11,11,.12);
        --accent:#2a78d6; --danger:#d03b3b; }
@media (prefers-color-scheme: dark) { :root { --bg:#0d0d0d; --card:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
        --line:rgba(255,255,255,.12); --accent:#3987e5; } }
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--ink); }
main { max-width: 440px; margin: 8vh auto; padding: 0 20px; }
main.wide { max-width: 720px; margin-top: 4vh; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; } p.sub { color: var(--ink2); margin: 0 0 1.5rem; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 20px; margin-bottom: 16px; }
label { display:block; font-size:.85rem; color:var(--ink2); margin: 0 0 6px; }
input[type=text], input[type=password] { width:100%; font-size:17px; padding:12px; border-radius:10px;
        border:1px solid var(--line); background:transparent; color:var(--ink); margin-bottom:14px; }
.row { display:flex; align-items:center; gap:8px; margin: 2px 0 16px; font-size:.95rem; }
button, .btn { display:inline-flex; justify-content:center; align-items:center; gap:8px; width:100%; font-size:17px;
        font-weight:600; padding:12px 16px; border-radius:10px; border:1px solid var(--line); cursor:pointer;
        background:transparent; color:var(--ink); text-decoration:none; }
button.primary { background: var(--accent); color:#fff; border-color: transparent; }
button.small { width:auto; font-size:.9rem; padding:6px 12px; font-weight:500; }
button.danger { color: var(--danger); }
.or { text-align:center; color:var(--ink2); margin: 14px 0; font-size:.85rem; }
.msg { padding:10px 12px; border-radius:10px; margin-bottom:14px; font-size:.95rem; }
.msg.err { background: rgba(208,59,59,.12); color: var(--danger); } .msg.ok { background: rgba(12,163,12,.12); }
table { width:100%; border-collapse: collapse; font-size:.92rem; } td { padding:10px 4px; border-top:1px solid var(--line); vertical-align: middle; }
td.r { text-align:right; } .muted { color: var(--ink2); font-size:.85rem; } a { color: var(--accent); }
"""

B64_JS = """
const b64u = {
  enc: buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,''),
  dec: s => Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/') + '==='.slice((s.length+3)%4)), c => c.charCodeAt(0)).buffer,
};
function credToJSON(c) {
  if (c.toJSON) { try { return c.toJSON(); } catch (e) {} }
  const r = c.response, out = { id: c.id, rawId: b64u.enc(c.rawId), type: c.type,
    clientExtensionResults: c.getClientExtensionResults(), authenticatorAttachment: c.authenticatorAttachment, response: {
      clientDataJSON: b64u.enc(r.clientDataJSON) } };
  if (r.attestationObject) { out.response.attestationObject = b64u.enc(r.attestationObject);
    if (r.getTransports) out.response.transports = r.getTransports(); }
  if (r.authenticatorData) { out.response.authenticatorData = b64u.enc(r.authenticatorData);
    out.response.signature = b64u.enc(r.signature); if (r.userHandle) out.response.userHandle = b64u.enc(r.userHandle); }
  return out;
}
async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(body || {}), credentials: 'same-origin' });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
"""


def _page(title: str, body: str, nonce: str, wide: bool = False) -> web.Response:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>{STYLE}</style></head><body><main class="{'wide' if wide else ''}">{body}</main></body></html>"""
    resp = web.Response(text=doc, content_type="text/html")
    resp.headers["Content-Security-Policy"] = (f"default-src 'self'; script-src 'nonce-{nonce}'; "
                                               "style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _safe_next(n: str | None) -> str:
    return n if n and n.startswith("/") and not n.startswith("//") else "/"


async def login_page(request, error: str = "") -> web.Response:
    if _authed(request):
        return _redirect(_safe_next(request.query.get("next")))
    nonce = secrets.token_urlsafe(12)
    has_passkey = bool(store.passkeys(request.app[db]))
    nxt = html.escape(_safe_next(request.query.get("next")))
    passkey_btn = """<div class="or">or</div>
      <button type="button" id="pk">Sign in with Face ID / passkey</button>""" if has_passkey else ""
    body = f"""
<h1>🔒 Financial Analyst</h1><p class="sub">Sign in to see your dashboard.</p>
<div class="card">
  {f'<div class="msg err">{html.escape(error)}</div>' if error else ''}
  <div id="pkerr" class="msg err" hidden></div>
  <form method="post" action="/login?next={nxt}">
    <label for="u">Username</label>
    <input id="u" name="username" type="text" value="finance" autocomplete="username webauthn" autocapitalize="none">
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" autofocus required>
    <div class="row"><input id="r" name="remember" type="checkbox" checked>
      <label for="r" style="margin:0;color:inherit">Keep me signed in on this device (90 days)</label></div>
    <button class="primary" type="submit">Sign in</button>
  </form>
  {passkey_btn}
</div>
<script nonce="{nonce}">{B64_JS}
const next = {json.dumps(_safe_next(request.query.get("next")))};
let aborter = null;
async function passkeySignIn(conditional) {{
  if (aborter) aborter.abort();
  aborter = new AbortController();
  const {{ flow, options }} = await post('/passkey/auth/options');
  const o = JSON.parse(options);
  const publicKey = {{ ...o, challenge: b64u.dec(o.challenge),
    allowCredentials: (o.allowCredentials || []).map(c => ({{ ...c, id: b64u.dec(c.id) }})) }};
  const cred = await navigator.credentials.get({{ publicKey, signal: aborter.signal,
                                                 ...(conditional ? {{ mediation: 'conditional' }} : {{}}) }});
  const remember = document.getElementById('r').checked;
  await post('/passkey/auth/verify', {{ flow, credential: credToJSON(cred), remember }});
  location.replace(next);
}}
function showErr(e) {{ if (e.name === 'AbortError' || e.name === 'NotAllowedError') return;
  const el = document.getElementById('pkerr'); el.textContent = e.message; el.hidden = false; }}
const btn = document.getElementById('pk');
if (btn) btn.addEventListener('click', () => passkeySignIn(false).catch(showErr));
// Offer the passkey right in the keyboard's AutoFill bar when the browser supports it
if ({json.dumps(has_passkey)} && window.PublicKeyCredential && PublicKeyCredential.isConditionalMediationAvailable) {{
  PublicKeyCredential.isConditionalMediationAvailable().then(ok => ok && passkeySignIn(true).catch(showErr));
}}
</script>"""
    return _page("Sign in · Financial Analyst", body, nonce)


async def login_submit(request) -> web.Response:
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Bad origin")
    if _rate_limited(request):
        return await login_page(request, "Too many attempts. Wait a few minutes and try again.")
    form = await request.post()
    if not store.check_password(str(form.get("password", ""))):
        _fail(request)
        await asyncio.sleep(0.6)
        return await login_page(request, "Wrong password.")
    request.app[fails_key].pop(request.remote, None)
    token, max_age = store.create_session(request.app[db], remember=bool(form.get("remember")),
                                          device=_device(request), method="password")
    resp = _redirect(_safe_next(request.query.get("next")))
    _set_cookie(resp, token, max_age)
    return resp


async def logout(request) -> web.Response:
    store.end_session(request.app[db], _token(request))
    resp = _redirect("/login")
    resp.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
    return resp


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = time.time() - ts
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= n:
            v = int(s // n)
            return f"{v} {unit}{'s' if v != 1 else ''} ago"
    return "just now"


async def security_page(request) -> web.Response:
    conn, me = request.app[db], _authed(request)
    nonce = secrets.token_urlsafe(12)
    pk_rows = "".join(
        f"""<tr><td>🔑 {html.escape(p['name'])}<div class="muted">added {_ago(p['created'])} · last used {_ago(p['last_used'])}</div></td>
        <td class="r"><button class="small danger" data-del="{html.escape(p['id'])}">Remove</button></td></tr>"""
        for p in store.passkeys(conn)) or '<tr><td class="muted">No passkeys yet.</td></tr>'
    sess_rows = "".join(
        f"""<tr><td>{html.escape(s['device'])}{' <b>(this device)</b>' if s['id'] == me['id'] else ''}
        <div class="muted">{'passkey' if s['method'] == 'passkey' else 'password'} · active {_ago(s['last_seen'])} ·
        {'remembered 90 days' if s['remember'] else 'until browser closes (max 12h)'}</div></td>
        <td class="r">{'' if s['id'] == me['id'] else f'<button class="small danger" data-revoke="{s["id"]}">Sign out</button>'}</td></tr>"""
        for s in store.list_sessions(conn))
    body = f"""
<p><a href="/">← Back to dashboard</a></p>
<h1>Security</h1><p class="sub">Passkeys and signed-in devices.</p>
<div id="msg" class="msg" hidden></div>
<div class="card">
  <h3 style="margin-top:0">Face ID / passkeys</h3>
  <p class="muted">Sign in with Face ID or Touch ID instead of the password. The passkey stays on your device
  (synced by iCloud Keychain); only a public key is stored here. The password keeps working as a backup.</p>
  <table>{pk_rows}</table>
  <div style="margin-top:14px"><button class="primary" id="add">Add a passkey on this device</button></div>
</div>
<div class="card">
  <h3 style="margin-top:0">Signed-in devices</h3>
  <table>{sess_rows}</table>
  <div style="margin-top:14px;display:flex;gap:10px">
    <button id="others" class="danger">Sign out all other devices</button>
    <a class="btn" href="/logout">Sign out here</a></div>
</div>
<script nonce="{nonce}">{B64_JS}
const msg = (t, ok) => {{ const m = document.getElementById('msg'); m.textContent = t;
  m.className = 'msg ' + (ok ? 'ok' : 'err'); m.hidden = false; }};
document.getElementById('add').addEventListener('click', async () => {{
  try {{
    const {{ flow, options }} = await post('/passkey/register/options');
    const o = JSON.parse(options);
    const publicKey = {{ ...o, challenge: b64u.dec(o.challenge), user: {{ ...o.user, id: b64u.dec(o.user.id) }},
      excludeCredentials: (o.excludeCredentials || []).map(c => ({{ ...c, id: b64u.dec(c.id) }})) }};
    const cred = await navigator.credentials.create({{ publicKey }});
    const ua = navigator.userAgent, name = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad'
             : /Macintosh/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows Hello'
             : /Android/.test(ua) ? 'Android' : 'Passkey';
    await post('/passkey/register/verify', {{ flow, credential: credToJSON(cred), name }});
    location.reload();
  }} catch (e) {{ if (e.name !== 'NotAllowedError') msg('Could not add passkey: ' + e.message); }}
}});
document.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', async () => {{
  if (!confirm('Remove this passkey?')) return;
  await post('/passkey/delete', {{ id: b.dataset.del }}).then(() => location.reload()).catch(e => msg(e.message));
}}));
document.querySelectorAll('[data-revoke]').forEach(b => b.addEventListener('click', async () => {{
  await post('/sessions/revoke', {{ id: b.dataset.revoke }}).then(() => location.reload()).catch(e => msg(e.message));
}}));
document.getElementById('others').addEventListener('click', async () => {{
  if (!confirm('Sign out every other device?')) return;
  await post('/sessions/revoke', {{ others: true }}).then(() => location.reload()).catch(e => msg(e.message));
}});
</script>"""
    return _page("Security · Financial Analyst", body, nonce, wide=True)


# --- passkey API ------------------------------------------------------------------

def _json_error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


async def _json_body(request) -> dict:
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Bad origin")
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="Expected JSON")


async def register_options(request) -> web.Response:
    await _json_body(request)
    conn, c = request.app[db], request.app[cfg]
    opts = generate_registration_options(
        rp_id=c["rp_id"], rp_name="Financial Analyst", user_name="finance", user_display_name="Financial Analyst",
        user_id=store.user_handle(conn),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"]))
                             for p in store.passkeys(conn)],
    )
    return web.json_response({"flow": _new_flow(request, "register", opts.challenge),
                              "options": options_to_json(opts)})


async def register_verify(request) -> web.Response:
    body = await _json_body(request)
    challenge = _take_flow(request, body.get("flow"), "register")
    if not challenge:
        return _json_error("This request expired. Try again.")
    c = request.app[cfg]
    try:
        v = verify_registration_response(credential=body.get("credential"), expected_challenge=challenge,
                                         expected_rp_id=c["rp_id"], expected_origin=c["origin"],
                                         require_user_verification=True)
    except Exception as e:  # noqa: BLE001 - library raises several types; all mean "reject"
        return _json_error(f"Passkey rejected: {e}")
    store.add_passkey(request.app[db], bytes_to_base64url(v.credential_id), v.credential_public_key,
                      v.sign_count, str(body.get("name") or "Passkey"))
    return web.json_response({"ok": True})


async def auth_options(request) -> web.Response:
    await _json_body(request)
    if _rate_limited(request):
        return _json_error("Too many attempts. Wait a few minutes.", 429)
    opts = generate_authentication_options(rp_id=request.app[cfg]["rp_id"],
                                           user_verification=UserVerificationRequirement.REQUIRED)
    return web.json_response({"flow": _new_flow(request, "auth", opts.challenge), "options": options_to_json(opts)})


async def auth_verify(request) -> web.Response:
    body = await _json_body(request)
    challenge = _take_flow(request, body.get("flow"), "auth")
    if not challenge:
        return _json_error("This request expired. Try again.")
    conn, c = request.app[db], request.app[cfg]
    cred = body.get("credential") or {}
    pk = store.get_passkey(conn, str(cred.get("id", "")))
    if not pk:
        _fail(request)
        return _json_error("This passkey isn't registered here. Sign in with the password and add it on the "
                           "Security page.", 401)
    try:
        v = verify_authentication_response(credential=cred, expected_challenge=challenge, expected_rp_id=c["rp_id"],
                                           expected_origin=c["origin"], credential_public_key=pk["public_key"],
                                           credential_current_sign_count=pk["sign_count"],
                                           require_user_verification=True)
    except Exception as e:  # noqa: BLE001
        _fail(request)
        return _json_error(f"Passkey rejected: {e}", 401)
    store.touch_passkey(conn, pk["id"], v.new_sign_count)
    token, max_age = store.create_session(conn, remember=bool(body.get("remember")), device=_device(request),
                                          method="passkey")
    resp = web.json_response({"ok": True})
    _set_cookie(resp, token, max_age)
    return resp


async def passkey_delete(request) -> web.Response:
    body = await _json_body(request)
    store.delete_passkey(request.app[db], str(body.get("id", "")))
    return web.json_response({"ok": True})


async def sessions_revoke(request) -> web.Response:
    body = await _json_body(request)
    if body.get("others"):
        store.revoke_others(request.app[db], _token(request))
    else:
        store.revoke_session(request.app[db], str(body.get("id", "")))
    return web.json_response({"ok": True})


# --- pass-through to Streamlit ----------------------------------------------------

def _forward_headers(request) -> dict:
    h = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP | {"cookie"}
         and not k.lower().startswith("sec-websocket")}
    # Keep Streamlit's own cookies (XSRF), never hand it our session token
    cookies = "; ".join(f"{k}={v}" for k, v in request.cookies.items() if k != COOKIE)
    if cookies:
        h["Cookie"] = cookies
    h["Host"] = request.host
    h["X-Forwarded-Proto"] = "https"
    h["X-Forwarded-For"] = request.remote or ""
    return h


async def proxy(request) -> web.StreamResponse:
    upstream = request.app[cfg]["upstream"]
    client = request.app[client_key]
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _proxy_ws(request, upstream, client)
    try:
        async with client.request(request.method, upstream + request.path_qs, headers=_forward_headers(request),
                                  data=await request.read() if request.can_read_body else None,
                                  allow_redirects=False) as up:
            resp = web.StreamResponse(status=up.status, reason=up.reason)
            for k, v in up.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    resp.headers.add(k, v)
            await resp.prepare(request)
            async for chunk in up.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except aiohttp.ClientConnectorError:
        nonce = secrets.token_urlsafe(8)
        r = _page("Starting…", f'<h1>Starting up…</h1><p class="sub">The dashboard is starting. This page will '
                               f'refresh.</p><script nonce="{nonce}">setTimeout(() => location.reload(), 2000)</script>',
                  nonce)
        r.set_status(503)
        return r


async def _proxy_ws(request, upstream: str, client: aiohttp.ClientSession) -> web.WebSocketResponse:
    offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    ws = web.WebSocketResponse(protocols=offered, max_msg_size=0, autoping=True)
    await ws.prepare(request)
    try:
        async with client.ws_connect(upstream.replace("http", "ws", 1) + request.path_qs, protocols=offered,
                                     headers=_forward_headers(request), max_msg_size=0, autoping=True) as up:
            async def pump(src, dst):
                async for msg in src:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    else:
                        break
            tasks = [asyncio.create_task(pump(ws, up)), asyncio.create_task(pump(up, ws))]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except aiohttp.ClientError:
        pass
    finally:
        await ws.close()
    return ws


# --- app wiring -------------------------------------------------------------------

@web.middleware
async def guard(request, handler):
    c = request.app[cfg]
    if request.host != c["origin"].split("://", 1)[1]:
        # One canonical name: passkeys and cookies are bound to it (and it blocks DNS-rebinding tricks)
        return web.Response(status=308, headers={"Location": c["origin"] + request.path_qs})
    public = request.path in PUBLIC
    if not public and not _authed(request):
        if request.path.startswith("/_stcore") or request.headers.get("Upgrade", "").lower() == "websocket" \
                or request.method != "GET":
            resp = web.Response(status=401, text="Sign in required")
        else:
            nxt = request.path_qs if request.path != "/" else "/"
            return web.Response(status=302, headers={
                "Location": f"/login?next={urllib.parse.quote(nxt, safe='')}" if nxt != "/" else "/login"})
    else:
        resp = await handler(request)
    return resp


SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    # same-origin, not no-referrer: no-referrer makes browsers send `Origin: null` on form posts,
    # which broke password sign-in. Nothing is ever sent to other sites either way.
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "SAMEORIGIN",
}


async def _add_security_headers(_request, response) -> None:
    """Runs just before headers go out, for EVERY response - including streamed Streamlit
    pass-throughs, whose headers are already sent by the time a middleware sees them."""
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)


async def _favicon(_request):
    return web.Response(status=204)


def make_app(host: str, port: int, upstream_port: int) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=250 * 1024 * 1024)
    app[cfg] = {"rp_id": host, "origin": f"https://{host}:{port}" if port != 443 else f"https://{host}",
                "upstream": f"http://127.0.0.1:{upstream_port}"}
    app[db] = store.connect()
    app[flows_key] = {}
    app[fails_key] = defaultdict(deque)

    async def client_ctx(app_):
        app_[client_key] = aiohttp.ClientSession(auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None))
        yield
        await app_[client_key].close()
    app.cleanup_ctx.append(client_ctx)
    app.on_response_prepare.append(_add_security_headers)

    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_submit)
    app.router.add_get("/logout", logout)
    app.router.add_get("/security", security_page)
    app.router.add_get("/favicon.ico", _favicon)
    app.router.add_post("/passkey/register/options", register_options)
    app.router.add_post("/passkey/register/verify", register_verify)
    app.router.add_post("/passkey/auth/options", auth_options)
    app.router.add_post("/passkey/auth/verify", auth_verify)
    app.router.add_post("/passkey/delete", passkey_delete)
    app.router.add_post("/sessions/revoke", sessions_revoke)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="dcool.home", help="name devices use (must match the certificate)")
    ap.add_argument("--ip", required=True, help="address to listen on (the Wi-Fi IP)")
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--upstream-port", type=int, default=8502)
    ap.add_argument("--cert", default="data/tls/server.pem")
    ap.add_argument("--key", default="data/tls/server.key")
    a = ap.parse_args()
    if not store.password_is_set():
        raise SystemExit("No password set. Start via 'Dashboard (Wi-Fi).command'.")
    tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(a.cert, a.key)
    web.run_app(make_app(a.host, a.port, a.upstream_port), host=a.ip, port=a.port, ssl_context=tls,
                access_log=None, print=lambda *_: print(f"Gate listening on https://{a.host}:{a.port}"))


if __name__ == "__main__":
    main()
