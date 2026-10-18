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



def test_amex_opens_chrome_without_the_automation_flag():
    """Amex stayed on its sign-in page in the recorder's Chrome (but not in everyday Chrome/Safari)."""
    assert {"amex", "merrill"} <= rec.PLAIN_CHROME and "bofa" not in rec.PLAIN_CHROME and "chase" not in rec.PLAIN_CHROME
    import inspect
    src = inspect.getsource(rec.main)
    assert '"--enable-automation"' in src and "AutomationControlled" in src



def test_chrome_opens_full_size_so_pop_ups_keep_their_buttons():
    """Merrill's statement pop-up scales to the window; in Chrome's small default its download buttons were cut off."""
    import inspect
    assert "--window-size=1680,1050" in rec.WINDOW
    for f in ("bofa_fetch.py", "chase_fetch.py", "amex_fetch.py"):
        assert "--window-size=1680,1050" in (Path(__file__).parent.parent / "tools" / f).read_text(), f



def test_after_a_click_only_layout_measurements_are_noted():
    js = rec.RECORDER_JS
    assert "event: 'layout'" in js and "getBoundingClientRect" in js
    assert "innerText" not in js.split("const layout")[1].split("};")[0]          # sizes and kinds, never content



def test_plain_chrome_keeps_the_pdf_viewer():
    import inspect
    src = inspect.getsource(rec.main)
    assert '"--disable-extensions"' in src and "--disable-component-extensions-with-background-pages" in src
