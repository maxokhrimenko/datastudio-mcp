import copy

import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import overlay_native_delta
from datastudio_mcp.native import merge_duplicate


def test_overlay_preserves_opaque_fields_and_serializer_representation():
    before = {"components": [{"id": "old", "opacity": 1, "defaults": [0]}]}
    after = {"components": [{"id": "new", "opacity": 1, "defaults": [0]}]}
    saved = {
        "components": [{"id": "old", "opacity": "1", "opaque": {"nested": 7}}],
        "future": {"untouched": True},
    }
    original = copy.deepcopy(saved)
    result = overlay_native_delta(before, after, saved)
    assert result == {
        "components": [{"id": "new", "opacity": "1", "opaque": {"nested": 7}}],
        "future": {"untouched": True},
    }
    assert saved == original


def test_changed_unalignable_array_fails_instead_of_losing_saved_elements():
    with pytest.raises(StudioError, match="aligned"):
        overlay_native_delta({"metrics": [1, 2]}, {"metrics": [1, 3]}, {"metrics": [1]})
    assert overlay_native_delta({"a": 1, "b": 2}, {"b": 3}, {"a": 1, "b": 2, "opaque": 4}) == {
        "b": 3,
        "opaque": 4,
    }


def test_duplicate_rebinds_conditions_omitted_by_view_codec():
    original = {
        "pageId": "p_old",
        "page": {"componentConfig": [{"componentId": "c_old"}]},
        "chartCondition": [],
    }
    duplicate = {
        "pageId": "p_new",
        "page": {"componentConfig": [{"componentId": "c_new"}]},
        "chartCondition": [],
    }
    saved = copy.deepcopy(original)
    saved["page"]["componentConfig"][0]["opaque"] = {"data": 1}
    saved["chartCondition"] = [{"chartId": "c_old", "conditionalFormat": {"rule": [{"x": 1}]}}]
    result, omitted = merge_duplicate(original, duplicate, saved)
    assert omitted == []
    assert result["chartCondition"][0]["chartId"] == "c_new"
    assert result["chartCondition"][0]["conditionalFormat"] == {"rule": [{"x": 1}]}
    assert result["page"]["componentConfig"][0] == {"componentId": "c_new", "opaque": {"data": 1}}
    assert saved["chartCondition"][0]["chartId"] == "c_old"
    saved["chartCondition"][0]["chartId"] = "foreign"
    # Live legacy pages retain conditions for charts no longer on the page.
    # Native duplication omits them. Report the omission, never edit the source.
    result, omitted = merge_duplicate(original, duplicate, saved)
    assert result["chartCondition"] == [] and omitted == ["foreign"]
    assert saved["chartCondition"][0]["chartId"] == "foreign"
