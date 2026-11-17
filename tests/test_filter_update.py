import asyncio
import copy

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from test_filter_create import FilterSession
from datastudio_mcp.browser import BrowserConfig, ReportSession, StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.filters import FilterConditionChange, Filters
from datastudio_mcp.service import Studio
from datastudio_mcp.server import create_server


def session_with_filters(root, mode="valid"):
    session = FilterSession(root, mode)
    for entry in session.saved["report"]["resource"]["filter"]["entry"]:
        entry["opaque"] = {"keep": True}
        entry["value"].update({"datasourceId": "ds", "version": 1,
                               "filterDefinition": {"legacy": "replace"}})
    session.saved["page"][0]["page"]["filters"] = ["f1", "f2"]
    session.saved["report"]["resource"]["dataViewResource"] = {
        "filterConfig": {"filterIds": ["f1"]}}
    return session


def change(filter_id, condition="GT", number=100):
    return FilterConditionChange(filter_id=filter_id, tree={"kind": "predicate",
        "field": {"name": "revenue"}, "condition": condition, "number_values": [number]})


def test_batch_update_keeps_ids_metadata_and_consumers_and_restores_full_trees(tmp_path):
    async def scenario():
        session = session_with_filters(tmp_path)
        studio = Studio(session)
        before = copy.deepcopy(session.saved)
        token = fingerprint(before["report"])
        changes = [change("f2", "LT", 200), change("f1")]
        preview = await Filters(studio).update_conditions(changes, token)
        assert preview["status"] == "planned" and not session.writes
        assert session.saved == before and not session.config.state_dir.exists()
        result = await Filters(studio).update_conditions(changes, token, dry_run=False)
        assert result["status"] == "verified" and len(session.writes) == 1
        assert [len(item["references"]) for item in result["changes"]] == [1, 2]
        entries = session.saved["report"]["resource"]["filter"]["entry"]
        assert [entry["key"] for entry in entries] == ["f1", "f2"]
        for old, new in zip(before["report"]["resource"]["filter"]["entry"], entries):
            expected = copy.deepcopy(old)
            expected["value"]["filterDefinition"] = new["value"]["filterDefinition"]
            expected["value"]["version"] = 3
            assert new == expected
        assert entries[0]["value"]["filterDefinition"]["filterExpression"]["numberValues"] == [100]
        assert entries[1]["value"]["filterDefinition"]["filterExpression"]["filterConditionType"] == "LT"
        assert session.saved["page"] == before["page"]
        assert session.saved["report"]["resource"]["dataViewResource"] == before["report"]["resource"]["dataViewResource"]
        undo = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert undo["status"] == "verified"
        assert session.saved["report"] == before["report"]
        assert session.saved["page"] == before["page"]
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,code", [("invalid", "INVALID_FILTER"),
                                       ("source-change", "VERSION_CONFLICT")])
def test_validation_failure_has_no_partial_batch_write(tmp_path, mode, code):
    async def scenario():
        session = session_with_filters(tmp_path, mode)
        before = copy.deepcopy(session.saved)
        with pytest.raises(StudioError) as caught:
            await Filters(Studio(session)).update_conditions(
                [change("f1"), change("f2")], fingerprint(before["report"]), dry_run=False)
        assert caught.value.code == code
        assert session.saved == before
        assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("case,code", [("empty", "INVALID_BATCH"), ("duplicate", "INVALID_BATCH"),
    ("missing", "NOT_FOUND"), ("stale", "VERSION_CONFLICT"), ("no-source", "INVALID_FILTER"),
    ("unattached", "OUT_OF_SCOPE")])
def test_invalid_target_or_report_token_never_writes(tmp_path, case, code):
    async def scenario():
        session = session_with_filters(tmp_path)
        changes = [change("f1")]
        if case == "empty":
            changes = []
        if case == "duplicate":
            changes *= 2
        if case == "missing":
            changes = [change("unknown")]
        value = session.saved["report"]["resource"]["filter"]["entry"][0]["value"]
        if case == "no-source":
            del value["datasourceId"]
        if case == "unattached":
            value["datasourceId"] = "other-source"
        token = "stale" if case == "stale" else fingerprint(session.saved["report"])
        with pytest.raises(StudioError) as caught:
            await Filters(Studio(session)).update_conditions(changes, token, dry_run=False)
        assert caught.value.code == code
        assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(scenario())


def test_mcp_update_exposes_typed_batch_and_maps_terminal_failure(tmp_path, monkeypatch):
    async def no_browser(self, page_id=None, *, reload=False):
        return None  # Dispatch is under test; the tab is not.

    monkeypatch.setattr(ReportSession, "acquire", no_browser)

    async def scenario():
        session = session_with_filters(tmp_path)
        browser = BrowserConfig(session.config.profile_dir, session.config.state_dir)
        async with Client(create_server(browser)) as client:
            with pytest.raises(ToolError, match="INVALID_BATCH"):
                await client.call_tool("filters_update_conditions", {
                    "changes": [], "expected_report_content_fingerprint": "unused",
                    "report_id": session.config.report_id})
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["second-filter", "last-source-read"])
def test_late_batch_failure_does_not_save_earlier_validated_filter(tmp_path, failure):
    class LateFailureSession(FilterSession):
        def __init__(self, root):
            super().__init__(root)
            self.validations = 0
            self.source_reads_after_validation = 0

        async def evaluate(self, script, args):
            self.validations += 1
            if failure == "second-filter" and self.validations == 2:
                return {"valid": False, "status": "invalid"}
            return await super().evaluate(script, args)

        async def post(self, endpoint, body):
            if endpoint == "getBlockDatasource" and self.validations == 2:
                self.source_reads_after_validation += 1
                # First read is the second validator's guard; next is the batch's.
                if failure == "last-source-read" and self.source_reads_after_validation == 2:
                    self.source["revision"] += 1
            return await super().post(endpoint, body)

    async def scenario():
        session = LateFailureSession(tmp_path)
        session.saved = session_with_filters(tmp_path).saved
        before = copy.deepcopy(session.saved)
        with pytest.raises(StudioError) as caught:
            await Filters(Studio(session)).update_conditions(
                [change("f1"), change("f2")], fingerprint(before["report"]), dry_run=False)
        assert caught.value.code == ("INVALID_FILTER" if failure == "second-filter" else "VERSION_CONFLICT")
        assert session.validations == 2
        assert session.saved == before
        assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(scenario())
