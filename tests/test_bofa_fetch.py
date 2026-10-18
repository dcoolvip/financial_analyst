"""The BofA pull's decisions, without a browser: which tiles are new, which account a statement is, the report."""
import importlib.util
import subprocess
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from finance import db
from tests.test_statements import BOFA_STMT, _pdf

spec = importlib.util.spec_from_file_location("bofa_fetch", Path(__file__).parent.parent / "tools/bofa_fetch.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)


def test_statement_tiles_are_read_and_remembered():
    info = bf.tile_info("September Statement\nAdvantage Savings - 1234\nSep 25, 2026\nActivating will open this ...")
    assert info == {"title": "September Statement", "label": "Advantage Savings - 1234", "date": date(2026, 9, 25)}
    assert bf.tile_key(info) == "Advantage Savings - 1234|2026-09-25"
    assert bf.tile_info("Tax documents\nsomething") is None


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    return db.connect(tmp_path / "finance.db")


def test_a_statement_finds_its_account_by_digits_else_the_only_one_of_its_kind(conn, tmp_path):
    chk = db.get_or_create_account(conn, "Bofa Checking", "Bank of America", "checking")
    sav = db.get_or_create_account(conn, "Bofa Savings", "Bank of America", "savings")
    db.get_or_create_account(conn, "Chase Checking", "Chase", "checking")
    pdf = tmp_path / "eStmt_2026-09-21.pdf"
    pdf.write_bytes(_pdf(BOFA_STMT.splitlines()))
    line = bf.import_statement(conn, pdf)
    assert line == "Bofa Checking (Aug 22 – Sep 21): 8 transactions, 0 already there, 8 added"
    assert db.accounts(conn).set_index("id").loc[chk, "last4"] == "0522"     # digits saved: next time by digits
    assert bf.import_statement(conn, pdf).endswith("8 transactions, 8 already there, 0 added")
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (sav,)).fetchone()[0] == 0


def test_an_unclear_statement_is_left_for_you_not_guessed(conn, tmp_path):
    db.get_or_create_account(conn, "Checking A", "Bank of America", "checking")
    db.get_or_create_account(conn, "Checking B", "Bank of America", "checking")
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(_pdf(BOFA_STMT.splitlines()))
    assert "couldn't tell which dashboard account" in bf.import_statement(conn, pdf)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


class FakeBox:
    def __init__(self): self.clicked, self.pressed = False, []
    @property
    def first(self): return self
    def click(self): self.clicked = True
    def press(self, key): self.pressed.append(key)


class FakeBofA:
    """Home page with a Log In link; Chrome has filled the sign-in boxes; Enter signs you in."""
    def __init__(self): self.url, self.login, self.pwd, self.signed_in = None, FakeBox(), FakeBox(), False
    def goto(self, url): self.url = url
    def get_by_role(self, role, name=None):
        login = self.login
        class L:
            first = property(lambda s: login)
            def or_(s, other): return s
        return L()
    def locator(self, sel):
        assert sel == "input[type=password] >> visible=true"
        return self.pwd
    def get_by_text(self, pattern):
        page = self
        class T:
            first = property(lambda s: s)
            def is_visible(s): return page.signed_in and bool(pattern.search("Log Out"))
        return T()
    def wait_for_timeout(self, ms):
        if self.pwd.pressed: self.signed_in = True


def test_sign_in_is_log_in_then_enter_on_chromes_saved_password():
    page = FakeBofA()
    assert bf.sign_in(page) is True
    assert page.url == bf.HOME and page.login.clicked and page.pwd.clicked and page.pwd.pressed == ["Enter"]


def test_a_download_must_be_the_statement_on_the_tile():
    """Regression: every tile's download came back as the mortgage statement (first link on the page)."""
    from finance.importers.statements import Statement
    mortgage = Statement(kind="loan", institution="Bank of America", last4="6784")
    savings_tile = {"title": "September Statement", "label": "Advantage Savings - 9897"}
    card_tile = {"title": "September Statement", "label": "Customized Cash Rewards Visa Signature - 1013"}
    assert "ending 6784, not 9897" in bf.matches_tile(savings_tile, mortgage)
    assert bf.matches_tile({"title": "October Statement", "label": "Mortgage - 6784"}, mortgage) is None
    assert "not credit card" in bf.matches_tile({"title": "x", "label": "Customized Cash Rewards"}, mortgage)
    savings = Statement(kind="deposit", institution="Bank of America", last4="9897")
    assert bf.matches_tile(savings_tile, savings) is None
    assert bf.matches_tile({"title": "September Statement", "label": "DCOOLVIP"},
                           Statement(kind="deposit", last4="0522")) is None   # a nickname: nothing to contradict
    assert bf.matches_tile(card_tile, savings) is not None



