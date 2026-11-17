import pytest
from pydantic import ValidationError

from datastudio_mcp.blend_create import NewBlend, blend_value, build_tree
from datastudio_mcp.browser import StudioError


def field(name, *, aggregation=0, semantic=None):
    return {"field": {"name": name}, "defaultAggregationType": aggregation,
            **({"outputSemanticInfo": {"semanticConfig": [{"semanticType": semantic}]}}
               if semantic else {})}


SOURCES = {
    "a": {"datasourceBlock": {"fields": [field("biz", semantic=32), field("count", aggregation=7),
                                         {**field("off"), "enabled": False}]}},
    "b": {"datasourceBlock": {"fields": [field("biz", semantic=32), field("net", aggregation=6)]}},
}


def spec(join_type="INNER_JOIN", keys=None, **overrides):
    keys = [{"left_table": 0, "left_field": "biz", "right_field": "biz"}] if keys is None else keys
    return NewBlend(**{"name": "B", "tables": [
        {"datasource_id": "a", "dimensions": ["biz"], "metrics": [{"name": "count"}]},
        {"datasource_id": "b", "dimensions": ["biz"], "metrics": [{"name": "net"}]}],
        "joins": [{"join_type": join_type, "keys": keys}], **overrides})


def test_tree_matches_the_editor_save_shape():
    tree, names = build_tree(spec(), SOURCES)
    join = tree["join"]
    assert join["type"] == 1 and join["left"]["query"]["datasourceId"] == "a"
    left, right = join["left"]["query"]["concepts"], join["right"]["query"]["concepts"]
    assert left[0]["semantic"] == [32] and left[1]["semantic"] == []
    assert "aggregation" not in left[1]["queryTimeTransformation"]["dataTransformation"]  # AUTO
    assert right[1]["queryTimeTransformation"]["dataTransformation"] == {
        "sourceFieldName": "net", "aggregation": 6}
    assert left[0]["id"]["id"] == "t0." + left[0]["id"]["name"]
    pair = join["condition"]["and"]["conditions"][0]["boolean"]["joinKeyPair"]
    assert pair == {"leftName": names[0]["biz"], "rightName": names[1]["biz"]}
    value = blend_value(spec(), tree)["blockDatasource"]
    assert value["blocks"][0]["type"] == 6 and value["datasourceBlock"]["name"] == "B"


def test_union_and_cross_shapes():
    union, _ = build_tree(spec("UNION"), SOURCES)
    assert set(union["union"]) == {"left", "right", "unionKeyPair"}
    cross, _ = build_tree(spec("CROSS_JOIN", keys=[]), SOURCES)
    assert cross["join"]["type"] == 3 and "condition" not in cross["join"]


def test_unknown_disabled_or_non_dimension_fields_are_refused():
    bad = spec()
    bad.tables[0].dimensions = ["off"]
    with pytest.raises(StudioError) as error:
        build_tree(bad, SOURCES)
    assert error.value.code == "FIELD_NOT_FOUND"
    with pytest.raises(StudioError) as error:
        build_tree(spec(keys=[{"left_table": 0, "left_field": "count", "right_field": "biz"}]),
                   SOURCES)
    assert error.value.code == "INVALID_BLEND_JOIN"


@pytest.mark.parametrize("overrides", [
    {"joins": []},
    {"joins": [{"join_type": "INNER_JOIN", "keys": []}]},
    {"joins": [{"join_type": "CROSS_JOIN",
                "keys": [{"left_table": 0, "left_field": "biz", "right_field": "biz"}]}]},
    {"joins": [{"keys": [{"left_table": 1, "left_field": "biz", "right_field": "biz"}]}]},
])
def test_spec_validation(overrides):
    with pytest.raises(ValidationError):
        spec(**overrides)
