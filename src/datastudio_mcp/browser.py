"""Existing Chrome session transport. Credentials never cross the page boundary."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import Page, Playwright, async_playwright

REPORT_ID = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
ORIGINS = {"https://datastudio.google.com", "https://lookerstudio.google.com"}
READ_ENDPOINTS = frozenset(
    {
        "getReport",
        "getClaimedDatasources",
        "getBlockDatasource",
        "getSchema",
        "validateFormula",
        "getShareableList",
        "getRevisions",
        "getRevision",
        "getSharing",
    }
)
WRITE_ENDPOINTS = frozenset(
    {
        "updatePage",
        "updateReport",
        "createPages",
        "deletePages",
        "updateReportNavigationInfo",
        "publishDatasource",
        "updateDataAccessControl",
        "claimDatasourceForReport",
        "deleteClaim",
        "publishReport",
        "createNamedRevision",
        "restoreRevision",
        "updateRevision",
        "renameDatasource",
    }
)
# The Data Studio home page: a neutral tab for work that belongs to no report. It runs no
# editor, so nothing on it can autosave over a datasource the API is changing.
HOME_PATH = re.compile(r"^(?:/u/[0-9]+)?/navigation/")
HOME_URL = "https://datastudio.google.com/navigation/reporting"


def is_report_view(url: str, report_id: str) -> bool:
    """Only known view routes are safe for API writes; unknown routes fail closed."""
    parsed = urlparse(url)
    return (
        f"{parsed.scheme}://{parsed.netloc}" in ORIGINS
        and re.fullmatch(
            rf"(?:/u/[0-9]+)?/reporting/{re.escape(report_id)}(?:/page/[^/]+)?/?",
            parsed.path,
        )
        is not None
    )


class StudioError(Exception):
    """A classified failure; raw response bodies may contain private data."""

    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False):
        super().__init__(message)
        self.code = code
        self.outcome_unknown = outcome_unknown


CHROME_BINARY = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
HOME = Path("~/.datastudio-mcp").expanduser()
LOGIN_HOSTS = frozenset({"accounts.google.com"})
# Prelude of every in-page API call: `request(endpoint, body)` posts with the page's own
# session, so cookies and tokens never leave the browser. Callers append their body and
# the closing brace; `args.timeout` bounds each request.
REQUEST_SCRIPT = """async (args) => {
    const token = decodeURIComponent(
        (document.cookie.match(/(?:^|; )RAP_XSRF_TOKEN=([^;]+)/)||[])[1]||'');
    const appVersion = performance.getEntriesByType('resource').map(r=>{
        try{return new URL(r.name).searchParams.get('appVersion')}
        catch{return null}
    }).find(Boolean);
    const account = location.pathname.match(/^\\/u\\/(\\d+)\\//)?.[1];
    const authuser = new URL(location.href).searchParams.get('authuser');
    const request = async (endpoint, body) => {
        if (!token || !appVersion) return {error:'SESSION_CONTEXT_MISSING'};
        const controller = new AbortController();
        const timer = setTimeout(()=>controller.abort(), args.timeout);
        try {
            const prefix = account !== undefined ? `/u/${account}` : (authuser ? '' : '/u/0');
            const url = `${prefix}/${endpoint}?appVersion=` + encodeURIComponent(appVersion) +
                (account === undefined && authuser
                    ? '&authuser=' + encodeURIComponent(authuser) : '');
            const response = await fetch(url, {
                method:'POST', credentials:'include', signal:controller.signal,
                headers:{'content-type':'application/json','x-rap-xsrf-token':token},
                body:JSON.stringify(body)
            });
            const text = (await response.text()).replace(/^\\)\\]\\}'\\s*/, '');
            let json = null;
            try {json = JSON.parse(text)} catch {}
            return {status:response.status, text, json};
        } catch {return {error:'TRANSPORT_FAILURE'}}
        finally {clearTimeout(timer)}
    };
