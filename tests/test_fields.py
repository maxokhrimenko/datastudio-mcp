import pytest
from pydantic import ValidationError

from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import merge_concept_definitions
from datastudio_mcp.fields import ChartField, FieldSelection, selection_changes


def test_source_fields_keep_dimension_and_metric_binding_keys():
    selection = FieldSelection(
        component_id="c",
        dimensions=[ChartField(name="date")],
        metrics=[ChartField(name="revenue", aggregation=6)],
    )
    properties = selection_changes([selection])[0].properties
    assert [p.key for p in properties] == ["actionConceptList", "metrics"]
    assert [
        p.value[0]["queryTimeTransformation"]["dataTransformation"]["sourceFieldName"]
        for p in properties
    ] == ["date", "revenue"]


def test_selection_preserves_omitted_role_and_explicit_empty_list():
    change = selection_changes([FieldSelection(component_id="c", metrics=[])])[0]
    assert len(change.properties) == 1
    assert change.properties[0].key == "metrics" and change.properties[0].value == []
    with pytest.raises(StudioError, match="Provide"):
        selection_changes([FieldSelection(component_id="c")])


def test_definitions_have_unique_ids_and_chart_local_names():
    selection = FieldSelection(
        component_id="c",
        dimensions=[ChartField(name="product")],
        metrics=[ChartField(name="revenue", aggregation=6, display_name="Income")],
    )
    first, second = selection_changes([selection]), selection_changes([selection])
    dimension = first[0].properties[0].value[0]
    metric = first[0].properties[1].value[0]
    assert dimension["name"] != metric["name"] != second[0].properties[1].value[0]["name"]
    assert metric["id"] == "t0." + metric["name"]
    assert metric["queryTimeTransformation"] == {
        "dataTransformation": {"sourceFieldName": "revenue", "aggregation": 6},
        "displayTransformation": {"displayName": "Income"},
    }
    assert "aggregation" not in dimension["queryTimeTransformation"]["dataTransformation"]
    assert selection.metrics[0].name == "revenue"


def test_metric_aggregation_is_never_guessed():
    with pytest.raises(StudioError, match="aggregation"):
        selection_changes([FieldSelection(component_id="c", metrics=[ChartField(name="x")])])


def test_retained_concept_preserves_identity_and_unmentioned_metadata():
    original = {
        "id": "t0.qt_keep",
        "name": "qt_keep",
        "namespace": "t0",
        "opaque": 17,
        "queryTimeTransformation": {
            "dataTransformation": {
                "sourceFieldName": "qt_keep",
                "sourceType": 1,
                "textFormula": "ABS(t0.x)",
                "frontendTextFormula": "ABS(t0.x)",
                "formulaOutputDataType": 2,
                "aggregation": 6,
            },
            "displayTransformation": {"displayName": "Keep label", "future": 42},
        },
    }
    selection = FieldSelection(
        component_id="c",
        metrics=[ChartField(name="other_source_field", concept_name="qt_keep", aggregation=6)],
    )
    with pytest.raises(StudioError, match="belong"):
        selection_changes([selection])
    actual = (
        selection_changes([selection], existing={("c", "qt_keep"): original})[0]
        .properties[0]
        .value[0]
    )
    assert actual["id"] == original["id"] and actual["opaque"] == 17
    assert actual["queryTimeTransformation"]["displayTransformation"] == {
        "displayName": "Keep label",
        "future": 42,
    }
    assert actual["queryTimeTransformation"]["dataTransformation"] == {
        "sourceFieldName": "other_source_field",
        "aggregation": 6,
    }
    assert "textFormula" in original["queryTimeTransformation"]["dataTransformation"]


def test_formula_requires_validation_and_correct_aggregation():
    with pytest.raises(ValidationError):
        ChartField(name="x", expression="1")
    with pytest.raises(ValidationError):
        ChartField()
    field = ChartField(expression="SUM(t0.x)", aggregation=7, display_name="Total")
    selection = FieldSelection(component_id="c", metrics=[field])
    with pytest.raises(StudioError, match="validation"):
        selection_changes([selection])
    validated = {
        ("c", field.expression): {
            "type": 6,
            "dataType": 2,
            "outputSemanticInfo": {"semanticConfig": [{"semanticType": 33}]},
        }
    }
    definition = selection_changes([selection], validated)[0].properties[0].value[0]
    transform = definition["queryTimeTransformation"]
    assert transform["dataTransformation"]["sourceFieldName"] == definition["name"]
    assert transform["dataTransformation"]["textFormula"] == field.expression
    assert transform["dataTransformation"]["formulaOutputDataType"] == 2
    assert transform["displayTransformation"]["displayName"] == "Total"
    assert "outputSemanticInfo" in transform["displayTransformation"]
    field.aggregation = 6
    with pytest.raises(StudioError, match="AUTO"):
        selection_changes([selection], validated)


def test_concept_merge_preserves_opaque_sort_dependency_across_array_changes():
    before = [{"name": "sort", "label": "old"}, {"name": "removed"}]
    after = [{"name": "new1"}, {"name": "sort", "label": "new"}, {"name": "new2"}]
    saved = [{"name": "sort", "label": "old", "opaque": {"future": 1}}, {"name": "removed"}]
    result = merge_concept_definitions(before, after, saved)
    assert result == [
        {"name": "new1"},
        {"name": "sort", "label": "new", "opaque": {"future": 1}},
        {"name": "new2"},
    ]
    assert saved[0]["label"] == "old"
    with pytest.raises(StudioError, match="inventories"):
        merge_concept_definitions(before, after, saved[:1])


@pytest.mark.parametrize("column_type,concept_type", [(0, 0), (1, 3), (2, 2), (3, 1), (4, 1), (8, 6), (9, 7)])
def test_formula_translates_column_enum_to_concept_enum(column_type, concept_type):
    selection = FieldSelection(component_id="c", dimensions=[ChartField(expression="validated")])
    column = {"dataType": column_type}
    definition = selection_changes([selection], {("c", "validated"): column})[0].properties[0].value[0]
    assert definition["queryTimeTransformation"]["dataTransformation"]["formulaOutputDataType"] == concept_type
    assert column == {"dataType": column_type}


@pytest.mark.parametrize("column_type", [None, True, "1", 5, 100, 999])
def test_formula_rejects_unknown_column_type(column_type):
    selection = FieldSelection(component_id="c", dimensions=[ChartField(expression="validated")])
    with pytest.raises(StudioError, match="Unknown validated formula column type"):
        selection_changes([selection], {("c", "validated"): {"dataType": column_type}})
