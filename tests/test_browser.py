import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from datastudio_mcp.browser import (
    EDIT_TOGGLE,
    MODEL_STATE_SCRIPT,
    REPORT_READY_SCRIPT,
    BrowserConfig,
    Config,
    ReportSession,
    StudioError,
    is_report_view,
    parse_report_reference,
    tab_mode,
    tab_report_id,
)
from datastudio_mcp.browser import Browser as ChromeBrowser

REPORT = "0f0f0f0f-1a1a-4b2b-8c3c-4d4d4d4d4d4d"
BASE = f"https://datastudio.google.com/u/0/reporting/{REPORT}"


@pytest.mark.parametrize("suffix", ["", "/", "/page/p_test", "/page/p_test/?x=1"])
def test_known_view_routes(suffix):
    assert is_report_view(BASE + suffix, REPORT)


@pytest.mark.parametrize(
    "url",
    [
        BASE + "/edit",
        BASE + "/page/p_test/edit",
        BASE + "/unknown/route",
        BASE.replace(REPORT, REPORT + "0"),
        BASE.replace("datastudio.google.com", "datastudio.google.com.attacker.test"),
        BASE.replace("https://", "http://"),
    ],
)
def test_unknown_or_edit_routes_fail_closed(url):
    assert not is_report_view(url, REPORT)


class FakeBrowser:
    def __init__(self, pages):
        self.contexts = [SimpleNamespace(pages=pages)]
        self.connected = True

    def is_connected(self):
        return self.connected


class Page:
    def __init__(self, url, hang=False):
        self.url = url
        self.calls = []
        self.hang = hang
        self.closed = False
        self.front = 0

    async def evaluate(self, script, arguments):
        self.calls.append(arguments)
        if self.hang:
            await asyncio.Event().wait()
        return {"status": 200, "text": "{}"}

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        self.front += 1


class EditorTab(Page):
    """Tab whose model state follows the URL: Edit loads a draft model, View keeps it."""

    def __init__(self, url, *, version="7", editable=True, dirty_polls=0, blocked_toggles=0):
        super().__init__(url)
        self.version = version
        self.editable = editable
        self.dirty_polls = dirty_polls
        self.blocked_toggles = blocked_toggles
        self.gotos = []
        self.toggled = 0
        self.escapes = 0
        self.keyboard = SimpleNamespace(press=self._escape)
        self.first = SimpleNamespace(click=self._click)

    def locator(self, selector):
        assert selector == EDIT_TOGGLE
        return self

    async def _escape(self, key):
        self.escapes += 1

    async def _click(self, timeout):  # noqa: ASYNC109 - mirrors the Playwright signature
        if self.blocked_toggles:
            self.blocked_toggles -= 1
            raise TimeoutError("covered by an overlay")
        self.toggled += 1
        self.url = self.url.replace("/edit", "", 1)

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url
        self.version = "7"

    async def evaluate(self, script, arguments):
        if script is MODEL_STATE_SCRIPT:
            dirty = self.dirty_polls > 0
            self.dirty_polls = max(0, self.dirty_polls - 1)
            return {"url": self.url, "version": self.version, "dirty": dirty}
        if script is REPORT_READY_SCRIPT:
            return {"url": self.url, "report_id": arguments}
        if "getReport" in script and "editable" in script:
            return self.editable
        return await super().evaluate(script, arguments)


def browser_for(root, *pages, launcher=None, launch_timeout_s=0.05, idle_close_s=0):
    browser = ChromeBrowser(
        BrowserConfig(
            root,
            root / "journal",
            chrome_binary=root / "chrome",
            timeout_ms=20,
            launch_timeout_s=launch_timeout_s,
            idle_close_s=idle_close_s,
        ),
        launcher=launcher,
    )
    if launcher is None:
        browser._browser = FakeBrowser(list(pages))
    return browser


def session_for(root, *pages):
    return ReportSession(
        browser_for(root, *pages), Config(REPORT, root, root / "journal", timeout_ms=20)
    )