def test_no_two_tiles_may_bring_down_the_same_document(tmp_path, monkeypatch):
    """The DCOOLVIP (nickname) tile's PDF was the mortgage too; with no digits to check, the duplicate is what gives
    it away - the Mortgage tile proves the document is its own, so the nickname tile's copy is rejected."""
    from finance.importers.statements import Statement
    mortgage = Statement(kind="loan", institution="Bank of America", last4="6784")
    mortgage.as_of = date(2026, 10, 5)
    monkeypatch.setattr(bf.statements, "parse_pdf", lambda b: mortgage)
    files = {}
    for name in ("2026-09-21_DCOOLVIP.pdf", "2026-10-05_Mortgage_6784.pdf"):
        files[name] = tmp_path / name
        files[name].write_bytes(b"%PDF")
    new = [("DCOOLVIP|2026-09-21", files["2026-09-21_DCOOLVIP.pdf"], {"title": "September Statement", "label": "DCOOLVIP"}),
           ("Mortgage - 6784|2026-10-05", files["2026-10-05_Mortgage_6784.pdf"],
            {"title": "October Statement", "label": "Mortgage - 6784"})]
    verdict = {info["label"]: why for _, _, info, why in bf.check_downloads(new)}
    assert verdict["Mortgage - 6784"] is None
    assert "same document as the Mortgage - #### one" in verdict["DCOOLVIP"]



def test_a_shortened_name_on_the_tile_still_finds_its_download_link():
    """Regression: the card tile reads 'Customized Cash Re...'; its link has the full name, so it was skipped."""
    card = {"title": "September Statement", "label": "Customized Cash Re... - 1013"}     # as on the tile
    link = "Download PDF\nfor September Statement for Customized Cash Rewards Visa Signature - 1013"
    assert bf.link_pattern(card).search(link)
    assert not bf.link_pattern(card).search("Download PDF for September Statement for Customized Cash Rewards - 9999")
    assert bf.link_pattern({"title": "September Statement", "label": "Customized Cash Re..."}).search(link)
    savings = {"title": "September Statement", "label": "Advantage Savings - 9897"}
    assert bf.link_pattern(savings).search("Download PDF for September Statement for Advantage Savings - 9897")
    assert not bf.link_pattern(savings).search("Download PDF for September Statement for DCOOLVIP")
    assert not bf.link_pattern({"title": "September Statement", "label": "Mortgage - 67"}).search(
        "Download PDF for September Statement for Mortgage - 6784")              # a full name must match fully