"""
SESSION_READY_SCRIPT = """() => /(?:^|; )RAP_XSRF_TOKEN=/.test(document.cookie) &&
    performance.getEntriesByType('resource').some(r => {
        try {return Boolean(new URL(r.name).searchParams.get('appVersion'))}
        catch {return false}
    })"""
REPORT_READY_SCRIPT = """reportId => {
    const injector = window.angular?.element(document.body).injector();
    // Angular's root injector exists before the application
    // module registers this provider during navigation.
    if (!injector || !injector.has('reportContextService')) return null;
    const report = injector.get('reportContextService').getReport();
    if (!report || report.getId() !== reportId) return null;
    const pageId = location.pathname.match(/\\/page\\/([^/]+)/)?.[1];
    // Viewer URLs may carry a page alias unknown to the model; then the
    // report itself is the readiness owner.
    const owner = (pageId && report.getPage(pageId)) || report;
    if (report.isReady$?.value !== true || owner.isReady$?.value !== true) return null;
    return {report_id:reportId,
        editor_report_version:String(report.getVersion()),
        url:location.href};
}"""


def parse_datasource_reference(reference: str) -> str:
    """Accept a bare datasource UUID or a datasource editor URL; fail closed otherwise."""
    text = reference.strip()
    if REPORT_ID.fullmatch(text):
        return text
    parsed = urlparse(text)
    match = re.search(r"/datasources/([^/?#]+)", parsed.path)
    if f"{parsed.scheme}://{parsed.netloc}" in ORIGINS and match and REPORT_ID.fullmatch(match[1]):
        return match[1]
    raise StudioError(
        "INVALID_DATASOURCE_REFERENCE", "Provide a datasource UUID or a datasource URL"
    )


def tab_report_id(url: str) -> str | None:
    """Report UUID of a report tab, including a Google sign-in tab that will return to it."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc in LOGIN_HOSTS:
        target = parse_qs(parsed.query).get("continue", [""])[0]
        return tab_report_id(target) if target else None
    if f"{parsed.scheme}://{parsed.netloc}" not in ORIGINS:
        return None
    match = re.search(r"/reporting/([^/]+)(?:/|$)", parsed.path)
    return match[1] if match and REPORT_ID.fullmatch(match[1]) else None


def tab_mode(url: str, report_id: str) -> str:
    """ "view" is safe for writes, "edit" is a live editor, anything else fails closed."""
    if is_report_view(url, report_id):
        return "view"
    if tab_report_id(url) == report_id and (
        re.fullmatch(
            rf"(?:/u/[0-9]+)?/reporting/{re.escape(report_id)}(?:/page/[^/]+)?/edit/?",
            urlparse(url).path,
        )
        is not None
    ):
        return "edit"
    return "unknown"


def parse_report_reference(reference: str) -> str:
    """Accept a bare report UUID or any known-origin report URL; fail closed otherwise."""
    text = reference.strip()
    if REPORT_ID.fullmatch(text):
        return text
    report_id = tab_report_id(text)
    if report_id is None:
        raise StudioError(
            "INVALID_REPORT_REFERENCE", "Provide a report UUID or a Datastudio report URL"
        )
    return report_id


def report_account(reference: str) -> str | None:
    """Google account selector from a report URL or its sign-in redirect."""
    parsed = urlparse(reference.strip())
    if parsed.scheme == "https" and parsed.netloc in LOGIN_HOSTS:
        return report_account(parse_qs(parsed.query).get("continue", [""])[0])
    if f"{parsed.scheme}://{parsed.netloc}" not in ORIGINS:
        return None
    match = re.match(r"/u/([0-9]+)/", parsed.path)
    return match[1] if match else parse_qs(parsed.query).get("authuser", [None])[0]