@pytest.mark.parametrize(
    "reference",
    [
        REPORT,
        f"  {REPORT} ",
        BASE,
        BASE + "/page/p_test?params=%7B%7D#x",
        f"https://lookerstudio.google.com/reporting/{REPORT}/edit",
        f"https://lookerstudio.google.com/u/2/reporting/{REPORT}/page/p_1/edit",
    ],
)
def test_report_reference_accepts_uuid_and_known_urls(reference):
    assert parse_report_reference(reference) == REPORT


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "not-a-report",
        f"https://example.com/reporting/{REPORT}",
        f"https://lookerstudio.google.com/datasources/{REPORT}",
        f"http://lookerstudio.google.com/reporting/{REPORT}",
        f"https://lookerstudio.google.com/reporting/{REPORT}0",
    ],
)
def test_report_reference_rejects_foreign_or_malformed(reference):
    with pytest.raises(StudioError) as error:
        parse_report_reference(reference)
    assert error.value.code == "INVALID_REPORT_REFERENCE"


def test_tab_report_id_and_mode():
    assert tab_report_id(BASE + "/page/p_1") == REPORT
    assert tab_report_id("https://accounts.google.com/ServiceLogin") is None
    signin = "https://accounts.google.com/v3/signin/confirmidentifier?authuser=0&continue="
    assert tab_report_id(signin + quote(BASE + "/page/p_1", safe="")) == REPORT
    assert tab_report_id(signin + quote("https://example.com/reporting/" + REPORT)) is None
    assert tab_report_id("http://accounts.google.com/x?continue=" + quote(BASE)) is None
    assert tab_report_id("https://lookerstudio.google.com/datasources/x") is None
    assert tab_mode(BASE, REPORT) == "view"
    assert tab_mode(BASE + "/page/p_1/edit", REPORT) == "edit"
    assert tab_mode(BASE + "/unknown", REPORT) == "unknown"


