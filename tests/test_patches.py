import asyncio

import pytest
from test_nativeops import OpsSession

from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import Patch, fingerprint
from datastudio_mcp.patches import DimensionColor, Patches
from datastudio_mcp.service import Studio


def patches(session):
    return Patches(Studio(session))


def run(coro):
    return asyncio.run(coro)


def test_report_patch_guards_version_batch_and_identity(tmp_path):
    session = OpsSession(tmp_path)
    content = {"theme": "a", "opaque": [1]}
    edit = [Patch(op="replace", path="/theme", value="b")]
    plan = run(patches(session).report(edit, fingerprint(content)))
    assert plan["status"] == "planned" and plan["after"] == {"theme": "b", "opaque": [1]}
    with pytest.raises(StudioError) as error:
        run(patches(session).report(edit, "stale"))
    assert error.value.code == "VERSION_CONFLICT"
    with pytest.raises(StudioError) as error:
        run(patches(session).report([], fingerprint(content)))
    assert error.value.code == "INVALID_BATCH"
    add = [Patch(op="add", path="/componentConfig", value=[{"componentId": "x"}])]
    with pytest.raises(StudioError) as error:
        run(patches(session).report(add, fingerprint(content)))
    assert error.value.code == "IMMUTABLE_ID"


def colors(*pairs):
    return [DimensionColor(value=v, color=c) for v, c in pairs]


def test_dimension_colors_merge_replace_and_dedupe(tmp_path):
    session = OpsSession(tmp_path)
    existing = {"key": {"value": "keep"}, "color": "#000000"}
    stale = {"key": {"value": "A"}, "color": "#111111"}

    async def plan(values, replace):
        saved = session.saved

        async def post(endpoint, body):
            raw = await type(saved).post(saved, endpoint, body)
            if endpoint == "getReport":
                raw["reportConfig"]["report"]["resource"] = {"colorMappingList": {
                    "colorMappingConfig": [{"colorMapping": [existing, stale],
                                            "colorCollectionIndex": [1]}]}}
            return raw

        session.post = post
        return await patches(session).dimension_colors(values, replace=replace)

    merged = run(plan(colors(("A", "#FF0000")), False))
    assert merged == {"status": "planned", "values_after": 2,
                      "changes": [{"value": "A", "before": "#111111", "after": "#FF0000"}]}
    replaced = run(plan(colors(("A", "#FF0000")), True))
    assert replaced["values_after"] == 1
    assert {"value": "keep", "before": "#000000", "after": None} in replaced["changes"]
    assert run(plan(colors(("A", "#111111")), False))["status"] == "unchanged"


def test_dimension_colors_reject_duplicates_bad_colors_and_over_1500(tmp_path):
    session = OpsSession(tmp_path)
    with pytest.raises(StudioError) as error:
        run(patches(session).dimension_colors(colors(("A", "#FFFFFF"), ("A", "#000000"))))
    assert error.value.code == "INVALID_BATCH"
    with pytest.raises(ValueError):
        DimensionColor(value="A", color="red")
    many = colors(*[(str(i), "#FFFFFF") for i in range(1501)])
    with pytest.raises(StudioError) as error:
        run(patches(session).dimension_colors(many))
    assert error.value.code == "TOO_MANY_VALUES"