@dataclass(frozen=True)
class BrowserConfig:
    """Profile-level settings; no report is fixed at process start."""

    profile_dir: Path
    state_dir: Path
    chrome_binary: Path = CHROME_BINARY
    timeout_ms: int = 30_000
    launch_timeout_s: float = 15.0
    # Chrome quits after this long without a tool call in any MCP process; 0 keeps it.
    idle_close_s: float = 600.0

    @classmethod
    def from_env(cls) -> BrowserConfig:
        def path(name: str, default: Path) -> Path:
            return Path(os.environ.get(name) or default).expanduser().resolve()

        return cls(
            profile_dir=path("DATASTUDIO_PROFILE_DIR", HOME / "profile"),
            state_dir=path("DATASTUDIO_STATE_DIR", HOME / "state"),
            chrome_binary=path("DATASTUDIO_CHROME_BINARY", CHROME_BINARY),
            idle_close_s=float(os.environ.get("DATASTUDIO_IDLE_CLOSE_S") or 600),
        )


@dataclass(frozen=True)
class Config:
    """Settings of one session; shared paths come from the browser.

    report_id None is the datasource scope: work on datasources that belongs to no report.
    """

    report_id: str | None
    profile_dir: Path
    state_dir: Path
    timeout_ms: int = 30_000

    @classmethod
    def for_report(cls, browser: BrowserConfig, report_id: str | None) -> Config:
        return cls(report_id, browser.profile_dir, browser.state_dir, browser.timeout_ms)


