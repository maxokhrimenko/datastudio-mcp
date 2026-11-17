import asyncio
import json
import uuid

import pytest
from fastmcp.exceptions import ToolError

from datastudio_mcp.browser import BrowserConfig, ReportSession, StudioError
from datastudio_mcp.server import create_server, journal_record
from datastudio_mcp.service import Studio

REPORT = "0f0f0f0f-1a1a-4b2b-8c3c-4d4d4d4d4d4d"
REPORT_FREE = {"report_open", "reports_list", "operation_get", "operation_restore"}
NEW_TOOLS = {
    "settings_describe", "settings_set", "bigquery_browse", "datasource_create_bigquery",
    "datasource_add_to_report", "datasources_remove_from_report", "report_publishing",
    "report_publish", "report_publishing_mode", "revisions_list", "revision_name_current",
    "revision_restore", "report_content_get", "report_patch",
    "dimension_value_colors_set", "report_rename", "page_screenshot", "native_catalog",
}


def test_every_report_tool_accepts_optional_report_id(tmp_path):
    server = create_server(BrowserConfig(tmp_path, tmp_path / "journal"))
    listed = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert REPORT_FREE <= set(listed)
    assert NEW_TOOLS <= set(listed)
    assert not {"component_types", "component_describe", "components_style"} & set(listed)
    for name, tool in listed.items():
        properties = tool.parameters["properties"]
        if name in REPORT_FREE:
            assert "report_id" not in properties, name
        else:
            assert "report_id" in properties, name
            assert "report_id" not in tool.parameters.get("required", []), name


def test_tools_without_active_report_fail_closed(tmp_path):
    server = create_server(BrowserConfig(tmp_path, tmp_path / "journal"))

    async def scenario():
        with pytest.raises(ToolError, match="NO_ACTIVE_REPORT"):
            await server.call_tool("report_inspect", {})
        with pytest.raises(ToolError, match="NO_ACTIVE_REPORT"):
            await server.call_tool("page_get", {"page_id": "p"})
        with pytest.raises(ToolError, match="INVALID_REPORT_REFERENCE"):
            await server.call_tool("report_inspect", {"report_id": "nope"})

    asyncio.run(scenario())


def test_journal_record_reads_any_report_and_rejects_bad_ids(tmp_path):
    state = tmp_path / "journal"
    state.mkdir()
    operation_id = str(uuid.uuid4())
    (state / f"{operation_id}.json").write_text(
        json.dumps({"operation_id": operation_id, "report_id": REPORT, "state": "verified"})
    )
    assert journal_record(state, operation_id)["report_id"] == REPORT
    for bad in ("nope", str(uuid.uuid4()), operation_id.upper()):
        with pytest.raises(ToolError, match="NOT_FOUND"):
            journal_record(state, bad)
    orphan = str(uuid.uuid4())
    (state / f"{orphan}.json").write_text(json.dumps({"operation_id": orphan}))
    with pytest.raises(ToolError, match="INVALID_JOURNAL"):
        journal_record(state, orphan)


def test_operation_get_needs_neither_browser_nor_active_report(tmp_path):
    state = tmp_path / "journal"
    state.mkdir()
    operation_id = str(uuid.uuid4())
    (state / f"{operation_id}.json").write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "report_id": REPORT,
                "state": "verified",
                "content": {"secret": 1},
            }
        )
    )
    server = create_server(BrowserConfig(tmp_path, state))

    async def scenario():
        result = await server.call_tool("operation_get", {"operation_id": operation_id})
        assert result.structured_content["report_id"] == REPORT
        assert "content" not in result.structured_content

    asyncio.run(scenario())


def _inspect_with(monkeypatch, tmp_path, outcomes):
    """Drive report_inspect with scripted inventory outcomes; returns the acquire log."""
    acquires, queue = [], list(outcomes)

    async def acquire(self, page_id=None, *, reload=False):
        acquires.append(reload)

    async def inventory(self):
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(ReportSession, "acquire", acquire)
    monkeypatch.setattr(Studio, "inventory", inventory)
    server = create_server(BrowserConfig(tmp_path, tmp_path / "journal"))
    return server, acquires


def test_report_tool_acquires_view_first_and_retries_once_on_stale_model(tmp_path, monkeypatch):
    server, acquires = _inspect_with(
        monkeypatch, tmp_path, [StudioError("EDITOR_STALE", "old model"), {"ok": 1}]
    )
    result = asyncio.run(server.call_tool("report_inspect", {"report_id": REPORT}))
    assert result.structured_content == {"ok": 1}
    assert acquires == [False, True]


def test_report_tool_does_not_retry_persistent_stale_or_unknown_outcomes(tmp_path, monkeypatch):
    stale = StudioError("EDITOR_STALE", "old model")
    server, acquires = _inspect_with(
        monkeypatch, tmp_path, [stale, StudioError("EDITOR_STALE", "x")])
    with pytest.raises(ToolError, match="EDITOR_STALE"):
        asyncio.run(server.call_tool("report_inspect", {"report_id": REPORT}))
    assert acquires == [False, True]

    unknown = StudioError("EDITOR_STALE", "sent", outcome_unknown=True)
    server, acquires = _inspect_with(monkeypatch, tmp_path, [unknown])
    with pytest.raises(ToolError, match="outcome_unknown=True"):
        asyncio.run(server.call_tool("report_inspect", {"report_id": REPORT}))
    assert acquires == [False]