def test_chase_takes_recent_unfetched_statements_and_matches_by_file_digits(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("chase_fetch", Path(__file__).parent.parent / "tools/chase_fetch.py")
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    today = date(2026, 10, 28)
    link = "Oct 06, 2026 Statement SAPPHIRE RESERVE (...2349) Saves document"
    assert cf.link_date(link) == date(2026, 10, 6)
    assert cf.wanted(link, set(), today)
    assert not cf.wanted(link, {link}, today)                                   # already fetched
    assert not cf.wanted("Jun 06, 2026 Statement SAPPHIRE RESERVE (...2349) Saves document", set(), today)   # too old
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    a = db.get_or_create_account(conn, "Chase Sapphire Reserve", "Chase", "credit_card")
    db.update_account_details(conn, a, last4="2349")
    db.get_or_create_account(conn, "Other Card", "Chase", "credit_card")
    assert cf.match_account(conn, "2349") == a and cf.match_account(conn, "9999") is None
    assert "not a statement" in cf.import_statement(conn, tmp_path / "20261006-tax-form-2349-.pdf")


def test_chase_download_arriving_from_a_pop_up_tab_is_caught(tmp_path):
    """Regression: the pull only watched the main tab; Chase's Submit + file come through a new tab, so every
    statement timed out. Now Submit is found in any tab and the download is caught from any tab."""
    spec = importlib.util.spec_from_file_location("chase_fetch2", Path(__file__).parent.parent / "tools/chase_fetch.py")
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)

    class Dl:
        suggested_filename = "20261006-statements-2349-.pdf"
        def save_as(self, path): Path(path).write_bytes(b"%PDF")

    class Submit:
        def __init__(self, tab): self.tab = tab
        def filter(self, visible): return self
        @property
        def first(self): return self
        def count(self): return 1 if self.tab.has_submit else 0
        def click(self): self.tab.clicked = True

    class Tab:
        def __init__(self, ctx, has_submit=False):
            self.context, self.has_submit, self.clicked, self.closed = ctx, has_submit, False, False
            self.keyboard = type("K", (), {"press": lambda s, k: None})()
        def locator(self, sel): return Submit(self)
        def wait_for_timeout(self, ms):
            if self.context.popup.clicked and not cf.DOWNLOADS:
                cf.DOWNLOADS.append(Dl())                        # the pop-up tab delivers the file
        def close(self): self.closed = True

    class Ctx:
        pass
    ctx = Ctx()
    main, ctx.popup = Tab(ctx), None
    ctx.popup = Tab(ctx, has_submit=True)
    ctx.pages = [main]
    cf.DOWNLOADS.clear()
    path = cf._save(main, lambda: ctx.pages.append(ctx.popup), tmp_path)        # clicking opens the pop-up
    assert path.name == "20261006-statements-2349-.pdf" and path.exists()
    assert ctx.popup.clicked and ctx.popup.closed and not main.closed            # Submit in the pop-up; tab tidied



def test_chase_download_watcher_gives_playwright_a_real_function():
    """Regression: handing Playwright list.append crashed the pull right after Chrome opened."""
    import inspect
    spec = importlib.util.spec_from_file_location("chase_fetch3", Path(__file__).parent.parent / "tools/chase_fetch.py")
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    handlers = []

    class Page:
        def on(self, event, f):
            assert inspect.isfunction(f), "Playwright wraps handlers by setting attributes: needs a Python function"
            handlers.append((event, f))

    class Ctx:
        pages = [Page()]
        def on(self, event, f): handlers.append((event, f))
    cf.DOWNLOADS.clear()
    cf.watch_downloads(Ctx())
    [f for e, f in handlers if e == "download"][0]("a download")
    assert cf.DOWNLOADS == ["a download"]
    [f for e, f in handlers if e == "page"][0](Page())                           # a new tab gets watched too
    assert sum(1 for e, _ in handlers if e == "download") == 2



def test_chase_never_submits_an_empty_password(monkeypatch):
    """Chase's sign-in frame only shows Chrome's saved login after a real click; pressing Enter on an empty password
    counted as a failed sign-in. Now: Enter only if filled, else a notification asks you to pick it."""
    spec = importlib.util.spec_from_file_location("chase_fetch4", Path(__file__).parent.parent / "tools/chase_fetch.py")
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    told = []
    monkeypatch.setattr(cf, "notify", told.append)
    monkeypatch.setattr(cf, "WAIT_FOR_SIGN_IN_S", 0)

    class Box:
        def __init__(self, value): self.value, self.pressed = value, []
        def input_value(self): return self.value
        def press(self, k): self.pressed.append(k)

    class Page:
        def __init__(self): self.signed_in = False
        def goto(self, url): pass
        def wait_for_timeout(self, ms):
            if self.box.pressed: self.signed_in = True
        def get_by_text(self, pattern):
            page = self
            class T:
                first = property(lambda s: s)
                def is_visible(s): return page.signed_in
            return T()
    for value, entered in (("", False), ("(saved)", True)):
        page = Page()
        page.box = Box(value)
        monkeypatch.setattr(cf, "_password_box", lambda p, box=page.box: box)
        cf.sign_in(page)
        assert (page.box.pressed == ["Enter"]) is entered
    assert any("pick your saved login" in t for t in told)