class Browser:
    """One Chrome profile, one CDP connection, shared by every report session."""

    def __init__(
        self,
        config: BrowserConfig,
        *,
        launcher: Callable[[list[str]], Any] | None = None,
    ):
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: PlaywrightBrowser | None = None
        self._connect_lock = asyncio.Lock()
        self._launcher = launcher or self._spawn

    def _port(self) -> int | None:
        try:
            lines = (self.config.profile_dir / "DevToolsActivePort").read_text().splitlines()
            port = int(lines[0])
            if 0 < port < 65536 and lines[1].startswith("/devtools/browser/"):
                return port
        except (OSError, IndexError, ValueError):
            pass
        return None

    async def _attach(self, port: int) -> PlaywrightBrowser:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}", timeout=self.config.timeout_ms
        )

    def touch(self) -> None:
        """Refresh the activity stamp every MCP process sharing this profile reads."""
        self.config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.config.state_dir / "last-activity").touch()

    def idle_for(self) -> float:
        try:
            return time.time() - (self.config.state_dir / "last-activity").stat().st_mtime
        except OSError:
            return float("inf")

    async def close_when_idle(self, tool_lock: asyncio.Lock) -> None:
        """Quit Chrome once no MCP process has used it for idle_close_s.

        A graceful quit flushes cookies, so the Google sign-in stays in the profile and
        the next tool call relaunches Chrome headless without asking for it again.
        """
        limit = self.config.idle_close_s
        while limit > 0:
            await asyncio.sleep(min(60.0, limit / 2))
            if tool_lock.locked():  # A long call of this process is still activity.
                self.touch()
                continue
            if self.idle_for() < limit or not self._profile_in_use():
                continue
            async with tool_lock:
                browser = await self.connect()
                if browser is None:
                    continue
                cdp = await browser.new_browser_cdp_session()
                with contextlib.suppress(Exception):  # The connection drops as Chrome quits.
                    await cdp.send("Browser.close")
                await self.close()

    async def connect(self) -> PlaywrightBrowser | None:
        """Attach to a running profile; None when it is not running with a debug port."""
        self.touch()
        async with self._connect_lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            port = self._port()
            if port is None:
                return None
            try:
                self._browser = await self._attach(port)
            except Exception:
                self._browser = None
                return None
            return self._browser

    async def require(self) -> PlaywrightBrowser:
        # Chrome may have quit while idle (close_when_idle): start it again headless.
        return await self.ensure()

    async def ensure(self, *, headless: bool = True) -> PlaywrightBrowser:
        """Attach, or launch the profile and attach once its debug port appears.

        The MCP's Chrome runs headless: no window. A visible one is opened only for a
        Google sign-in (see `relaunch`); the session lives in the profile either way.
        """
        browser = await self.connect()
        if browser is not None:
            return browser
        # A live Chrome that failed one attach (busy) is retried, never relaunched: a
        # second launch hands off to it and exits, and the launch would delete the live
        # port file, leaving the profile unreachable until Chrome restarts.
        if not self._profile_in_use():
            self._launch(headless=headless)
        deadline = time.monotonic() + self.config.launch_timeout_s
        while True:
            browser = await self.connect()
            if browser is not None:
                return browser
            if time.monotonic() >= deadline:
                raise StudioError(
                    "BROWSER_UNAVAILABLE",
                    f"Chrome did not expose a debug port within "
                    f"{self.config.launch_timeout_s:g}s. If this profile is already open "
                    f"without remote debugging, quit that window. Binary: "
                    f"{self.config.chrome_binary}; profile: {self.config.profile_dir}",
                )
            await asyncio.sleep(0.25)

    def _launch(self, *, headless: bool = True) -> None:
        self.config.profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A crashed Chrome leaves its port file behind; a fresh launch rewrites it.
        (self.config.profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
        self._launcher(
            [
                str(self.config.chrome_binary),
                f"--user-data-dir={self.config.profile_dir}",
                "--remote-debugging-port=0",
                *(["--headless=new", "--window-size=1920,1080"] if headless else []),
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ]
        )

    async def headless(self) -> bool:
        browser = await self.require()
        cdp = await browser.new_browser_cdp_session()
        return "HeadlessChrome/" in (await cdp.send("Browser.getVersion"))["userAgent"]

    def _profile_in_use(self) -> bool:
        """Chrome's profile lock: a symlink to "<host>-<pid>"; stale after a hard exit."""
        try:
            pid = int(os.readlink(self.config.profile_dir / "SingletonLock").rsplit("-", 1)[1])
            os.kill(pid, 0)
        except (OSError, IndexError, ValueError):
            return False
        return True

    async def relaunch(self, *, headless: bool) -> PlaywrightBrowser:
        """Quit the profile's Chrome and start it again, headless or with a window.

        Used only around a Google sign-in, when no editor holds unsaved work.
        """
        browser = await self.require()
        cdp = await browser.new_browser_cdp_session()
        with contextlib.suppress(Exception):  # The connection drops as Chrome quits.
            await cdp.send("Browser.close")
        await self.close()
        deadline = time.monotonic() + self.config.launch_timeout_s
        while self._profile_in_use():
            if time.monotonic() >= deadline:
                raise StudioError("BROWSER_UNAVAILABLE", "Chrome did not quit to relaunch")
            await asyncio.sleep(0.25)
        return await self.ensure(headless=headless)

    @staticmethod
    def _spawn(command: list[str]) -> None:
        # Fixed local binary, argument list, no shell; detached so MCP exit keeps Chrome.
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def pages(self) -> list[Page]:
        if not (self._browser and self._browser.is_connected()):
            return []
        return [p for context in self._browser.contexts for p in context.pages]

    def report_tabs(self) -> list[tuple[Page, str]]:
        tabs = []
        for page in self.pages():
            report_id = tab_report_id(page.url)
            if report_id is not None:
                tabs.append((page, report_id))
        return tabs

    async def open_tab(self, reference: str) -> Page:
        report_id = parse_report_reference(reference)
        account = report_account(reference)
        url = f"https://lookerstudio.google.com/reporting/{report_id}"
        if account is not None:
            url += "?" + urlencode({"authuser": account})
        browser = await self.require()
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=self.config.timeout_ms,
        )
        return page

    async def close(self) -> None:
        # Stopping the client disconnects CDP; never close the user's Chrome.
        if self._playwright:
            await self._playwright.stop()
        self._playwright = None
        self._browser = None