def test_browser_config_defaults_and_overrides(monkeypatch, tmp_path):
    for name in ("DATASTUDIO_PROFILE_DIR", "DATASTUDIO_STATE_DIR", "DATASTUDIO_CHROME_BINARY"):
        monkeypatch.delenv(name, raising=False)
    config = BrowserConfig.from_env()
    assert config.profile_dir == Path.home() / ".datastudio-mcp" / "profile"
    assert config.state_dir == Path.home() / ".datastudio-mcp" / "state"
    assert config.chrome_binary.name == "Google Chrome"
    monkeypatch.setenv("DATASTUDIO_PROFILE_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("DATASTUDIO_CHROME_BINARY", str(tmp_path / "chrome"))
    config = BrowserConfig.from_env()
    assert config.profile_dir == (tmp_path / "p").resolve()
    assert config.chrome_binary == (tmp_path / "chrome").resolve()


def test_report_tabs_only_match_known_report_routes(tmp_path):
    other = "11111111-2222-4333-8444-555555555555"
    pages = [
        Page(BASE),
        Page(f"https://lookerstudio.google.com/u/1/reporting/{other}/page/p/edit"),
        Page("https://accounts.google.com/ServiceLogin"),
        Page("https://lookerstudio.google.com/datasources/abc"),
    ]
    browser = browser_for(tmp_path, *pages)
    assert [(p.url, rid) for p, rid in browser.report_tabs()] == [
        (BASE, REPORT),
        (pages[1].url, other),
    ]


def test_ensure_launches_chrome_once_and_waits_for_port(tmp_path):
    launched = []

    def launcher(command):
        launched.append(command)
        (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")

    browser = browser_for(tmp_path, launcher=launcher)
    attempts = []

    async def fake_attach(port):
        attempts.append(port)
        return FakeBrowser([])

    browser._attach = fake_attach

    async def scenario():
        result = await browser.ensure()
        assert result.is_connected()
        assert len(launched) == 1
        assert launched[0][0] == str(tmp_path / "chrome")
        assert f"--user-data-dir={tmp_path}" in launched[0]
        assert "--remote-debugging-port=0" in launched[0]
        assert "--headless=new" in launched[0]  # No window unless a sign-in needs one.
        assert attempts == [9222]
        assert await browser.ensure() is result
        assert len(launched) == 1

    asyncio.run(scenario())


def test_ensure_replaces_stale_port_file_and_reports_launch_timeout(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("1\n/devtools/browser/dead\n")
    launched = []

    def launcher(command):
        launched.append(command)
        assert not (tmp_path / "DevToolsActivePort").exists()

    browser = browser_for(tmp_path, launcher=launcher)

    async def never(port):
        raise RuntimeError("refused")

    browser._attach = never

    async def scenario():
        with pytest.raises(StudioError) as error:
            await browser.ensure()
        assert error.value.code == "BROWSER_UNAVAILABLE"
        assert len(launched) == 1

    asyncio.run(scenario())


def test_ensure_retries_a_busy_live_chrome_instead_of_relaunching_it(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/live\n")
    (tmp_path / "SingletonLock").symlink_to(f"host-{os.getpid()}")
    launched = []
    browser = browser_for(tmp_path, launcher=launched.append, launch_timeout_s=5)
    attempts = []

    async def busy_once(port):
        attempts.append(port)
        if len(attempts) == 1:
            raise TimeoutError("busy")
        return FakeBrowser([])

    browser._attach = busy_once

    async def scenario():
        assert (await browser.ensure()).is_connected()
        assert launched == [] and attempts == [9222, 9222]
        assert (tmp_path / "DevToolsActivePort").exists()

    asyncio.run(scenario())


def test_require_relaunches_chrome_that_quit(tmp_path):
    launched = []
    browser = browser_for(tmp_path, launcher=launched.append, launch_timeout_s=0.3)

    async def scenario():
        assert await browser.connect() is None
        with pytest.raises(StudioError) as error:
            await browser.require()
        assert error.value.code == "BROWSER_UNAVAILABLE"
        assert len(launched) == 1 and "--headless=new" in launched[0]

    asyncio.run(scenario())


def test_idle_chrome_quits_gracefully_and_busy_calls_keep_it(tmp_path, monkeypatch):
    browser = browser_for(tmp_path, launcher=lambda command: None, idle_close_s=0.02)
    monkeypatch.setattr(browser, "_profile_in_use", lambda: True)
    sent = []

    class Cdp:
        async def send(self, method):
            sent.append(method)

    class Live(FakeBrowser):
        async def new_browser_cdp_session(self):
            return Cdp()

    async def attach(port):
        return Live([])

    browser._attach = attach
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/live\n")

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()  # A tool call in flight: the stamp stays fresh, no quit.
        task = asyncio.create_task(browser.close_when_idle(lock))
        await asyncio.sleep(0.05)
        assert sent == [] and browser.idle_for() < 0.05
        lock.release()
        await asyncio.sleep(0.1)
        task.cancel()
        assert sent and set(sent) == {"Browser.close"}

    asyncio.run(scenario())


def test_editor_write_is_rejected_before_browser_dispatch(tmp_path):
    async def scenario():
        page = Page(BASE + "/page/p_test/edit")
        session = session_for(tmp_path, page)
        with pytest.raises(StudioError) as error:
            await session.post("updatePage", {"reportId": REPORT})
        assert error.value.code == "EDITOR_MODE_UNSAFE"
        assert not error.value.outcome_unknown
        assert page.calls == []
        assert await session.post("getReport", {"reportId": REPORT}) == {}
        assert len(page.calls) == 1

    asyncio.run(scenario())


def test_every_write_marks_the_draft_model_stale_and_acquire_reloads_it(tmp_path):
    async def scenario():
        tab = EditorTab(BASE)
        session = session_for(tmp_path, tab)
        await session.post("updatePage", {"reportId": REPORT})
        assert session.stale
        await session.acquire()
        assert tab.gotos == [BASE + "/edit"]
        assert not session.stale
        await session.acquire()
        assert len(tab.gotos) == 1  # Nothing changed since: no needless reload.

    asyncio.run(scenario())


def test_duplicate_report_tab_blocks_viewer_writes(tmp_path):
    async def scenario():
        viewer, editor = Page(BASE), Page(BASE + "/edit")
        with pytest.raises(StudioError) as error:
            await session_for(tmp_path, viewer, editor).post("updatePage", {"reportId": REPORT})
        assert error.value.code == "AMBIGUOUS_REPORT_TAB"
        assert viewer.calls == editor.calls == []

    asyncio.run(scenario())


def test_datasource_editor_blocks_publication_before_dispatch(tmp_path):
    async def scenario():
        viewer = Page(BASE)
        source_editor = Page("https://datastudio.google.com/u/0/datasources/source/edit")
        with pytest.raises(StudioError) as error:
            await session_for(tmp_path, viewer, source_editor).post(
                "publishDatasource", {"reportId": REPORT, "draft": {"datasourceId": "source"}}
            )
        assert error.value.code == "DATASOURCE_EDITOR_OPEN"
        assert viewer.calls == source_editor.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint,unknown", [("updatePage", True), ("getReport", False)])
def test_stalled_renderer_has_bounded_classified_outcome(tmp_path, endpoint, unknown):
    async def scenario():
        page = Page(BASE, hang=True)
        with pytest.raises(StudioError) as error:
            await session_for(tmp_path, page).post(endpoint, {"reportId": REPORT})
        assert error.value.code == "TRANSPORT_FAILURE"
        assert error.value.outcome_unknown is unknown
        assert len(page.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("selector", ["/u/2", "?authuser=1", "?authuser=other%40example.com"])
def test_open_tab_keeps_account_and_opens_view(tmp_path, selector):
    from urllib.parse import parse_qs, urlparse

    reference = f"https://lookerstudio.google.com/reporting/{REPORT}/edit"
    reference = (reference.replace("/reporting/", selector + "/reporting/")
                 if selector.startswith("/") else reference + selector)
    navigated = []

    async def goto(url, **kwargs):
        navigated.append(url)

    async def new_page():
        return SimpleNamespace(goto=goto)

    browser = browser_for(tmp_path)
    browser._browser.contexts[0].new_page = new_page
    asyncio.run(browser.open_tab(reference))
    parsed = urlparse(navigated[0])
    assert parsed.path == f"/reporting/{REPORT}"
    expected = "2" if selector.startswith("/") else parse_qs(selector[1:])["authuser"][0]
    assert parse_qs(parsed.query) == {"authuser": [expected]}


def test_refresh_reloads_through_editor_route_and_keeps_account_selector(tmp_path):
    tab = EditorTab(BASE.replace("/u/0", "") + "?authuser=other%40example.com")
    asyncio.run(session_for(tmp_path, tab).refresh("p_next"))
    assert tab.gotos == [
        f"https://datastudio.google.com/reporting/{REPORT}/page/p_next/edit"
        "?authuser=other%40example.com"
    ]
    assert tab.url.endswith("/page/p_next?authuser=other%40example.com")


def test_acquire_leaves_editor_through_view_toggle_after_autosave(tmp_path):
    tab = EditorTab(BASE + "/page/p_1/edit", dirty_polls=3)
    page = asyncio.run(session_for(tmp_path, tab).acquire())
    assert page is tab and tab.toggled == 1 and tab.gotos == []
    assert tab.url == BASE + "/page/p_1"


def test_acquire_keeps_the_view_tab_and_closes_autosaved_editor_duplicates(tmp_path):
    viewer, editor = EditorTab(BASE), EditorTab(BASE + "/edit", dirty_polls=2)
    page = asyncio.run(session_for(tmp_path, editor, viewer).acquire())
    assert page is viewer and editor.closed and not viewer.closed
    assert editor.dirty_polls == 0  # Closed only after its model was clean.


def test_acquire_navigates_to_edit_route_when_viewer_shows_published_revision(tmp_path):
    tab = EditorTab(BASE, version="NaN", editable=True)
    asyncio.run(session_for(tmp_path, tab).acquire())
    assert tab.gotos == [BASE + "/edit"] and tab.toggled == 1


def test_acquire_keeps_plain_viewer_for_view_only_account(tmp_path):
    tab = EditorTab(BASE, version="NaN", editable=False)
    page = asyncio.run(session_for(tmp_path, tab).acquire())
    assert page is tab and tab.gotos == [] and tab.toggled == 0


def test_acquire_reloads_editor_when_overlay_blocks_the_view_toggle(tmp_path):
    tab = EditorTab(BASE + "/edit", blocked_toggles=3)
    asyncio.run(session_for(tmp_path, tab).acquire())
    assert tab.gotos == [BASE + "/edit"] and tab.escapes == 2


def test_profile_lock_of_a_dead_chrome_does_not_block_relaunch(tmp_path):
    browser = browser_for(tmp_path)
    assert not browser._profile_in_use()
    (tmp_path / "SingletonLock").symlink_to(f"host-{os.getpid()}")
    assert browser._profile_in_use()
    (tmp_path / "SingletonLock").unlink()
    (tmp_path / "SingletonLock").symlink_to("host-999999999")
    assert not browser._profile_in_use()



def test_acquire_opens_the_view_route_when_the_view_toggle_never_answers(tmp_path):
    # The editor's toggle stays covered; after one editor reload the View route is opened.
    tab = EditorTab(BASE + "/edit", blocked_toggles=100)
    page = asyncio.run(session_for(tmp_path, tab).acquire())
    assert page is tab and tab.gotos == [BASE + "/edit", BASE]
    assert tab.url == BASE
