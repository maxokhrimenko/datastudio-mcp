"""Open a report by URL or UUID in the MCP Chrome profile and make it the default."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .browser import (
    LOGIN_HOSTS,
    REPORT_READY_SCRIPT,
    StudioError,
    parse_report_reference,
    report_account,
    tab_mode,
    tab_report_id,
)
from .workspace import Workspace


def report_access(native_version: str) -> dict[str, str]:
    """Only an editable report loads with a version; a view-only model reports NaN."""
    if native_version.isdigit():
        return {"access": "edit"}
    return {
        "access": "view",
        "access_note": "This account can only view the report. Reading saved content works; "
        "native inspection and every write are refused because the viewer model carries "
        "no version. Ask for edit access to change it.",
    }


def _login_page(url: str) -> bool:
    return urlparse(url).netloc in LOGIN_HOSTS


def _login_required(report_id: str, url: str, profile_dir: Path) -> dict[str, Any]:
    return {
        "status": "login_required",
        "report_id": report_id,
        "instruction": "A Chrome window of the MCP profile is open on Google sign-in "
        f"({profile_dir}). Sign in there, then call report_open again; the window closes "
        "and the MCP continues in the background",
        "tab_url": url,
    }


def _login_tabs(browser: Any, report_id: str | None = None) -> list[Any]:
    return [page for page in browser.pages() if _login_page(page.url) and (
        report_id is None or tab_report_id(page.url) == report_id)]


async def _show_for_sign_in(browser: Any, reference: str) -> None:
    """Sign-in needs a person: reopen the profile's Chrome with a window on Google sign-in.

    Sign-in is opened directly: a report shared with anyone would load signed out.
    """
    if await browser.headless():
        await browser.relaunch(headless=False)
        tab = await browser.open_tab(reference, sign_in=True)
        await tab.bring_to_front()


async def open_report(
    workspace: Workspace,
    reference: str,
    wait_seconds: int = 90,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Open a report and bring it to the draft View, whatever tabs and modes exist.

    Duplicate tabs are closed (editors only after their autosave), a different account
    selector reopens the report with the requested one, an Edit tab is switched to the
    draft View. Only a Google sign-in needs the user.
    """
    report_id = parse_report_reference(reference)
    browser = workspace.browser
    await browser.ensure()
    # A window left from a sign-in goes back to the background once nothing needs it.
    if (not await browser.headless() and not _login_tabs(browser)
            and not any(tab_mode(page.url, r) == "edit" for page, r in browser.report_tabs())):
        await browser.relaunch(headless=True)
    bundle = workspace.resolve(report_id)
    session = bundle.studio.session
    tabs = [page for page, tab_report in browser.report_tabs() if tab_report == report_id]
    account = report_account(reference)
    if account is not None and any(report_account(tab.url) != account for tab in tabs):
        deadline = clock() + wait_seconds
        for tab in tabs:
            if tab_mode(tab.url, report_id) == "edit":
                await session._settled(tab, deadline)
            await tab.close()
        tabs = []
    if not tabs:
        tab = await browser.open_tab(reference)
        await tab.bring_to_front()
    deadline = clock() + wait_seconds
    # Google may send the tab to sign-in (possibly after a redirect): show it in a window
    # and wait for the user, then continue in the background.
    signing_in = False
    while True:
        if not _login_tabs(browser, report_id):
            try:
                page = await session.acquire()  # Waits for the draft model itself.
                break
            except StudioError as exc:
                if exc.code != "AUTH_REQUIRED":
                    raise
        if not signing_in:
            signing_in = True
            await _show_for_sign_in(browser, reference)
        if clock() >= deadline:
            tabs = _login_tabs(browser, report_id) or [
                t for t, r in browser.report_tabs() if r == report_id]
            return _login_required(report_id, tabs[0].url if tabs else "",
                                   browser.config.profile_dir)
        await sleep(1)
    if signing_in and not await browser.headless():
        await browser.relaunch(headless=True)
        await browser.open_tab(reference)
        page = await session.acquire()
    ready = await page.evaluate(REPORT_READY_SCRIPT, report_id)
    if not ready:
        raise StudioError("EDITOR_NOT_READY", "Report tab is not ready; inspect it for errors")
    inventory = await bundle.studio.inventory()
    bundle.name = inventory.get("name")
    workspace.active = report_id
    return {
        "status": "ready",
        # The /datastudio-mcp skill states its version; a mismatch means a stale skill.
        "mcp_version": __version__,
        "mode": tab_mode(ready["url"], report_id),
        "tab_url": ready["url"],
        **report_access(ready["editor_report_version"]),
        **inventory,
    }


async def list_reports(workspace: Workspace) -> list[dict[str, Any]]:
    browser = workspace.browser
    if await browser.connect() is None:
        return []
    return [
        {
            "report_id": report_id,
            "mode": tab_mode(page.url, report_id),
            "tab_url": page.url,
            "active": report_id == workspace.active,
            "name": workspace.name(report_id),
        }
        for page, report_id in browser.report_tabs()
    ]
