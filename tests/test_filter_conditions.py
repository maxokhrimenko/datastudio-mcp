import pytest
from pydantic import TypeAdapter, ValidationError

from datastudio_mcp.browser import StudioError
from datastudio_mcp.filter_conditions import FilterNode, filter_definition, predicates


def parse(value):
    return TypeAdapter(FilterNode).validate_python(value)


def predicate(**extra):
    return {"kind": "predicate", "field": {"name": "revenue"}, "condition": "GT",
            "number_values": [600000], **extra}


def test_and_or_include_and_numeric_values_preserve_structure():
    tree = parse({"kind": "and", "children": [predicate(), {"kind": "or", "children": [
        predicate(include=False, condition="NU", number_values=[]),
        predicate(field={"name": "business"}, condition="IN", number_values=[], string_values=["A", "B"]),
    ]}]})
    result = filter_definition(tree)
    first, group = result["and"]["filterDefinitions"]
    assert first["filterExpression"]["numberValues"] == [600000]
    assert first["filterExpression"]["conceptType"] == 0
    assert group["or"]["filterDefinitions"][0]["filterExpression"]["include"] is False
    assert group["or"]["filterDefinitions"][1]["filterExpression"]["stringValues"] == ["A", "B"]
    assert len({node["filterExpression"]["concept"]["name"] for node in [first, *group["or"]["filterDefinitions"]]}) == 3


def test_calculated_boolean_reuses_validated_enum_conversion():
    tree = parse(predicate(field={"expression": "t0.revenue > 600000"}, condition="T", number_values=[]))
    with pytest.raises(StudioError, match="validation"):
        filter_definition(tree)
    expr = filter_definition(tree, {("root", tree.field.expression): {"dataType": 1}})["filterExpression"]
    assert expr["queryTimeTransformation"]["dataTransformation"]["formulaOutputDataType"] == 3
    assert expr["filterConditionType"] == "T"


def test_metrics_require_explicit_aggregation_and_retained_ids_are_rejected():
    with pytest.raises(StudioError, match="aggregation"):
        filter_definition(parse(predicate(role="metric")))
    expr = filter_definition(parse(predicate(role="metric", field={"name": "revenue", "aggregation": 6})))["filterExpression"]
    assert expr["conceptType"] == 1
    assert expr["queryTimeTransformation"]["dataTransformation"]["aggregation"] == 6
    with pytest.raises(StudioError, match="retain"):
        filter_definition(parse(predicate(field={"name": "x", "concept_name": "qt_old"})))


@pytest.mark.parametrize("payload", [{"kind": "and", "children": []},
    predicate(number_values=[True]), predicate(number_values=[float("inf")]),
    predicate(number_values=[9007199254740993]),
    predicate(include="false"), predicate(condition="arbitrary code"), predicate(unknown=1)])
def test_invalid_tree_input_is_rejected(payload):
    with pytest.raises(ValidationError):
        parse(payload)


def test_complexity_is_bounded_before_formula_work():
    tree = predicate()
    for _ in range(10):
        tree = {"kind": "and", "children": [tree]}
    with pytest.raises(StudioError, match="100 nodes"):
        predicates(parse(tree))