def test_amex_csv_imports_without_doubles_and_flags_a_gap(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("amex_fetch", Path(__file__).parent.parent / "tools/amex_fetch.py")
    af = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(af)
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    card = db.get_or_create_account(conn, "Amex Credit Card", "American Express", "credit_card")
    csv = tmp_path / "activity.csv"
    csv.write_bytes((Path(__file__).parent / "fixtures" / "amex_activity.csv").read_bytes())
    first = af.import_csv(conn, csv)
    assert first.startswith("Amex Credit Card (") and "0 already there" in first
    again = af.import_csv(conn, csv)
    assert " 0 added" in again                                                   # same file again: nothing doubled
    conn.execute("UPDATE transactions SET date = '2020-01-01'")                  # pretend the dashboard stopped long ago
    assert "⚠️ gap" in af.import_csv(conn, csv)
    db.get_or_create_account(conn, "Amex Gold", "American Express", "credit_card")
    assert "2 Amex cards" in af.import_csv(conn, csv)                           # two cards: never guess


def test_merrill_picks_account_statements_and_imports_by_digits(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("merrill_fetch", Path(__file__).parent.parent / "tools/merrill_fetch.py")
    mf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mf)
    today = date(2026, 10, 28)
    cma = mf.row_info("09/30/2026   CMA Edge", "CMA Edge")
    assert cma == {"date": date(2026, 9, 30), "name": "CMA Edge"} and mf.wanted(cma, set(), today)
    assert mf.row_info("09/30/2026  Equity Cost Basis Statement", "Equity Cost Basis Statement") is None
    assert not mf.wanted(cma, {mf.key(cma)}, today)                              # already fetched
    assert not mf.wanted(mf.row_info("06/30/2026 CMA Edge", "CMA Edge"), set(), today)   # too old
    from tests.test_statements import MERRILL_CMA
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    a = db.get_or_create_account(conn, "Merrill Brokerage", "Merrill", "brokerage")
    db.update_account_details(conn, a, last4="5678")
    pdf = tmp_path / "ML_09302026_CMAEdge.pdf"
    pdf.write_bytes(_pdf(MERRILL_CMA.strip().splitlines()))
    line = mf.import_statement(conn, pdf)
    assert line.startswith("Merrill Brokerage: value saved (Jul 31)") and "positions" in line
    assert conn.execute("SELECT COUNT(*) FROM holdings WHERE account_id = ?", (a,)).fetchone()[0] >= 2



def test_merrill_keeps_the_pdf_as_the_window_loads_it_and_presses_enter_when_filled():
    """Merrill's statement link is single-use (asking again gave an HTML error page): the PDF is caught as the window
    loads it. And the real sign-in page (after an interim one) gets Enter once Chrome has filled the password."""
    spec = importlib.util.spec_from_file_location("merrill_fetch2", Path(__file__).parent.parent / "tools/merrill_fetch.py")
    mf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mf)
    routes = {}
    ctx = type("Ctx", (), {"route": lambda self, pattern, f: routes.__setitem__(pattern, f)})()
    mf.catch_pdfs(ctx)
    fulfilled = []

    class Resp:
        def __init__(self, body): self._b = body
        def body(self): return self._b

    class Route:
        def __init__(self, body): self.b = body
        def fetch(self): return Resp(self.b)
        def fulfill(self, response, body): fulfilled.append(body)
    mf.CAUGHT.clear()
    routes["**/Records/**"](Route(b"<html>the wrapper page</html>"))
    routes["**/Records/**"](Route(b"%PDF-1.7 the statement"))
    assert mf.CAUGHT == [b"%PDF-1.7 the statement"] and len(fulfilled) == 2       # the window still gets both
    assert mf.wait_for_pdf(type("P", (), {"wait_for_timeout": lambda s, ms: None})(), 0) == b"%PDF-1.7 the statement"

    class Box:
        def __init__(self, v): self.v, self.pressed = v, 0
        def filter(self, visible): return self
        @property
        def first(self): return self
        def count(self): return 1
        def evaluate(self, js): return {"filled": bool(self.v), "auto": bool(self.v), "typing": False}
        def press(self, k): self.pressed += 1

    class Page:
        def __init__(self, url, v): self.url, self.box = url, Box(v)
        def locator(self, sel): return self.box
    pressed = set()
    interim, real = Page("https://www.merrilledge.com/login", ""), Page("https://olui2.fs.ml.com/login?x=1", "(saved)")
    assert not mf._press_if_filled(interim, pressed) and interim.box.pressed == 0   # empty: never submitted
    assert mf._press_if_filled(real, pressed) and real.box.pressed == 1
    assert not mf._press_if_filled(real, pressed) and real.box.pressed == 1        # once per page



