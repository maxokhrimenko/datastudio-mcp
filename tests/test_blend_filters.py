import asyncio
import copy

import pytest

from datastudio_mcp.blend_filters import BlendFilters, BlendInputFilters
from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.report_resources import report_content
from datastudio_mcp.service import Studio
from test_report_filters import BindingSession

PATH = "/blockDatasource/blocks/0/treeQueryBlockConfig/join/right/query"


def fixture(root, mode="valid"):
    session = BindingSession(root, mode)
    session.saved["report"]["resource"]["dataViewResource"] = {"entry": [{"key": "b", "value": {
        "blockDatasource": {"datasourceBlock": {"name": "Keep", "fields": [{"opaque": 1}]},
                            "blocks": [{"treeQueryBlockConfig": {"join": {"type": 3,
                                "left": {"query": {"datasourceId": "other"}},
                                "right": {"query": {"datasourceId": "ds", "concepts": [1],
                                    "filterConfig": {"opaque": [7], "filterIds": ["f1"]}}}}}}]}}}]}
    return session


def spec(ids, path=PATH):
    return BlendInputFilters(blend_id="b", input_path=path, filter_ids=ids)


@pytest.mark.parametrize("ids", [[], ["f2", "f1"]])
def test_only_input_filter_ids_change_and_undo_preserves_all_content(tmp_path, ids):
    async def run():
        session = fixture(tmp_path)
        before = copy.deepcopy(session.saved)
        studio = Studio(session)
        service = BlendFilters(studio)
        token = fingerprint(before["report"])
        assert (await service.patch([spec(ids)], token))["status"] == "planned"
        assert not session.writes and session.saved == before
        result = await service.patch([spec(ids)], token, dry_run=False)
        assert result["status"] == "verified" and len(session.writes) == 1
        expected = report_content(before)
        query = expected["resource"]["dataViewResource"]["entry"][0]["value"]["blockDatasource"]["blocks"][0]["treeQueryBlockConfig"]["join"]["right"]["query"]
        query["filterConfig"]["filterIds"] = ids
        assert report_content(session.saved) == expected
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))["status"] == "verified"
        assert report_content(session.saved) == report_content(before)
    asyncio.run(run())


@pytest.mark.parametrize("mode,code", [("invalid", "INVALID_FILTER"), ("source-change", "VERSION_CONFLICT")])
def test_native_or_source_failure_prevents_journal_and_dispatch(tmp_path, mode, code):
    async def run():
        session = fixture(tmp_path, mode)
        with pytest.raises(StudioError) as caught:
            await BlendFilters(Studio(session)).patch([spec(["f2"])],
                fingerprint(session.saved["report"]), dry_run=False)
        assert caught.value.code == code and not session.writes
        assert not session.config.state_dir.exists()
    asyncio.run(run())


@pytest.mark.parametrize("changes,code", [
    ([spec(["missing"])], "NOT_FOUND"),
    ([spec(["f2"], PATH.replace("right", "left"))], "FILTER_SOURCE_MISMATCH"),
    ([spec([], "/opaque")], "INVALID_BLEND_INPUT"),
    ([spec([]), spec([])], "INVALID_BATCH"),
    ([spec([]), spec([], "/opaque")], "INVALID_BLEND_INPUT"),
])
def test_bad_or_late_invalid_batch_never_writes(tmp_path, changes, code):
    async def run():
        session = fixture(tmp_path)
        before = copy.deepcopy(session.saved)
        with pytest.raises(StudioError) as caught:
            await BlendFilters(Studio(session)).patch(changes, fingerprint(before["report"]), dry_run=False)
        assert caught.value.code == code and not session.writes and session.saved == before
        assert not session.config.state_dir.exists()
    asyncio.run(run())


def test_clear_absent_binding_is_noop_even_for_missing_historical_source(tmp_path):
    async def run():
        session = fixture(tmp_path)
        result = await BlendFilters(Studio(session)).patch([spec([], PATH.replace("right", "left"))],
            fingerprint(session.saved["report"]), dry_run=False)
        assert result["status"] == "unchanged" and not session.writes
    asyncio.run(run())
