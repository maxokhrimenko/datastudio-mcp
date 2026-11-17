import asyncio
import copy

import pytest
from fastmcp import Client

from test_filter_create import FilterSession
from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.report_filters import ReportFilters
from datastudio_mcp.report_resources import ReportResources, report_content
from datastudio_mcp.service import Studio
from datastudio_mcp.server import create_server


class BindingSession(FilterSession):
    def __init__(self, root, mode="valid"):
        super().__init__(root)
        self.binding_mode = mode
        self.saved["report"]["propertyConfig"] = {
            "reportProperty": {"filters": ["f1"], "opaque": {"keep": 7}}
        }
        for entry in self.saved["report"]["resource"]["filter"]["entry"]:
            entry["value"]["datasourceId"] = "ds"

    async def evaluate(self, script, args):
        assert args["pageId"] is None and args["beforeInherit"] is False
        assert args["beforeIds"] == ["f1"]
        if self.binding_mode == "invalid":
            return {"error": "INVALID_FILTER"}
        if self.binding_mode == "source-change":
            self.source["revision"] += 1
        return {"owner_scope": "report", "consumers": []}


def test_operation_inspection_exposes_scope_without_saved_content(tmp_path):
    async def scenario():
        session = BindingSession(tmp_path)
        studio = Studio(session)
        result = await ReportFilters(studio).patch(
            ["f2"], fingerprint(session.saved["report"]), dry_run=False
        )
        async with Client(create_server(session.config)) as client:
            inspected = (await client.call_tool("operation_get", {
                "operation_id": result["operation_id"]
            })).data
        assert inspected["edit_scope"] == "report_filters"
        assert inspected["state"] == "verified"
        assert "before" not in inspected and "intended_report" not in inspected
    asyncio.run(scenario())


@pytest.mark.parametrize("ids", [[], ["f2", "f1"]])
def test_report_binding_preserves_all_other_content_and_restores(tmp_path, ids):
    async def scenario():
        session = BindingSession(tmp_path)
        before = copy.deepcopy(session.saved)
        studio = Studio(session)
        token = fingerprint(before["report"])
        preview = await ReportFilters(studio).patch(ids, token)
        assert preview["status"] == "planned" and session.saved == before
        assert not session.writes and not session.config.state_dir.exists()
        result = await ReportFilters(studio).patch(ids, token, dry_run=False)
        assert result["status"] == "verified" and len(session.writes) == 1
        intended = copy.deepcopy(before["report"])
        intended["propertyConfig"]["reportProperty"]["filters"] = ids
        assert session.saved["report"] == intended
        assert session.saved["page"] == before["page"]
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified"
        assert studio.operation(restored["operation_id"])["edit_scope"] == "report_filters"
        assert session.saved["report"] == before["report"]
        assert session.saved["page"] == before["page"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,code", [("invalid", "INVALID_FILTER"), ("source-change", "VERSION_CONFLICT")]
)
def test_filter_or_source_validation_failure_does_not_write(tmp_path, mode, code):
    async def scenario():
        session = BindingSession(tmp_path, mode)
        with pytest.raises(StudioError) as caught:
            await ReportFilters(Studio(session)).patch(
                ["f2"], fingerprint(session.saved["report"]), dry_run=False
            )
        assert caught.value.code == code and not session.writes
        assert not session.config.state_dir.exists()

    asyncio.run(scenario())


def test_narrow_writer_scope_rejects_extra_property_and_resource_changes(tmp_path):
    async def scenario():
        session = BindingSession(tmp_path)
        before = copy.deepcopy(session.saved)
        for area in ("resource", "propertyConfig", "componentConfig"):
            target = report_content(before)
            target["propertyConfig"]["reportProperty"]["filters"] = ["f2"]
            target[area] = {}
            with pytest.raises(StudioError):
                await ReportResources(Studio(session)).write(
                    before, target, filter_bindings_only=True
                )
        assert not session.writes and not session.config.state_dir.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "ids,code",
    [(["missing"], "NOT_FOUND"), (["f1", "f1"], "INVALID_BATCH"), ([""], "INVALID_BATCH")],
)
def test_invalid_ids_never_journal(tmp_path, ids, code):
    async def scenario():
        session = BindingSession(tmp_path)
        with pytest.raises(StudioError) as caught:
            await ReportFilters(Studio(session)).patch(
                ids, fingerprint(session.saved["report"]), dry_run=False
            )
        assert caught.value.code == code
        assert not session.writes and not session.config.state_dir.exists()

    asyncio.run(scenario())
