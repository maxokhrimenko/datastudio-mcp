import asyncio
import copy

import pytest
from pydantic import ValidationError

from datastudio_mcp.blends import BlendInputRename, Blends
from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.report_resources import report_content
from datastudio_mcp.service import Studio
from test_blend_rename import session_with_blends

PATH = "/blockDatasource/blocks/0/treeQueryBlockConfig/query"


def spec(key="b1", name="New", path=PATH):
    return BlendInputRename(blend_id=key, input_path=path, name=name)


def test_utf16_limit_and_empty_reset():
    assert spec(name="").name == ""
    assert spec(name="😀" * 50).name == "😀" * 50
    for name in ["a" * 101, "😀" * 51, "\ud800"]:
        with pytest.raises(ValidationError):
            spec(name=name)


def test_batch_changes_only_input_names_and_clear_undo_are_lossless(tmp_path):
    async def run():
        session = session_with_blends(tmp_path)
        before = copy.deepcopy(session.saved)
        studio = Studio(session)
        service = Blends(studio)
        token = fingerprint(before["report"])
        changes = [spec("b1", "Same"), spec("b2", "Same")]
        assert (await service.rename_inputs(changes, token))["status"] == "planned"
        assert session.saved == before and not session.writes
        write = await service.rename_inputs(changes, token, dry_run=False)
        assert write["status"] == "verified" and len(session.writes) == 1
        expected = report_content(before)
        for entry in expected["resource"]["dataViewResource"]["entry"]:
            entry["value"]["blockDatasource"]["blocks"][0]["treeQueryBlockConfig"]["query"]["displayName"] = "Same"
        assert report_content(session.saved) == expected
        clear = await service.rename_inputs([spec("b1", ""), spec("b2", "")],
            fingerprint(session.saved["report"]), dry_run=False)
        assert clear["status"] == "verified" and report_content(session.saved) == report_content(before)
        assert (await studio.restore_operation(clear["operation_id"], dry_run=False))["status"] == "verified"
        assert report_content(session.saved) == expected
        assert (await studio.restore_operation(write["operation_id"], dry_run=False))["status"] == "verified"
        assert report_content(session.saved) == report_content(before)
    asyncio.run(run())


@pytest.mark.parametrize("changes", [[], [spec(), spec()], [spec("missing")],
                                     [spec(), spec("b2", path="/opaque")]])
def test_invalid_or_late_bad_target_never_journals_or_writes(tmp_path, changes):
    async def run():
        session = session_with_blends(tmp_path)
        before = copy.deepcopy(session.saved)
        with pytest.raises(StudioError):
            await Blends(Studio(session)).rename_inputs(changes, fingerprint(before["report"]), dry_run=False)
        assert session.saved == before and not session.writes
        assert not session.config.state_dir.exists()
    asyncio.run(run())


def test_stale_token_and_missing_label_noop(tmp_path):
    async def run():
        session = session_with_blends(tmp_path)
        service = Blends(Studio(session))
        with pytest.raises(StudioError):
            await service.rename_inputs([spec()], "stale", dry_run=False)
        assert (await service.rename_inputs([spec(name="")], fingerprint(session.saved["report"]),
                                           dry_run=False))["status"] == "unchanged"
        assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(run())