def test_merrill_sign_in_starts_where_chrome_has_the_login_saved():
    """Regression: starting at the statements page landed on an olui2 sign-in page with nothing filled in."""
    import inspect
    spec = importlib.util.spec_from_file_location("merrill_fetch3", Path(__file__).parent.parent / "tools/merrill_fetch.py")
    mf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mf)
    src = inspect.getsource(mf.sign_in)
    assert "page.goto(HOME)" in src and "page.goto(DOCUMENTS)" not in src
    assert mf.HOME == "https://www.merrilledge.com/"



def test_merrill_waits_out_its_own_redirect_after_sign_in():
    """Regression: the update stopped with 'interrupted by another navigation' (Merrill redirecting to the accounts
    overview right after sign-in) before reaching the statements."""
    spec = importlib.util.spec_from_file_location("merrill_fetch4", Path(__file__).parent.parent / "tools/merrill_fetch.py")
    mf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mf)

    class Page:
        def __init__(self, fails): self.fails, self.went, self.waits = fails, [], 0
        def wait_for_load_state(self, state, timeout=None): self.waits += 1
        def wait_for_timeout(self, ms): pass
        def goto(self, url):
            if self.fails:
                self.fails -= 1
                raise RuntimeError('Page.goto: Navigation to "x" is interrupted by another navigation to "y"')
            self.went.append(url)
    p = Page(fails=2)
    mf.goto_settled(p, mf.DOCUMENTS)
    assert p.went == [mf.DOCUMENTS] and p.waits == 3
    with pytest.raises(RuntimeError):
        mf.goto_settled(Page(fails=5), mf.DOCUMENTS)                           # gives up after 3 tries, loudly


def test_fidelity_reads_last_month_and_fills_the_chosen_account(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("fidelity_fetch", Path(__file__).parent.parent / "tools/fidelity_fetch.py")
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)
    assert ff.last_month(date(2026, 10, 28)) == (2026, 9) and ff.last_month(date(2027, 1, 28)) == (2026, 12)
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    md = db.get_or_create_account(conn, "MD 401(k)", "Fidelity", "retirement")
    assert "pick which account" in ff.import_text(conn, "fidelity", "text", "NetBenefits_2026-09.txt")
    db.set_setting(conn, "browser_pull_accounts", {"fidelity": md})
    assert "couldn't read the balance" in ff.import_text(conn, "fidelity", "nothing useful here", "x.txt")



def test_each_fidelity_login_is_its_own_connection_and_never_fills_the_wrong_account(tmp_path, monkeypatch):
    """Both people sign in at the same NetBenefits page: each login has its own profile, Fills account and history,
    and a statement for a different person than a login's earlier ones is never imported."""
    from finance import bank_pull
    assert bank_pull.command("fidelity")[0].endswith("fidelity_fetch.py") and bank_pull.command("fidelity")[1:] == []
    assert bank_pull.command("fidelity_2")[0].endswith("fidelity_fetch.py")
    assert bank_pull.command("fidelity_2")[1:] == ["--login", "fidelity_2"]
    spec = importlib.util.spec_from_file_location("fidelity_fetch2", Path(__file__).parent.parent / "tools/fidelity_fetch.py")
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "finance.db")
    db.set_setting(conn, "household_people", [{"name": "Jane"}, {"name": "Sam"}])
    a = db.get_or_create_account(conn, "J 401(k)", "Fidelity", "retirement")
    b = db.get_or_create_account(conn, "S 401(k)", "Fidelity", "retirement")
    db.set_setting(conn, "browser_pull_accounts", {"fidelity": a, "fidelity_2": b})
    assert ff.same_holder(conn, "fidelity", "Account Statement\nJANE SAMPLE\nBeginning Balance") is None   # first: Jane
    assert ff.same_holder(conn, "fidelity_2", "Account Statement\nSAM SAMPLE\n...") is None                # Sam's login
    assert "different person" in ff.same_holder(conn, "fidelity", "Account Statement\nSAM SAMPLE")       # wrong window
    assert "not imported" in ff.import_text(conn, "fidelity", "SAM SAMPLE statement", "x.txt")
    assert db.get_setting(conn, "browser_pull_holders") == {"fidelity": "Jane", "fidelity_2": "Sam"}



