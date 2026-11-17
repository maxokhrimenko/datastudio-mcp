import asyncio
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from datastudio_mcp.browser import Browser, BrowserConfig, StudioError
from datastudio_mcp.opening import list_reports, open_report, report_access
from datastudio_mcp.workspace import Workspace

REPORT = "0f0f0f0f-1a1a-4b2b-8c3c-4d4d4d4d4d4d"
BASE = f"https://lookerstudio.google.com/reporting/{REPORT}"
SIGN_IN = "https://accounts.google.com/ServiceLogin?continue=" + quote(BASE)


class Page:
    def __init__(self, urls, ready_after=0):
        self.urls = list(urls)
        self.url = self.urls[0]
        self.ready_after = ready_after
        self.evaluations = 0
        self.closed = False
        self.front = 0

    def advance(self):
        if len(self.urls) > 1:
            self.urls.pop(0)
            self.url = self.urls[0]

    async def evaluate(self, script, report_id):
        self.evaluations += 1
        self.advance()
        if self.evaluations > self.ready_after and "/reporting/" in self.url:
            return {"report_id": report_id, "editor_report_version": "7", "url": self.url}
        return None

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        self.front += 1


class LoginPage(Page):
    """Sits on Google sign-in; the login-page check must advance it without evaluating."""

    async def evaluate(self, script, report_id):
        raise AssertionError("evaluate must not run on the sign-in page")


class FakeBrowser:
    def __init__(self, pages):
        self.contexts = [SimpleNamespace(pages=pages)]

    def is_connected(self):
        return True


class FakeSession:
    """ReportSession.acquire/_settled seen from open_report: it lands the tab on View."""

    def __init__(self, browser, errors):
        self.browser = browser
        self.errors = list(errors)
        self.acquires = 0
        self.settled = []

    async def acquire(self):
        self.acquires += 1
        if self.errors:
            raise self.errors.pop(0)
        tabs = [p for p in self.browser._browser.contexts[0].pages
                if not p.closed and f"/reporting/{REPORT}" in p.url]
        assert len(tabs) == 1  # open_report must not leave duplicate tabs to acquire.
        tabs[0].url = tabs[0].url.replace("/edit", "")
        return tabs[0]

    async def _settled(self, page, deadline):
        self.settled.append(page)


def harness(tmp_path, *pages, new_page=None, errors=(), inventory_error=None, headless=True):
    browser = Browser(BrowserConfig(tmp_path, tmp_path / "journal", launch_timeout_s=0.01))
    browser._browser = FakeBrowser(list(pages))
    opened = []

    async def open_tab(report_id):
        opened.append(report_id)
        page = new_page or Page([BASE])
        browser._browser.contexts[0].pages.append(page)
        return page

    browser.open_tab = open_tab
    # The profile's Chrome runs headless; relaunches are recorded, never spawned.
    browser.mode = {"headless": headless}
    browser.relaunches = []

    async def is_headless():
        return browser.mode["headless"]

    async def relaunch(*, headless):
        browser.relaunches.append(headless)
        browser.mode["headless"] = headless
        browser._browser.contexts[0].pages.clear()

    browser.headless, browser.relaunch = is_headless, relaunch
    session = FakeSession(browser, errors)

    async def inventory():
        if inventory_error:
            raise inventory_error
        return {"report_id": REPORT, "name": "Weekly", "pages": []}

    def factory(_):
        return SimpleNamespace(
            studio=SimpleNamespace(inventory=inventory, session=session), name=None
        )

    ws = Workspace(browser, factory=factory)
    ws.session = session
    return ws, opened


async def noop(seconds):
    return None


def run(coro):
    return asyncio.run(coro)


def test_open_creates_tab_sets_active_and_returns_inventory(tmp_path):
    ws, opened = harness(tmp_path)
    result = run(open_report(ws, BASE + "/page/p_1", wait_seconds=1, sleep=noop))
    assert opened == [BASE + "/page/p_1"]
    assert result["status"] == "ready"
    assert result["mode"] == "view"
    assert result["name"] == "Weekly"
    assert ws.active == REPORT
    assert ws.name(REPORT) == "Weekly"


def test_open_reuses_single_editor_tab_and_reports_the_view_it_lands_in(tmp_path):
    editor = Page([BASE + "/page/p_1/edit"])
    ws, opened = harness(tmp_path, editor)
    result = run(open_report(ws, REPORT, wait_seconds=1, sleep=noop))
    assert opened == [] and not editor.closed
    assert ws.session.acquires == 1
    assert result["status"] == "ready" and result["mode"] == "view"


def test_open_preserves_explicit_account_reference(tmp_path):
    ws, opened = harness(tmp_path)
    reference = BASE + "?authuser=1"
    run(open_report(ws, reference, wait_seconds=1, sleep=noop))
    assert opened == [reference]


def test_open_replaces_tabs_of_another_account_after_autosave(tmp_path):
    editor, viewer = Page([BASE + "/edit"]), Page([BASE])
    ws, opened = harness(tmp_path, editor, viewer)
    reference = BASE + "?authuser=1"
    result = run(open_report(ws, reference, wait_seconds=1, sleep=noop))
    assert result["status"] == "ready" and ws.active == REPORT
    assert editor.closed and viewer.closed and opened == [reference]
    assert ws.session.settled == [editor]  # Only the editor needed to autosave first.


def test_open_reuses_matching_account_path(tmp_path):
    page = Page([BASE.replace("/reporting/", "/u/1/reporting/")])
    ws, opened = harness(tmp_path, page)
    result = run(open_report(ws, BASE + "?authuser=1", wait_seconds=1, sleep=noop))
    assert result["status"] == "ready" and opened == [] and not page.closed