# Native model state of a report tab: None while loading. A digit version means the
# editable draft is loaded; a published revision (publishing on, View reloaded) has none.
MODEL_STATE_SCRIPT = """reportId => {
    const injector = window.angular?.element(document.body).injector();
    if (!injector || !injector.has('reportContextService')) return null;
    const report = injector.get('reportContextService').getReport();
    if (!report || report.getId() !== reportId || report.isReady$?.value !== true) return null;
    const pages = report.getPages();
    const models = [report, ...pages, ...pages.flatMap(page => page.getComponents())];
    return {version: String(report.getVersion()), url: location.href,
        dirty: models.some(model => Object.values(model.isChanged || {}).some(Boolean)),
        toggle: Boolean(document.querySelector('button.edit-mode-toggle-button'))};
}"""
# Leaving the editor: the in-app View toggle keeps the loaded draft model; a navigation
# would load the published revision instead.
EDIT_TOGGLE = "button.edit-mode-toggle-button"


class ReportSession:
    """Report- or datasource-scoped transport over the shared browser.

    A report session talks through that report's single View tab. The datasource scope
    (report_id None) talks through a Data Studio home tab, opened on demand.
    """

    def __init__(self, browser: Browser, config: Config):
        self.browser = browser
        self.config = config
        # Set when something the loaded model caches (datasource schemas) changed
        # server-side; the next acquire() reloads the draft model.
        self.stale = False
        self.dispatches = 0  # Write requests sent; lets callers tell "nothing was sent".

    async def connect(self) -> PlaywrightBrowser:
        return await self.browser.require()


    def _tabs(self, browser: PlaywrightBrowser) -> list[Page]:
        return [
            page
            for context in browser.contexts
            for page in context.pages
            if tab_report_id(page.url) == self.config.report_id
            and urlparse(page.url).netloc not in LOGIN_HOSTS
        ]

    async def _state(self, page: Page) -> dict[str, Any] | None:
        try:
            return await page.evaluate(MODEL_STATE_SCRIPT, self.config.report_id)
        except Exception:
            return None  # Navigation destroyed the context; poll again.

    async def _settled(self, page: Page, deadline: float) -> dict[str, Any]:
        """Wait until the editor has autosaved: model clean for two consecutive polls."""
        clean = 0
        while True:
            state = await self._state(page)
            clean = clean + 1 if state and not state["dirty"] else 0
            if clean >= 2:
                return state
            if time.monotonic() >= deadline:
                raise StudioError(
                    "EDITOR_DIRTY",
                    "The editor kept unsaved changes; wait for autosave and retry",
                )
            await asyncio.sleep(0.5)

    async def _leave_editor(self, page: Page) -> bool:
        """Click the editor's View toggle; Escape closes menus/dialogs over it first."""
        for attempt in range(3):
            try:
                await page.locator(EDIT_TOGGLE).first.click(timeout=4000)
                # Wait for the route to leave the editor, so a slow switch is not toggled
                # back by the next poll.
                for _ in range(20):
                    if tab_mode(page.url, self.config.report_id) != "edit":
                        return True
                    await asyncio.sleep(0.25)
                return True
            except Exception:
                if attempt < 2:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.4)
        return False

    async def _editable(self, page: Page) -> bool:
        result = await page.evaluate(
            REQUEST_SCRIPT
            + """const r = await request('getReport', {reportId: args.reportId, draftRequestMode: 1,
                includePages: false, updateViewedDate: false, updateFirestore: false});
            return r.json?.reportConfig?.shareable?.editable === true;}""",
            {"reportId": self.config.report_id, "timeout": self.config.timeout_ms},
        )
        return result is True

    def _route(self, page: Page, page_id: str | None, edit: bool) -> str:
        parsed = urlparse(page.url)
        prefix = parsed.path.split("/reporting/", 1)[0]
        url = f"{parsed.scheme}://{parsed.netloc}{prefix}/reporting/{self.config.report_id}"
        if page_id is None:
            match = re.search(r"/page/([^/]+)", parsed.path)
            page_id = match[1] if match else None
        if page_id is not None:
            url += f"/page/{quote(page_id, safe='')}"
        if edit:
            url += "/edit"
        account = report_account(page.url)
        if account is not None and not prefix:
            url += "?" + urlencode({"authuser": account})
        return url

    async def acquire(self, page_id: str | None = None, *, reload: bool = False) -> Page:
        """Bring this report into the state every tool needs, whatever the user left open.

        One tab; the draft model loaded in View (the editor's own View toggle, so publishing
        cannot substitute the published revision); pending editor changes autosaved first.
        reload (or a page change) reloads the draft through the editor route. View-only
        access keeps the plain viewer. Never discards unsaved editor changes.
        """
        if self.config.report_id is None:
            return await self.page()
        try:
            return await self._acquire(page_id, reload)
        except StudioError:
            raise
        except Exception as exc:  # Playwright navigation/evaluation failures.
            raise StudioError(
                "EDITOR_NOT_READY", f"Report tab could not be prepared ({type(exc).__name__})"
            ) from exc

    async def _acquire(self, page_id: str | None, reload: bool) -> Page:
        # Relaunches Chrome headless when it quit while idle; the sign-in is in the profile.
        browser = await self.browser.require()
        reload = reload or self.stale
        deadline = time.monotonic() + max(60.0, self.config.timeout_ms / 1000 * 3)
        tabs = self._tabs(browser)
        if any(
            tab_report_id(p.url) == self.config.report_id and urlparse(p.url).netloc in LOGIN_HOSTS
            for context in browser.contexts
            for p in context.pages
        ):
            # Opening yet another tab would only redirect to sign-in again.
            raise StudioError("AUTH_REQUIRED", "Sign in to Google in the MCP Chrome window")
        if not tabs:
            tab = await self.browser.open_tab(self.config.report_id)
            tabs = [tab]
        # Keep a View tab when there is one; edits in the others are autosaved first.
        tabs.sort(key=lambda tab: tab_mode(tab.url, self.config.report_id) != "view")
        for extra in tabs[1:]:
            if tab_mode(extra.url, self.config.report_id) == "edit":
                await self._settled(extra, deadline)
            await extra.close()
        page = tabs[0]
        navigated = False
        current = re.search(r"/page/([^/]+)", urlparse(page.url).path)
        if reload or (page_id is not None and (current[1] if current else None) != page_id):
            if tab_mode(page.url, self.config.report_id) == "edit":
                await self._settled(page, deadline)
            await page.goto(
                self._route(page, page_id, edit=True),
                wait_until="domcontentloaded",
                timeout=self.config.timeout_ms,
            )
            navigated = True
        editable: bool | None = None
        stuck = 0  # failed attempts to leave the editor by its View toggle
        while True:
            if time.monotonic() >= deadline:
                raise StudioError(
                    "EDITOR_NOT_READY", "Report tab did not reach the draft View; inspect the tab"
                )
            if urlparse(page.url).netloc in LOGIN_HOSTS:
                raise StudioError(
                    "AUTH_REQUIRED", "Sign in to Google in the MCP Chrome window, then retry"
                )
            state = await self._state(page)
            if state is None:
                await asyncio.sleep(0.3)
                continue
            mode = tab_mode(state["url"], self.config.report_id)
            if mode == "view" and state["version"].isdigit():
                self.stale = False
                return page
            if mode == "edit":
                state = await self._settled(page, deadline)
                if await self._leave_editor(page):
                    continue
                stuck += 1
                # A dialog or side panel the user left open covers the toggle. Edits are
                # autosaved by now: reload the editor (closing every overlay) and retry;
                # if the toggle still does not answer, open the report's View route itself.
                await page.goto(
                    self._route(page, page_id, edit=stuck < 2),
                    wait_until="domcontentloaded",
                    timeout=self.config.timeout_ms,
                )
                navigated = navigated and stuck < 2
                continue
            if editable is None:
                editable = await self._editable(page)
            if not editable:
                return page  # View-only account: the plain viewer is all there is.
            if navigated and mode == "view":
                raise StudioError(
                    "EDITOR_NOT_READY", "Edit route returned to View without a draft model"
                )
            await page.goto(
                self._route(page, page_id, edit=True),
                wait_until="domcontentloaded",
                timeout=self.config.timeout_ms,
            )
            navigated = True

    async def page(self) -> Page:
        browser = await self.connect()
        pages = [p for context in browser.contexts for p in context.pages]
        if self.config.report_id is None:
            return await self._home(browser, pages)
        matches = []
        for page in pages:
            parsed = urlparse(page.url)
            if f"{parsed.scheme}://{parsed.netloc}" not in ORIGINS:
                continue
            match = re.search(r"/reporting/([^/]+)(?:/|$)", parsed.path)
            if match and match[1] == self.config.report_id:
                matches.append(page)
        if not matches:
            raise StudioError(
                "REPORT_NOT_OPEN", "Open the configured report in the connected Chrome window"
            )
        if len(matches) > 1:
            raise StudioError(
                "AMBIGUOUS_REPORT_TAB", "Keep one tab for this report to avoid stale editors"
            )
        return matches[0]

    async def _home(self, browser: PlaywrightBrowser, pages: list[Page]) -> Page:
        for page in pages:
            parsed = urlparse(page.url)
            if f"{parsed.scheme}://{parsed.netloc}" in ORIGINS and HOME_PATH.match(parsed.path):
                return page
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=self.config.timeout_ms)
            # The session token and app version appear once the app shell has loaded.
            async with asyncio.timeout(self.config.timeout_ms / 1000):
                while True:
                    if await page.evaluate(SESSION_READY_SCRIPT):
                        break
                    await asyncio.sleep(0.2)
        except Exception as exc:
            raise StudioError(
                "HOME_NOT_READY",
                "The Data Studio home tab did not load; check the login in the MCP Chrome window",
            ) from exc
        if not HOME_PATH.match(urlparse(page.url).path):
            raise StudioError(
                "AUTH_REQUIRED", "Sign in to Google in the MCP Chrome window, then retry"
            )
        return page

    async def columns(self, datasource_id: str, connector: dict[str, Any]) -> list[dict[str, Any]]:
        """The connector's current columns, as the editor's Refresh fields reads them.

        The first-party access token the connector call needs is minted and spent inside the
        page; it never reaches Python.
        """
        page = await self.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    REQUEST_SCRIPT
                    + """
                    const access = await request('getFirstPartyAccessToken', {});
                    if (access.error || !access.json?.accessToken)
                        return {error:'TOKEN_UNAVAILABLE'};
                    const response = await request('getColumns', {
                        connectorBlockConfig:args.connector, datasourceId:args.datasourceId,
                        googleAuthOAuthToken:access.json.accessToken,
                        isBigQueryConnection:false, useThirdpartyAuth:false});
                    if (response.error) return response;
                    return {status:response.status, columns:response.json?.columns};
                    }""",
                    {
                        "datasourceId": datasource_id,
                        "connector": connector,
                        "timeout": self.config.timeout_ms,
                    },
                ),
                timeout=self.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError("TRANSPORT_FAILURE", "Browser request interrupted") from exc
        if result.get("error"):
            raise StudioError(result["error"], "Connector columns could not be read")
        if result.get("status") != 200 or not isinstance(result.get("columns"), list):
            raise StudioError(
                "CONNECTOR_UNAVAILABLE",
                "The connector returned no column list; check its credentials and table",
            )
        return result["columns"]

    async def post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        if endpoint not in READ_ENDPOINTS | WRITE_ENDPOINTS:
            raise StudioError(
                "UNSUPPORTED_ENDPOINT", "Endpoint is not part of the report-scoped contract"
            )
        if "reportId" in body and body["reportId"] != (self.config.report_id or ""):
            raise StudioError("OUT_OF_SCOPE", "Request targets another report")
        page = await self.page()
        if endpoint == "publishDatasource":
            browser = await self.connect()
            for context in browser.contexts:
                for tab in context.pages:
                    parsed = urlparse(tab.url)
                    if (
                        f"{parsed.scheme}://{parsed.netloc}" in ORIGINS
                        and "/datasources/" in parsed.path
                    ):
                        raise StudioError(
                            "DATASOURCE_EDITOR_OPEN",
                            "Close datasource editor tabs before publication",
                        )
        mutation = endpoint in WRITE_ENDPOINTS
        if (
            mutation
            and self.config.report_id is not None
            and not is_report_view(page.url, self.config.report_id)
        ):
            raise StudioError(
                "EDITOR_MODE_UNSAFE",
                "Save pending edits and switch this report to View before API mutations",
            )
        if mutation:
            # Any dispatched write makes the loaded draft model older than the saved state;
            # the next acquire() reloads it, so no tool ever works from a superseded model.
            self.stale = True
            self.dispatches += 1
        try:
            # Bound the entire CDP evaluation, not only fetch. A suspended renderer
            # may never execute its AbortController timer.
            result = await asyncio.wait_for(
                page.evaluate(
                    REQUEST_SCRIPT
                    + """
                    if (args.reportId) {
                        if (!location.pathname.includes(`/reporting/${args.reportId}`))
                            return {error: 'OUT_OF_SCOPE'};
                        // Recheck inside the browser to catch navigation after the
                        // Python preflight. A live editor could autosave stale state.
                        const viewPath = new RegExp('^(?:/u/[0-9]+)?/reporting/' + args.reportId +
                            '(?:/page/[^/]+)?/?$');
                        if (args.mutation && !viewPath.test(location.pathname))
                            return {error:'EDITOR_MODE_UNSAFE'};
                    } else if (!/^(?:\\/u\\/[0-9]+)?\\/navigation\\//.test(location.pathname)) {
                        return {error: 'OUT_OF_SCOPE'};
                    }
                    const response = await request(args.endpoint, args.body);
                    return response.error ? response : {status:response.status, text:response.text};
                    }""",
                    {
                        "endpoint": endpoint,
                        "body": body,
                        "timeout": self.config.timeout_ms,
                        "reportId": self.config.report_id,
                        "mutation": mutation,
                    },
                ),
                timeout=self.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError(
                "TRANSPORT_FAILURE", "Browser request interrupted", outcome_unknown=mutation
            ) from exc
        if result.get("error"):
            unknown = mutation and result["error"] == "TRANSPORT_FAILURE"
            raise StudioError(
                result["error"],
                "Browser session could not complete the request",
                outcome_unknown=unknown,
            )
        status = result["status"]
        if status in (401, 403):
            raise StudioError("AUTH_REQUIRED", "Login or editing permission is required")
        if status == 409:
            raise StudioError("VERSION_CONFLICT", "Server rejected a stale version; read again")
        if status != 200:
            # The server's own reason (a validation message), never request content.
            reason = " ".join(str(result.get("text") or "").split())[:300]
            raise StudioError(
                f"HTTP_{status}",
                f"{endpoint} returned HTTP {status}" + (f": {reason}" if reason else ""),
                outcome_unknown=mutation and status >= 500,
            )
        if mutation and not result["text"].strip():
            return {}  # Some writes (renameDatasource) answer 200 with an empty body.
        try:
            payload = json.loads(result["text"])
        except ValueError as exc:
            raise StudioError(
                "INVALID_RESPONSE",
                "Expected JSON; session or API contract may have changed",
                outcome_unknown=mutation,
            ) from exc
        if not isinstance(payload, dict) or payload.get("error"):
            raise StudioError(
                "API_ERROR", "API returned an error or unexpected shape", outcome_unknown=mutation
            )
        return payload

    async def refresh(self, page_id: str | None = None) -> dict[str, Any]:
        """Reload the draft model (optionally on another page); never discards editor edits."""
        page = await self.acquire(page_id, reload=True)
        result = await page.evaluate(REPORT_READY_SCRIPT, self.config.report_id)
        if not result:
            raise StudioError("EDITOR_NOT_READY", "Viewer refresh did not finish")
        return result
