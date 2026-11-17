import asyncio
import copy

import pytest

from test_report_resources import Session
from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.filters import Filters
from datastudio_mcp.service import Studio


def test_batch_delete_and_restore_preserve_registry_order_and_other_content(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        studio = Studio(session)
        original = copy.deepcopy(session.saved["report"])
        token = fingerprint(original)
        preview = await Filters(studio).delete(["f2", "f1"], token)
        assert preview["status"] == "planned" and not session.writes
        result = await Filters(studio).delete(["f2", "f1"], token, dry_run=False)
        assert result["status"] == "verified"
        assert session.saved["report"]["resource"]["filter"] == {"future": True, "entry": []}
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))["status"] == "verified"
        assert session.saved["report"] == original
    asyncio.run(scenario())


@pytest.mark.parametrize("location", ["page", "nested", "report", "blend", "opaque"])
def test_any_exact_reference_rejects_entire_delete_batch_before_journal(tmp_path, location):
    async def scenario():
        session = Session(tmp_path)
        report = session.saved["report"]
        if location == "page":
            session.saved["page"][0]["page"]["propertyConfig"] = {"pageProperty": {"filters": ["f1"]}}
        elif location == "nested":
            session.saved["page"][0]["page"]["groupConfig"] = [{"groupId": "g", "componentConfig": [
                {"componentId": "c", "propertyConfig": {"componentProperty": {"filters": ["f1"]}}}]}]
        elif location == "report":
            report["componentConfig"][0]["filters"] = ["f1"]
        elif location == "blend":
            report["resource"]["dataViewResource"] = {"filterConfig": {"filterIds": ["f1"]}}
        else:
            report["opaque"]["future_reference"] = "f1"
        for dry_run in [True, False]:
            with pytest.raises(StudioError) as caught:
                await Filters(Studio(session)).delete(["f2", "f1"], fingerprint(report), dry_run=dry_run)
            assert caught.value.code == "FILTER_IN_USE"
        assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("ids", [[], ["f1", "f1"], ["missing"], [""], ["f1"] * 101])
def test_invalid_delete_batch_does_not_write(tmp_path, ids):
    async def scenario():
        session = Session(tmp_path)
        with pytest.raises(StudioError):
            await Filters(Studio(session)).delete(ids, fingerprint(session.saved["report"]), dry_run=False)
        assert not session.writes
    asyncio.run(scenario())
