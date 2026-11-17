import asyncio

import pytest

from datastudio_mcp.blends import Blends
from datastudio_mcp.browser import StudioError
from datastudio_mcp.service import Studio
from test_blend_joins import definition
from test_blend_filters import fixture


@pytest.mark.parametrize("change_source", [False, True])
def test_rebuild_checks_source_freshness_and_never_writes(tmp_path, change_source):
    async def run():
        session = fixture(tmp_path)
        session.saved["report"]["resource"]["dataViewResource"]["entry"][0]["value"] = definition()
        async def evaluate(script, args):
            assert args["rebuildOutputSchema"] is True
            if change_source:
                session.source["revision"] += 1
            return {"structurally_valid": True, "rebuilt_output_fields": [], "output_schema_rebuilt": True}
        session.evaluate = evaluate
        if change_source:
            with pytest.raises(StudioError, match="Source changed"):
                await Blends(Studio(session)).validate("b", rebuild_output_schema=True)
        else:
            result = await Blends(Studio(session)).validate("b", rebuild_output_schema=True)
            assert result["output_schema_rebuilt"] is True
        assert not session.writes and not session.config.state_dir.exists()
    asyncio.run(run())


def test_missing_input_source_refuses_rebuild_before_native_evaluation(tmp_path):
    async def run():
        session = fixture(tmp_path)
        raw = definition()
        raw["blockDatasource"]["blocks"][0]["treeQueryBlockConfig"]["join"]["right"]["query"]["datasourceId"] = ""
        session.saved["report"]["resource"]["dataViewResource"]["entry"][0]["value"] = raw
        async def evaluate(script, args):
            raise AssertionError("Native evaluation must not run")
        session.evaluate = evaluate
        with pytest.raises(StudioError, match="Input datasource missing"):
            await Blends(Studio(session)).validate("b", rebuild_output_schema=True)
        assert not session.writes
    asyncio.run(run())
