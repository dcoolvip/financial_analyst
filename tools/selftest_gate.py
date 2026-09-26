"""End-to-end check of Wi-Fi mode over real HTTPS: gate + Streamlit on spare ports, driven by a client
that verifies the certificate like a browser would. Uses a throwaway password; your real one is untouched.

    ~/.venvs/financial-analyst/bin/python tools/selftest_gate.py
"""
import asyncio
import hashlib
import os
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from finance.paths import data_dir  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GATE, UP = 18501, 18502
ORIGIN = f"https://localhost:{GATE}"
PW = "selftest-" + os.urandom(4).hex()


def start() -> list[subprocess.Popen]:
    auth = tempfile.mkdtemp()
    salt = os.urandom(16)
    Path(auth, "app_password").write_text(salt.hex() + ":" + hashlib.scrypt(PW.encode(), salt=salt, n=2**14, r=8, p=1).hex())
    env = {**os.environ, "FINANCE_GATE": "1", "FINANCE_AUTH_DIR": auth}
    py = sys.executable
    return [
        subprocess.Popen([py, "-m", "streamlit", "run", "app.py", "--server.port", str(UP),
                          "--server.address", "127.0.0.1", "--server.headless", "true"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([py, "-m", "finance.gate", "--host", "localhost", "--ip", "127.0.0.1",
                          "--port", str(GATE), "--upstream-port", str(UP)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE),
    ]


async def run() -> list[tuple[str, bool, str]]:
    tls = ssl.create_default_context(cafile=str(data_dir() / "tls/ca.pem"))
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls)) as anon:
        for _ in range(40):  # wait for both servers
            try:
                async with anon.get(f"{ORIGIN}/_stcore/health", allow_redirects=False) as r:
                    if r.status == 401:
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.5)
        async with anon.get(f"{ORIGIN}/", allow_redirects=False) as r:
            check("Signed-out visitors are sent to /login", r.status == 302 and r.headers.get("Location") == "/login",
                  f"{r.status} {r.headers.get('Location')}")
        try:
            async with anon.ws_connect(f"wss://localhost:{GATE}/_stcore/stream", protocols=["streamlit"],
                                       headers={"Origin": ORIGIN}):
                check("Signed-out WebSocket refused", False, "connected")
        except aiohttp.WSServerHandshakeError as e:
            check("Signed-out WebSocket refused", e.status == 401, str(e.status))
        async with anon.post(f"{ORIGIN}/login", data={"password": "wrong"}, headers={"Origin": ORIGIN}) as r:
            check("Wrong password rejected", "Wrong password" in await r.text())
        # Real browsers often send Origin: null on form posts; Sec-Fetch-Site vouches for them
        async with anon.post(f"{ORIGIN}/login", data={"password": PW},
                             headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"}, allow_redirects=False) as r:
            check("Browser-style form sign-in (Origin: null)", r.status == 303, f"{r.status} {(await r.text())[:40]}")
        async with anon.post(f"{ORIGIN}/login", data={"password": PW},
                             headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"}, allow_redirects=False) as r:
            check("Cross-site form post blocked", r.status == 403, str(r.status))

    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls), cookie_jar=jar) as s:
        async with s.post(f"{ORIGIN}/login", data={"password": PW, "remember": "on"}, headers={"Origin": ORIGIN},
                          allow_redirects=False) as r:
            check("Password sign-in", r.status == 303 and "__Host-fa_session" in r.cookies, str(r.status))
        async with s.get(f"{ORIGIN}/") as r:
            body = await r.text()
            check("Dashboard page served through the gate", r.status == 200 and "streamlit" in body.lower(),
                  str(r.status))
            check("HSTS header present", "max-age" in r.headers.get("Strict-Transport-Security", ""))
        async with s.get(f"{ORIGIN}/_stcore/health") as r:
            check("Streamlit health through the gate", r.status == 200 and (await r.text()).strip() == "ok", str(r.status))
        async with s.get(f"{ORIGIN}/security") as r:
            check("Security page", r.status == 200 and "passkey" in (await r.text()).lower(), str(r.status))
        try:
            async with s.ws_connect(f"wss://localhost:{GATE}/_stcore/stream", protocols=["streamlit"],
                                    headers={"Origin": ORIGIN}, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                check("Live WebSocket through the gate", not ws.closed and ws.protocol == "streamlit", str(ws.protocol))
        except Exception as e:  # noqa: BLE001
            check("Live WebSocket through the gate", False, f"{type(e).__name__}: {e}")
    return results


def main() -> int:
    if not (data_dir() / "tls/ca.pem").exists():
        subprocess.run([str(ROOT / "tools/make_tls.sh")], check=True)
    procs = start()
    try:
        results = asyncio.run(asyncio.wait_for(run(), 60))
    except Exception as e:  # noqa: BLE001
        err = procs[1].stderr.read().decode()[-800:] if procs[1].poll() is not None else ""
        print(f"Self-test could not run: {type(e).__name__}: {e}\n{err}")
        return 1
    finally:
        for p in procs:
            p.terminate()
        time.sleep(0.5)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   ({detail})"))
    failed = sum(not ok for _, ok, _ in results)
    print("\nAll checks passed." if not failed else f"\n{failed} check(s) failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
