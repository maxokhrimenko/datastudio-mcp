import asyncio
import copy

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from test_report_resources import Session
from datastudio_mcp.browser import BrowserConfig, ReportSession, StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.filters import Filters, NewFilter
from datastudio_mcp.service import Studio
from datastudio_mcp.server import create_server


class FilterSession(Session):
    def __init__(self, root, validation_mode="valid"):
        super().__init__(root)
        self.validation_mode = validation_mode
        self.source = {"datasourceId": "ds", "revision": 1, "datasourceBlock": {"fields": [
            {"field": {"ns": "t0", "name": "revenue"}, "dataType": 2, "enabled": True}]}}

    async def page(self):
        return self

    async def evaluate(self, script, args):
        assert args["physicalTypes"] == {"revenue": 2}
        if self.validation_mode == "source-change":
            self.source["revision"] += 1
        return {"valid": self.validation_mode != "invalid", "status": self.validation_mode}

    async def post(self, endpoint, body):
        if endpoint == "getClaimedDatasources":
            return {"claimedDatasources": [{"basicEntityInfo": {"id": "ds"}}]}
        if endpoint == "getBlockDatasource":
            return {"draft": copy.deepcopy(self.source), "published": copy.deepcopy(self.source)}
        return await super().post(endpoint, body)


def spec(name):
    return NewFilter(name=name, datasource_id="ds", tree={"kind": "predicate",
        "field": {"name": "revenue"}, "condition": "GT", "number_values": [100]})


def test_validated_batch_creation_preserves_registry_and_undo(tmp_path):
    async def scenario():
        session = FilterSession(tmp_path)
        studio = Studio(session)
        original = copy.deepcopy(session.saved["report"])
        token = fingerprint(original)
        specs = [spec("A"), spec("B")]
        preview = await Filters(studio).create(specs, token)
        assert preview["status"] == "planned" and not session.writes
        result = await Filters(studio).create(specs, token, dry_run=False)
        assert result["status"] == "verified"
        assert len(set(result["created_filter_ids"])) == 2
        assert set(result["created_filter_ids"]).isdisjoint(preview["created_filter_ids"])
        entries = session.saved["report"]["resource"]["filter"]["entry"]
        assert entries[:2] == original["resource"]["filter"]["entry"]
        assert [e["value"]["name"] for e in entries[2:]] == ["A", "B"]
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))["status"] == "verified"
        assert session.saved["report"] == original
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,code", [("invalid", "INVALID_FILTER"), ("source-change", "VERSION_CONFLICT")])
def test_invalid_filter_or_concurrent_source_change_never_journals_or_writes(tmp_path, mode, code):
    async def scenario():
        session = FilterSession(tmp_path, mode)
        with pytest.raises(StudioError) as caught:
            await Filters(Studio(session)).create([spec("A")], fingerprint(session.saved["report"]), dry_run=False)
        assert caught.value.code == code
        assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(scenario())


def test_mcp_create_dispatch_does_not_shadow_filter_service_with_input_list(tmp_path, monkeypatch):
    async def no_browser(self, page_id=None, *, reload=False):
        return None  # Dispatch is under test; the tab is not.

    monkeypatch.setattr(ReportSession, "acquire", no_browser)

    async def scenario():
        session = FilterSession(tmp_path)
        browser = BrowserConfig(session.config.profile_dir, session.config.state_dir)
        async with Client(create_server(browser)) as client:
            with pytest.raises(ToolError, match="INVALID_BATCH"):
                await client.call_tool("filters_create", {"filters": [],
                    "expected_report_content_fingerprint": "unused",
                    "report_id": session.config.report_id})
    asyncio.run(scenario())
