"""The bank recorder must never keep account numbers, balances or anything typed (no browser needed)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("bank_recorder", Path(__file__).parent.parent / "tools/bank_recorder.py")
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


def test_digits_and_amounts_are_masked():
    assert rec.mask("Adv Plus Banking - 6784  $12,345.67 available") == "Adv Plus Banking - ####  $… available"
    assert rec.mask("Balance -$1,000") == "Balance $…"


def test_steps_keep_labels_only():
    s = rec._clean({"event": "click", "tag": "a", "text": "Download", "id": "acct-123456",
                    "href": "/myaccounts/details/deposit/account-details.go?adx=9f8e7d6c5b&acct=12345678",
                    "aria": None, "name": ""})
    assert s == {"event": "click", "tag": "a", "text": "Download", "id": "acct-######",
                 "href": "/myaccounts/details/deposit/account-details.go"}


def test_typed_text_is_never_captured():
    js = rec.RECORDER_JS
    assert "isField ? null" in js                           # text boxes report no text at all
    assert ".value" not in js.replace("t.value || ''", "").replace("t.options[t.selectedIndex]", "")



def test_after_a_click_only_layout_measurements_are_noted():
    js = rec.RECORDER_JS
    assert "event: 'layout'" in js and "getBoundingClientRect" in js
    assert "innerText" not in js.split("const layout")[1].split("};")[0]          # sizes and kinds, never content


def test_bank_windows_keep_the_sandbox_open_full_size_and_plain_only_when_asked():
    """--no-sandbox showed up on Merrill's window: Playwright switches Chrome's sandbox off by default."""
    from finance import bank_browser
    calls = []

    class Chromium:
        def launch_persistent_context(self, profile, **kw):
            calls.append(kw)
            return type("Ctx", (), {"on": lambda self, e, f: None})()
    pw = type("PW", (), {"chromium": Chromium()})()
    bank_browser.launch(pw, "/tmp/p")
    bank_browser.launch(pw, "/tmp/p", plain=True)
    normal, plain = calls
    assert normal["chromium_sandbox"] is True and plain["chromium_sandbox"] is True
    assert "--window-size=1680,1050" in normal["args"] and "ignore_default_args" not in normal
    assert set(plain["ignore_default_args"]) == {"--enable-automation", "--disable-extensions",
                                                 "--disable-component-extensions-with-background-pages"}
    assert {"amex", "merrill"} <= rec.PLAIN_CHROME and "bofa" not in rec.PLAIN_CHROME


def test_a_window_the_bank_opens_is_resized_to_full_size(monkeypatch):
    """Merrill opens each statement in its own window, which came up thumbnail-sized with its buttons out of view."""
    from finance import bank_browser
    resized = []
    monkeypatch.setattr(bank_browser, "resize", lambda page, size: resized.append((page.name, size)))
    handlers = {}
    ctx = type("Ctx", (), {"on": lambda self, e, f: handlers.__setitem__(e, f)})()
    bank_browser.grow_popups(ctx)

    class Page:
        def __init__(self, name, opener): self.name, self._opener = name, opener
        def opener(self): return self._opener
    handlers["page"](Page("first tab", None))
    handlers["page"](Page("statement window", object()))
    assert resized == [("statement window", bank_browser.POPUP)]


def test_every_bank_window_opens_through_the_shared_launcher():
    for f in ("bank_recorder.py", "bofa_fetch.py", "chase_fetch.py", "amex_fetch.py"):
        src = (Path(__file__).parent.parent / "tools" / f).read_text()
        assert "bank_browser.launch(" in src and "launch_persistent_context" not in src, f

