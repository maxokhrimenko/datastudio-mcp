import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import (
    Patch,
    apply_patches,
    assert_subset,
    drop_new_nulls,
    fingerprint,
    overlay_delta,
)


def test_lossless_patch_and_escaped_paths():
    original = {"unknown": {"opaque": [1, None]}, "a/b": {"~key": [10, 20]}}
    changed = apply_patches(
        original,
        [
            Patch(op="test", path="/a~1b/~0key/0", value=10),
            Patch(op="replace", path="/a~1b/~0key/0", value=11),
            Patch(op="add", path="/a~1b/~0key/-", value=30),
            Patch(op="remove", path="/a~1b/~0key/1"),
        ],
    )
    assert changed == {"unknown": {"opaque": [1, None]}, "a/b": {"~key": [11, 30]}}
    assert original["a/b"]["~key"] == [10, 20]


@pytest.mark.parametrize("path", ["/values/-1", "/values/00", "/values/2", "/values/١", "/bad~2"])
def test_bad_pointer_rejected(path):
    with pytest.raises(StudioError):
        apply_patches({"values": [1]}, [Patch(op="replace", path=path, value=3)])


def test_test_operation_is_type_sensitive_and_all_or_nothing():
    original = {"value": True, "other": 3}
    with pytest.raises(StudioError, match="Test failed"):
        apply_patches(
            original,
            [Patch(op="replace", path="/other", value=4), Patch(op="test", path="/value", value=1)],
        )
    assert original == {"value": True, "other": 3}


def test_explicit_null_is_distinct_from_missing_value():
    with pytest.raises(ValueError):
        Patch(op="add", path="/value")
    assert apply_patches({}, [Patch(op="add", path="/value", value=None)]) == {"value": None}


def test_readback_allows_server_defaults_but_detects_changed_values_and_missing_entities():
    assert assert_subset({"color": "red"}, {"color": "red", "opacity": 1}) == []
    assert assert_subset({"fields": ["a", "b"]}, {"fields": ["a"]}) == ["/fields"]
    assert assert_subset({"field": "a"}, {"field": "b"}) == ["/field"]
    # proto default scalars are omitted by the server: absent == 0 / false / ""
    assert assert_subset({"themeRef": {"index": 0}}, {"themeRef": {}}) == []
    assert assert_subset({"themeRef": {"index": 1}}, {"themeRef": {}}) == ["/themeRef/index"]
    assert assert_subset({"show": False, "name": ""}, {}) == []
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_json_number_readback_ignores_integer_vs_decimal_spelling_not_type():
    assert assert_subset({"left": 470.0}, {"left": 470}) == []
    assert assert_subset({"alpha": 1}, {"alpha": 1.0}) == []
    assert assert_subset({"left": 470.5}, {"left": 470}) == ["/left"]
    assert assert_subset({"alpha": True}, {"alpha": 1}) == ["/alpha"]
    assert assert_subset({"alpha": 1.0}, {"alpha": "1"}) == ["/alpha"]


def test_drop_new_nulls_keeps_only_saved_nulls():
    saved = {"kept": None, "nested": [{"a": 1}], "same": {}}
    value = {"kept": None, "new": None, "nested": [{"a": 1, "b": None}, {"c": None, "d": 0}],
             "same": {"x": None}}
    assert drop_new_nulls(value, saved) == {
        "kept": None, "nested": [{"a": 1}, {"d": 0}], "same": {}}


def test_overlay_delta_aligns_identified_lists_and_keeps_opaque_saved_fields():
    saved = [{"componentId": "a", "opaque": 1, "text": "x"}, {"componentId": "b", "opaque": 2}]
    before = [{"componentId": "a", "text": "x"}, {"componentId": "b"}]
    after = [{"componentId": "b"}, {"componentId": "a", "text": "y"}, {"componentId": "new"}]
    assert overlay_delta(before, after, saved) == [
        {"componentId": "b", "opaque": 2},
        {"componentId": "a", "opaque": 1, "text": "y"},
        {"componentId": "new"},
    ]


def test_overlay_delta_removes_deleted_identified_elements():
    saved = [{"componentId": "a", "opaque": 1}, {"componentId": "b"}]
    before = [{"componentId": "a"}, {"componentId": "b"}]
    merged = overlay_delta(before, [{"componentId": "a"}], saved)
    assert merged == [{"componentId": "a", "opaque": 1}]


def test_overlay_delta_refuses_a_loaded_model_that_differs_from_saved():
    saved = [{"componentId": "a"}]
    before = [{"componentId": "a"}, {"componentId": "unsaved-human-edit"}]
    with pytest.raises(StudioError) as error:
        overlay_delta(before, before + [{"componentId": "n"}], saved)
    assert error.value.code == "EDITOR_STALE"


def test_overlay_delta_unchanged_returns_saved_verbatim():
    saved = {"opaque": [1], "a": 1}
    assert overlay_delta({"a": 1}, {"a": 1}, saved) == saved


def test_transparent_color_is_written_without_its_zero_opacity():
    from datastudio_mcp.document import normalize_colors
    value = {"a": {"color": "transparent", "opacity": 0}, "b": [{"color": "#fff", "opacity": 0}]}
    assert normalize_colors(value) == {"a": {"color": "transparent"},
                                               "b": [{"color": "#fff", "opacity": 0}]}


def test_palette_picker_annotations_are_not_written():
    # The server drops them; writing them made read-back fail on every palette color.
    from datastudio_mcp.document import normalize_colors
    value = {"colColor": {"color": "#fd3433", "seriesRef": {"index": 0},
                          "available": True, "disabled": False}, "available": "kept"}
    assert normalize_colors(value) == {"colColor": {"color": "#fd3433", "seriesRef": {"index": 0}},
                                       "available": "kept"}