def test_sign_in_shows_a_window_then_work_continues_headless(tmp_path):
    page = Page([SIGN_IN, BASE])
    ws, opened = harness(tmp_path, new_page=page)
    ticks = iter(range(100))
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        page.advance()

    result = run(open_report(ws, REPORT, wait_seconds=50, clock=lambda: next(ticks), sleep=sleep))
    assert result["status"] == "ready"
    assert slept  # The loop waited on the sign-in page instead of failing fast.
    # Headless Chrome hit sign-in -> a window for the user -> back to headless.
    assert ws.browser.relaunches == [False, True] and ws.browser.mode["headless"]
    assert opened == [REPORT] * 3  # first try, the sign-in window, the background tab


def test_leftover_sign_in_window_returns_to_background(tmp_path):
    ws, _ = harness(tmp_path, Page([BASE]), headless=False)
    assert run(open_report(ws, REPORT, wait_seconds=1, sleep=noop))["status"] == "ready"
    assert ws.browser.relaunches == [True]


def test_visible_chrome_with_an_editor_is_left_alone(tmp_path):
    ws, _ = harness(tmp_path, Page([BASE + "/edit"]), headless=False)
    assert run(open_report(ws, REPORT, wait_seconds=1, sleep=noop))["status"] == "ready"
    assert ws.browser.relaunches == []  # Closing Chrome could drop unsaved editor work.


def test_open_reports_login_required_after_deadline(tmp_path):
    page = LoginPage([SIGN_IN])
    ws, _ = harness(tmp_path, new_page=page)
    ticks = iter([0, 1, 2, 3])
    result = run(open_report(ws, REPORT, wait_seconds=2, clock=lambda: next(ticks), sleep=noop))
    assert result["status"] == "login_required"
    assert "sign in" in result["instruction"].lower()
    assert ws.active is None
    assert ws.browser.relaunches == [False]  # The window stays open for the user.


def test_open_not_ready_after_deadline_is_classified(tmp_path):
    page = Page([BASE], ready_after=10**6)
    ws, _ = harness(tmp_path, page)
    ticks = iter([0, 1, 2, 3])
    with pytest.raises(StudioError) as error:
        run(open_report(ws, REPORT, wait_seconds=2, clock=lambda: next(ticks), sleep=noop))
    assert error.value.code == "EDITOR_NOT_READY"


def test_open_rejects_bad_reference_before_touching_the_browser(tmp_path):
    ws, opened = harness(tmp_path)
    with pytest.raises(StudioError) as error:
        run(open_report(ws, "https://example.com/x", wait_seconds=1, sleep=noop))
    assert error.value.code == "INVALID_REPORT_REFERENCE"
    assert opened == []


def test_list_reports_marks_active_and_names(tmp_path):
    other = "11111111-2222-4333-8444-555555555555"
    ws, _ = harness(
        tmp_path, Page([BASE]), Page([f"https://lookerstudio.google.com/reporting/{other}/edit"])
    )
    run(open_report(ws, REPORT, wait_seconds=1, sleep=noop))
    assert run(list_reports(ws)) == [
        {"report_id": REPORT, "mode": "view", "tab_url": BASE, "active": True, "name": "Weekly"},
        {
            "report_id": other,
            "mode": "edit",
            "tab_url": f"https://lookerstudio.google.com/reporting/{other}/edit",
            "active": False,
            "name": None,
        },
    ]


def test_list_reports_without_browser_is_empty(tmp_path):
    browser = Browser(BrowserConfig(tmp_path, tmp_path / "journal"))
    assert run(list_reports(Workspace(browser))) == []


def test_sign_in_seen_only_by_acquire_waits_for_the_user(tmp_path):
    # The tab may still be on the report URL when acquire() sees the sign-in redirect.
    auth = [StudioError("AUTH_REQUIRED", "sign in")] * 3
    ws, _ = harness(tmp_path, Page([BASE]), errors=auth)
    ticks = iter(range(100))
    result = run(open_report(ws, REPORT, wait_seconds=50, clock=lambda: next(ticks), sleep=noop))
    assert result["status"] == "ready"
    assert ws.browser.relaunches == [False, True]  # Window for sign-in, then background.


def test_sign_in_not_completed_by_deadline_is_reported_not_raised(tmp_path):
    auth = [StudioError("AUTH_REQUIRED", "sign in")] * 10
    ws, _ = harness(tmp_path, Page([BASE]), errors=auth)
    ticks = iter(range(100))
    result = run(open_report(ws, REPORT, wait_seconds=3, clock=lambda: next(ticks), sleep=noop))
    assert result["status"] == "login_required"
    assert ws.active is None
    assert ws.browser.relaunches == [False]


def test_acquire_errors_other_than_login_propagate_at_once(tmp_path):
    # acquire() already waits for the draft model; open_report must not mask real errors.
    ws, _ = harness(tmp_path, Page([BASE]), errors=[StudioError("EDITOR_DIRTY", "unsaved")])
    with pytest.raises(StudioError) as error:
        run(open_report(ws, REPORT, wait_seconds=5, sleep=noop))
    assert error.value.code == "EDITOR_DIRTY" and ws.session.acquires == 1


def test_other_read_errors_propagate(tmp_path):
    ws, _ = harness(tmp_path, Page([BASE]), inventory_error=StudioError("INVALID_REPORT", "bad"))
    with pytest.raises(StudioError) as error:
        run(open_report(ws, REPORT, wait_seconds=1, sleep=noop))
    assert error.value.code == "INVALID_REPORT"


def test_view_only_model_is_reported_instead_of_looking_stale():
    assert report_access("1793277888514") == {"access": "edit"}
    viewer = report_access("NaN")
    assert viewer["access"] == "view" and "only view" in viewer["access_note"]
