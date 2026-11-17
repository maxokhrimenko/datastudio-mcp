import asyncio
import copy

import pytest

from datastudio_mcp.blend_joins import BlendJoinChange, BlendJoins, JoinKeyPair, RUNTIME_CODES, patch_join
from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.report_resources import report_content
from datastudio_mcp.service import Studio
from test_blend_filters import fixture

PATH = "/blockDatasource/blocks/0/treeQueryBlockConfig/join"


def change(kind, pairs=None, path=PATH):
    return BlendJoinChange(blend_id="b", join_path=path, join_type=kind, key_pairs=pairs)


def definition():
    return {"opaque": [1], "blockDatasource": {"blocks": [{"treeQueryBlockConfig": {
        "join": {"left": {"query": {"datasourceId": "ds"}},
                 "right": {"query": {"datasourceId": "ds"}}, "type": 3, "future": [2],
                 "condition": {"and": {"conditions": [{"boolean": {"joinKeyPair": {
                     "leftName": "a", "rightName": "b"}}, "pruningMode": 0}]}}}}}],
                 "datasourceBlock": {"name": "Blend", "fields": [{"opaque": 3}]}}}


@pytest.mark.parametrize("kind,wire", [("INNER_JOIN", 1), ("FULL_OUTER_JOIN", 2),
                                     ("LEFT_OUTER_JOIN", 3), ("RIGHT_OUTER_JOIN", 4)])
def test_type_only_changes_preserve_condition_metadata_and_every_other_value(kind, wire):
    before = definition()
    expected = copy.deepcopy(before)
    expected["blockDatasource"]["blocks"][0]["treeQueryBlockConfig"]["join"]["type"] = wire
    assert patch_join(before, change(kind)) == expected
    assert before == definition()


def test_cross_and_union_are_wire_encodings_not_runtime_codes():
    before = definition()
    cross = patch_join(before, change("CROSS_JOIN"))
    node = cross["blockDatasource"]["blocks"][0]["treeQueryBlockConfig"]["join"]
    assert node["type"] == 3 and "condition" not in node and node["future"] == [2]
    with pytest.raises(StudioError):
        patch_join(cross, change("INNER_JOIN"))
    pairs = [JoinKeyPair(left_name="a", right_name="b")]
    union = patch_join(cross, change("UNION", pairs))
    tree = union["blockDatasource"]["blocks"][0]["treeQueryBlockConfig"]
    assert "join" not in tree and "type" not in tree["union"]
    assert tree["union"]["unionKeyPair"] == [{"leftName": "a", "rightName": "b"}]
    restored = patch_join(union, change("LEFT_OUTER_JOIN", path=PATH.replace("/join", "/union")))
    assert restored["opaque"] == before["opaque"]
    assert restored["blockDatasource"]["datasourceBlock"] == before["blockDatasource"]["datasourceBlock"]


def test_native_failure_or_feature_flag_prevents_any_write(tmp_path):
    async def run():
        for mode, kind in [("invalid", "INNER_JOIN"), ("valid", "UNION")]:
            session = fixture(tmp_path)
            session.saved["report"]["resource"]["dataViewResource"]["entry"][0]["value"] = definition()
            async def evaluate(script, args):
                if "candidate" not in args:
                    return {"types": [{"name": k, "code": v, "available": k != "UNION"} for k, v in RUNTIME_CODES.items()]}
                return {"structurally_valid": mode != "invalid", "issues": [73], "normalization_paths": []}
            session.evaluate = evaluate
            with pytest.raises(StudioError):
                await BlendJoins(Studio(session)).patch([change(kind)], fingerprint(session.saved["report"]), dry_run=False)
            assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(run())


def test_real_resource_writer_persists_only_candidate_and_restores(tmp_path):
    async def run():
        session = fixture(tmp_path)
        session.saved["report"]["resource"]["dataViewResource"]["entry"][0]["value"] = definition()
        before = copy.deepcopy(session.saved)
        async def evaluate(script, args):
            if "candidate" not in args:
                return {"types": [{"name": k, "code": v, "available": True} for k, v in RUNTIME_CODES.items()]}
            assert args["definition"] == definition()
            assert args["candidate"] == patch_join(definition(), change("CROSS_JOIN"))
            return {"structurally_valid": True, "issues": [], "normalization_paths": ["/unknown"]}
        session.evaluate = evaluate
        studio = Studio(session)
        result = await BlendJoins(studio).patch([change("CROSS_JOIN")], fingerprint(before["report"]), dry_run=False)
        assert result["status"] == "verified" and len(session.writes) == 1
        expected = report_content(before)
        expected["resource"]["dataViewResource"]["entry"][0]["value"] = patch_join(definition(), change("CROSS_JOIN"))
        assert report_content(session.saved) == expected
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))["status"] == "verified"
        assert report_content(session.saved) == report_content(before)
    asyncio.run(run())