def test_fidelity_goes_in_through_view_your_statements_like_the_recording():
    """Regression: jumping straight to the statements address after sign-in gave 'The site is temporarily
    unavailable'. Now: home page, click 'View your statements', wait for the form (one reload if Fidelity balks)."""
    spec = importlib.util.spec_from_file_location("fidelity_fetch3", Path(__file__).parent.parent / "tools/fidelity_fetch.py")
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)
    log = []

    class Loc:
        def __init__(self, what): self.what = what
        @property
        def first(self): return self
        def click(self, timeout=None): log.append(("click", self.what))
        def count(self): return 1 if self.what == "unavailable" and page.balky else 0
        def wait_for(self, state=None, timeout=None):
            if page.balky:
                raise TimeoutError("form not there")
            log.append(("form", self.what))

    class Page:
        balky = True
        def wait_for_load_state(self, *a, **k): log.append(("settle",))
        def get_by_text(self, t, exact=False): return Loc("view" if isinstance(t, str) else "unavailable")
        def locator(self, sel): return type("V", (), {"filter": lambda s, visible: Loc(sel)})()
        def reload(self): log.append(("reload",)); Page.balky = False
        def goto(self, url): log.append(("goto", url))
    page = Page()
    ff.open_statements(page)
    assert log == [("settle",), ("click", "view"), ("reload",), ("form", "select[aria-label='Time period']")]
    assert not [e for e in log if e[0] == "goto"]                                 # no jumping straight to the address



def test_fidelity_dropdowns_are_the_visible_selects_by_name():
    """Regression: get_by_label("Time period") matched several elements on Fidelity's page and stopped the pull."""
    spec = importlib.util.spec_from_file_location("fidelity_fetch4", Path(__file__).parent.parent / "tools/fidelity_fetch.py")
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)
    asked = []

    class Page:
        def locator(self, sel):
            asked.append(sel)
            return type("L", (), {"filter": lambda s, visible: type("F", (), {"first": "the one"})()})()
    assert ff.dropdown(Page(), "Month") == "the one" and asked == ["select[aria-label='Month']"]
    import inspect
    assert "get_by_label(" not in inspect.getsource(ff.show_statement) + inspect.getsource(ff.open_statements)



def test_fidelity_fills_one_form_and_checks_monthly_stuck_before_submitting():
    """Regression: Month and Year were set but Time period still read 'Select a time period' (two copies of the form),
    so Fidelity refused. Now all three are set inside the form holding Get statement, and Monthly is checked first."""
    import inspect
    spec = importlib.util.spec_from_file_location("fidelity_fetch5", Path(__file__).parent.parent / "tools/fidelity_fetch.py")
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)
    src = inspect.getsource(ff.show_statement)
    assert "statement_form(page)" in src and 'dropdown(form, "Time period")' in src and 'dropdown(page, ' not in src
    assert 'chosen.strip() == "Monthly"' in src
    did = []

    class Box:
        def click(self): did.append("click")
        def select_option(self, label): did.append(f"select {label}")
        def dispatch_event(self, e): did.append(e)
        def press(self, k): did.append(k)
    ff.choose(Box(), "Monthly")
    assert did == ["click", "select Monthly", "change", "Tab"]



def test_fidelity_waits_for_month_and_year_to_appear():
    """Regression: the update chose Monthly, gave the Month box one second to appear and timed out on a slow day."""
    spec = importlib.util.spec_from_file_location("fidelity_fetch6", Path(__file__).parent.parent / "tools/fidelity_fetch.py")
    ff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ff)
    ticks = {"n": 0}

    class Box:
        def __init__(self, there): self.there = there
        def count(self): return 1 if self.there() else 0

    class Scope:
        def __init__(self, where): self.where = where
        def locator(self, sel):
            there = (lambda: ticks["n"] >= 6) if self.where == "page" else (lambda: False)   # shows up late, outside the form
            return type("L", (), {"filter": lambda s, visible: type("F", (), {"first": Box(there)})()})()

    class Page(Scope):
        def wait_for_timeout(self, ms): ticks["n"] += 1
    box = ff.appeared(Page("page"), Scope("form"), "Month")
    assert box.count() == 1 and ticks["n"] == 6                                     # waited ~3 s, then found it
