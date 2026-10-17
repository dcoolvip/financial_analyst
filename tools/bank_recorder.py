"""Record how you reach a bank's "download transactions" page, so it can be automated later.

    ~/.venvs/financial-analyst/bin/python tools/bank_recorder.py bofa statements
    ~/.venvs/financial-analyst/bin/python tools/bank_recorder.py bofa transactions

The second word names the recording (one per route), so recording another route doesn't replace the first.

Opens a real Google Chrome window (its own profile in the private data folder, so "remember this device"
sticks between runs). You sign in yourself - password and text code - and nothing is recorded until you're
signed in. Then click through to one account's Download as usual and download the file. Close the window
when done.

What's recorded: which buttons/links/menus you clicked (their labels, with every digit masked as # and
dollar amounts removed) and the page addresses (digits masked). Never anything typed into a box, never
balances, never account numbers. The downloaded file is kept in the private data folder for the dashboard.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from finance import paths  # noqa: E402

BANKS = {"bofa": "https://www.bankofamerica.com/", "chase": "https://www.chase.com/"}
SIGNED_IN = re.compile(r"\b(log ?out|sign ?out)\b", re.I)
WAIT_FOR_SIGN_IN_S = 10 * 60
WAIT_FOR_DONE_S = 20 * 60

_MONEY = re.compile(r"-?\$\s?[\d,]+(?:\.\d+)?")
_DIGIT = re.compile(r"\d")


def mask(s) -> str:
    """No amounts, no digits: '$1,234.56' -> '$…', 'Checking - 1234' -> 'Checking - ####'."""
    return _DIGIT.sub("#", _MONEY.sub("$…", str(s or "")))[:120].strip()


# In the page: describe what was clicked or chosen - labels only, never what's typed into a field.
RECORDER_JS = r"""
(() => {
  const describe = (el) => {
    const t = el.closest('a,button,[role=button],[role=link],[role=tab],[role=menuitem],[role=option],label,select,input,li,span,div') || el;
    const isField = t.tagName === 'INPUT' && !['checkbox','radio','submit','button'].includes((t.type||'').toLowerCase());
    return {
      tag: t.tagName.toLowerCase(), type: t.type || null, role: t.getAttribute('role'),
      text: isField ? null : (t.innerText || t.value || '').trim().slice(0, 120),
      aria: t.getAttribute('aria-label'), id: t.id || null, name: t.getAttribute('name'),
      cls: (t.className && typeof t.className === 'string') ? t.className.split(/\s+/).slice(0, 3).join(' ') : null,
      href: t.getAttribute('href'),
    };
  };
  document.addEventListener('click', e => window.__record && window.__record({event: 'click', ...describe(e.target)}), true);
  document.addEventListener('change', e => {
    const t = e.target;
    if (t.tagName === 'SELECT') {
      window.__record && window.__record({event: 'choose', ...describe(t), text: t.options[t.selectedIndex]?.text || ''});
    } else if (['checkbox','radio'].includes((t.type||'').toLowerCase())) {
      window.__record && window.__record({event: 'choose', ...describe(t), checked: t.checked});
    }
  }, true);
})();
"""


def _clean(step: dict) -> dict:
    out = {k: (mask(v) if isinstance(v, str) else v) for k, v in step.items() if v not in (None, "")}
    if "href" in out:
        out["href"] = re.sub(r"[?#].*", "", out["href"])          # keep the path, drop query strings
    return out


def main(bank: str, label: str = "statements") -> None:
    from playwright.sync_api import sync_playwright

    out_dir = paths.data_dir() / "bank_fetch"
    profile, downloads = out_dir / "chrome-profile" / bank, out_dir / "downloads" / bank
    for d in (profile, downloads):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    steps, recording = [], {"on": False}

    def record(_source, step: dict) -> None:
        if recording["on"]:
            steps.append({"t": round(time.time(), 1), **_clean(step)})

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False,
                                                    accept_downloads=True, viewport=None)
        ctx.expose_binding("__record", record)
        ctx.add_init_script(RECORDER_JS)

        def watch(page) -> None:
            page.on("framenavigated", lambda f: recording["on"] and f == page.main_frame and steps.append(
                {"t": round(time.time(), 1), "event": "page", "url": mask(re.sub(r"[?#].*", "", f.url))}))
            def got(dl) -> None:
                name = dl.suggested_filename
                dl.save_as(downloads / name)
                steps.append({"t": round(time.time(), 1), "event": "download", "file": mask(name)})
                print(f"Saved a download ({mask(name)}) - you can close the window when you're done.", flush=True)
            page.on("download", got)
        ctx.on("page", watch)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        watch(page)
        page.goto(BANKS[bank])
        print("Sign in in the Chrome window (password, text code). Nothing is recorded until you're signed in.",
              flush=True)

        deadline = time.time() + WAIT_FOR_SIGN_IN_S
        while time.time() < deadline:
            try:
                if any(p.get_by_text(SIGNED_IN).first.is_visible() for p in ctx.pages):
                    break
            except Exception:
                pass
            if not ctx.pages:
                print("Window closed before signing in - nothing recorded.")
                return
            time.sleep(2)
        else:
            print("Didn't see you sign in within 10 minutes - stopping.")
            ctx.close()
            return
        recording["on"] = True
        steps.append({"t": round(time.time(), 1), "event": "signed_in",
                      "url": mask(re.sub(r"[?#].*", "", ctx.pages[-1].url))})
        print("Signed in - recording. Click through to one account's Download as usual, download the file, "
              "then close the window.", flush=True)
        deadline = time.time() + WAIT_FOR_DONE_S
        try:
            while ctx.pages and time.time() < deadline:
                try:                      # wait *through Playwright*, so clicks, downloads and closing get seen
                    ctx.pages[-1].wait_for_timeout(500)
                except Exception:         # that page just closed - look at the ones left
                    pass
        except KeyboardInterrupt:
            print("Stopped - saving what was recorded.")
        finally:
            _save(out_dir, f"{bank}_{label}", steps)
            try:
                ctx.close()
            except Exception:
                pass


def _save(out_dir: Path, name: str, steps: list) -> None:
    rec = out_dir / f"{name}_recording.json"
    rec.write_text(json.dumps({"recording": name, "recorded": time.time(), "steps": steps}, indent=1))
    print(f"Recorded {len(steps)} steps (labels only, digits masked) to {rec}")


def _done() -> None:
    import os
    pid = paths.data_dir() / "bank_fetch" / "bofa_pull.pid"
    if pid.exists() and pid.read_text().strip() == str(os.getpid()):
        pid.unlink()


if __name__ == "__main__":
    import atexit
    atexit.register(_done)
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in BANKS or not re.fullmatch(r"\w*", sys.argv[-1]):
        sys.exit(f"usage: bank_recorder.py {{{','.join(BANKS)}}} [statements|transactions|...]")
    main(*sys.argv[1:])
