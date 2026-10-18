"""How the bank pulls and the recorder open Chrome - in one place.

* A full-size window (Chrome otherwise opens small), and pop-up windows the bank opens - Merrill shows statements
  in its own window - are resized to full size as they appear (they came up thumbnail-sized, buttons out of view).
* Chrome's security sandbox stays ON. Playwright turns it off by default (--no-sandbox); these windows visit banks.
* plain=True: no "script-driven" signals (Amex stalls the sign-in otherwise), and Chrome's built-in PDF viewer kept.
"""
from __future__ import annotations

WINDOW = {"width": 1680, "height": 1050}
POPUP = {"width": 1400, "height": 1000}
PLAIN_IGNORE = ["--enable-automation", "--disable-extensions", "--disable-component-extensions-with-background-pages"]


def launch(pw, profile, plain: bool = False):
    args = [f"--window-size={WINDOW['width']},{WINDOW['height']}", "--window-position=0,0"]
    extra = {}
    if plain:
        extra["ignore_default_args"] = PLAIN_IGNORE
        args.append("--disable-blink-features=AutomationControlled")
    ctx = pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False, accept_downloads=True,
                                                viewport=None, chromium_sandbox=True, args=args, **extra)
    grow_popups(ctx)
    return ctx


def resize(page, size: dict) -> bool:
    """Resize the real window a page sits in (not just its content), via Chrome's own window controls."""
    try:
        cdp = page.context.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {**size, "left": 60, "top": 40}})
        return True
    except Exception:
        return False


def grow_popups(ctx) -> None:
    def opened(page):
        if page.opener() is not None:            # a window the bank opened (window.open), not the first tab
            resize(page, POPUP)
    ctx.on("page", opened)
