import asyncio
import copy

import pytest

from datastudio_mcp.blends import BlendRename, Blends
from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.report_resources import report_content
from datastudio_mcp.service import Studio
from test_report_resources import Session


def session_with_blends(root, mode="success"):
    session = Session(root, mode)
    session.saved["report"]["resource"]["dataViewResource"] = {"future": True, "entry": [
        {"key": key, "entry_metadata": [1], "value": {"opaque": [2], "blockDatasource": {
            "datasourceBlock": {"name": "Same", "fields": [{"opaque": 3}]},
            "blocks": [{"treeQueryBlockConfig": {"query": {"datasourceId": "", "future": 4}}}],
        }}} for key in ("b1", "b2")]}
    return session


def test_batch_rename_changes_only_names_and_undo_is_exact(tmp_path):
    async def scenario():
        session = session_with_blends(tmp_path)
        before = copy.deepcopy(session.saved)
        studio = Studio(session)
        changes = [BlendRename(blend_id="b2", name="Second"), BlendRename(blend_id="b1", name="First")]
        result = await Blends(studio).rename(changes, fingerprint(before["report"]), dry_run=False)
        assert result["status"] == "verified" and len(session.writes) == 1
        expected = report_content(before)
        for entry, name in zip(expected["resource"]["dataViewResource"]["entry"], ["First", "Second"]):
            entry["value"]["blockDatasource"]["datasourceBlock"]["name"] = name
        assert report_content(session.saved) == expected
        assert session.saved["page"] == before["page"]
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))["status"] == "verified"
        assert report_content(session.saved) == report_content(before)
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,status", [("timeout", "verified"), ("corrupt", "verification_failed")])
def test_readback_resolves_timeout_without_retry_and_detects_collateral_changes(tmp_path, mode, status):
    async def scenario():
        session = session_with_blends(tmp_path, mode)
        result = await Blends(Studio(session)).rename([BlendRename(blend_id="b1", name="New")],
            fingerprint(session.saved["report"]), dry_run=False)
        assert result["status"] == status and len(session.writes) == 1
    asyncio.run(scenario())


def test_preview_noop_and_invalid_batch_do_not_write(tmp_path):
    async def scenario():
        session = session_with_blends(tmp_path)
        service = Blends(Studio(session))
        token = fingerprint(session.saved["report"])
        assert (await service.rename([BlendRename(blend_id="b1", name="New")], token))["status"] == "planned"
        assert (await service.rename([BlendRename(blend_id="b1", name="Same")], token,
                                     dry_run=False))["status"] == "unchanged"
        for changes, expected in [([], token), ([BlendRename(blend_id="missing", name="New")], token),
            ([BlendRename(blend_id="b1", name="A"), BlendRename(blend_id="b1", name="B")], token),
            ([BlendRename(blend_id="b1", name="New")], "stale")]:
            with pytest.raises(StudioError):
                await service.rename(changes, expected, dry_run=False)
        assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(scenario())


def test_late_invalid_output_block_rejects_whole_batch_before_journal(tmp_path):
    async def scenario():
        session = session_with_blends(tmp_path)
        session.saved["report"]["resource"]["dataViewResource"]["entry"][1]["value"] = {}
        before = copy.deepcopy(session.saved)
        with pytest.raises(StudioError, match="output datasource block"):
            await Blends(Studio(session)).rename([BlendRename(blend_id=k, name="New") for k in ("b1", "b2")],
                fingerprint(before["report"]), dry_run=False)
        assert session.saved == before and not session.writes
        assert not session.config.state_dir.exists()
    asyncio.run(scenario())


def test_dispatch_rechecks_version(tmp_path):
    async def scenario():
        session = session_with_blends(tmp_path, "conflict")
        with pytest.raises(StudioError, match="before resource dispatch"):
            await Blends(Studio(session)).rename([BlendRename(blend_id="b1", name="New")],
                fingerprint(session.saved["report"]), dry_run=False)
        assert not session.writes
    asyncio.run(scenario())
