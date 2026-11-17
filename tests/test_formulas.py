import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.formulas import (
    Formulas,
    FormulaOverride,
    dependency_summary,
    mask_formula_comments,
    reference_identifiers,
    validation_result,
)


@pytest.mark.parametrize("self_reference", [True, False])
def test_proposed_graph_does_not_mutate_source(self_reference):
    async def scenario():
        draft = {
            "datasourceBlock": {
                "fields": [
                    {
                        "field": {"ns": "t0", "name": "calc_a"},
                        "createdBy": 2,
                        "textFormula": "'old'",
                    }
                ]
            }
        }
        expression = "t0.calc_a" if self_reference else "'t0.calc_a'"
        refs = (
            [{"field_id": "t0.calc_a", "kind": "datasource", "position": {"start": 0, "end": 9}}]
            if self_reference
            else []
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(
                side_effect=[
                    {"dataRequest": {}},
                    {"references": refs, "unparsed": []},
                ]
            )
        )
        session = SimpleNamespace(
            config=SimpleNamespace(timeout_ms=1000),
            page=AsyncMock(return_value=page),
            post=AsyncMock(return_value={"column": {"enabled": True, "dataType": 0}}),
        )
        studio = SimpleNamespace(
            session=session,
            datasource=AsyncMock(
                return_value={
                    "draft": draft,
                    "published": draft,
                }
            ),
        )
        result = await Formulas(studio).dependencies(
            "source",
            [
                FormulaOverride(field_id="t0.calc_a", expression=expression),
            ],
        )
        assert result["graph_mode"] == "proposed"
        assert result["cycle"] == (["t0.calc_a", "t0.calc_a"] if self_reference else None)
        assert draft["datasourceBlock"]["fields"][0]["textFormula"] == "'old'"
        assert session.post.call_args.args[0] == "validateFormula"
        assert session.post.await_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case,code",
    [
        ("missing", "FIELD_NOT_FOUND"),
        ("physical", "NOT_CALCULATED_FIELD"),
        ("duplicate", "INVALID_BATCH"),
    ],
)
def test_proposed_graph_rejects_invalid_targets_before_validation(case, code):
    async def scenario():
        draft = {
            "datasourceBlock": {
                "fields": [
                    {
                        "field": {"ns": "t0", "name": "physical"},
                        "createdBy": 1,
                        "textFormula": "t0.raw",
                        "immutableVersion": {"ns": "t0", "name": "raw"},
                    }
                ]
            }
        }
        studio = SimpleNamespace(
            datasource=AsyncMock(
                return_value={
                    "draft": draft,
                    "published": draft,
                }
            )
        )  # No session: invalid targets must fail before any formula API call.
        override = FormulaOverride(
            field_id="t0.missing" if case == "missing" else "t0.physical", expression="1"
        )
        with pytest.raises(StudioError) as error:
            await Formulas(studio).dependencies(
                "source", [override, override] if case == "duplicate" else [override]
            )
        assert error.value.code == code

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("a/* b */+c", "a       +c"),
        ("a-- b\r\n+c#tail", "a    \r\n+c     "),
        ("'/* x */' + \"--y\" + `#z`", "'/* x */' + \"--y\" + `#z`"),
        ("'''a\n/* b */''' #c", "'''a\n/* b */'''   "),
        (r"r'quote\'/*literal*/'/*real*/", r"r'quote\'/*literal*/'" + "        "),
        ("/*😀*/t0.x", "      t0.x"),
    ],
)
def test_comment_mask_preserves_literals_and_utf16(expression, expected):
    actual = mask_formula_comments(expression)
    assert actual == expected
    assert len(actual.encode("utf-16-le")) == len(expression.encode("utf-16-le"))


def test_reference_identity_layers_and_collisions():
    fields = [
        {
            "field": {"ns": "t0", "name": "_amount_"},
            "immutableVersion": {"ns": "t0", "name": "amount"},
        },
        {
            "field": {"ns": "t0", "name": "_amount_copy_"},
            "immutableVersion": {"ns": "t0", "name": "amount"},
        },
    ]
    assert reference_identifiers(fields) == {
        "t0._amount_": "datasource",
        "t0._amount_copy_": "datasource",
        "t0.amount": "connector",
    }
    fields.append({"field": {"ns": "t0", "name": "amount"}})
    with pytest.raises(StudioError, match="overlap"):
        reference_identifiers(fields)


@pytest.mark.parametrize(
    "graph,cycle",
    [
        ({"a": ["a"]}, ["a", "a"]),
        ({"a": ["b"], "b": ["c"], "c": ["a"]}, ["a", "b", "c", "a"]),
        ({"a": ["b", "c"], "b": ["c"], "c": []}, None),
        ({}, None),
    ],
)
def test_dependency_cycle_and_reverse_edges(graph, cycle):
    result = dependency_summary(graph)
    assert result["cycle"] == cycle
    for field, dependencies in graph.items():
        for dependency in dependencies:
            assert field in result["dependents"][dependency]


def test_dependency_unknown_and_deep_chain():
    with pytest.raises(StudioError, match="absent"):
        dependency_summary({"a": ["missing"]})
    graph = {str(index): [str(index + 1)] for index in range(1500)}
    graph["1500"] = []
    assert dependency_summary(graph)["cycle"] is None


def test_dependency_connector_projection_is_a_separate_layer():
    async def scenario():
        draft = {
            "datasourceBlock": {
                "fields": [
                    {
                        "field": {"ns": "t0", "name": "_month_"},
                        "immutableVersion": {"ns": "t0", "name": "month"},
                        "createdBy": 1,
                        "textFormula": "t0.month",
                    }
                ]
            }
        }
        studio = SimpleNamespace(
            datasource=AsyncMock(
                return_value={
                    "draft": draft,
                    "published": draft,
                }
            )
        )
        result = await Formulas(studio).dependencies("source")
        assert result["dependencies"] == {"t0._month_": []}
        assert result["connector_fields"] == {"t0._month_": "t0.month"}
        assert result["deletion_safe"] is False
        assert studio.datasource.await_count == 2

    asyncio.run(scenario())


def test_success_needs_column_metadata_not_http_success():
    column = {"dataType": 2, "enabled": True, "type": 6}
    assert validation_result({"column": column, "errorStatus": {}})["valid"] is True
    for response in ({}, {"column": {}}, {"column": {"dataType": True, "enabled": True}}):
        with pytest.raises(StudioError):
            validation_result(response)


def test_errors_are_preserved_without_guessing_syntax_vs_connector():
    response = {"errorStatus": {"error": 1}, "errorMessage": "GOOGLESQL_PARSE_FAILURE"}
    result = validation_result(response)
    assert result["valid"] is False and result["diagnostics"] == response
    result = validation_result({"connectorErrorStatus": {"error": 2}})
    assert result["status"] == "validation_failed"


def test_source_membership_and_concurrent_change(tmp_path):
    async def scenario():
        page = SimpleNamespace(evaluate=AsyncMock(return_value={"dataRequest": {}}))
        session = SimpleNamespace(
            config=SimpleNamespace(timeout_ms=1000),
            page=AsyncMock(return_value=page),
            post=AsyncMock(return_value={"column": {"dataType": 2, "enabled": True}}),
        )
        studio = SimpleNamespace(
            session=session,
            datasource=AsyncMock(side_effect=StudioError("OUT_OF_SCOPE", "Not attached")),
        )
        validator = Formulas(studio)
        with pytest.raises(StudioError, match="Not attached"):
            await validator.validate("foreign", "1")
        session.post.assert_not_called()
        studio.datasource = AsyncMock(side_effect=[{"draft": {"v": 1}}, {"draft": {"v": 2}}])
        with pytest.raises(StudioError, match="changed"):
            await validator.validate("source", "1")
        session.post.assert_awaited_once_with("validateFormula", {"dataRequest": {}})

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("unparsed", [[], [{"text": "t0.raw", "position": {"start": 0, "end": 6}}]])
def test_references_checks_source_after_native_parse(changed, unparsed):
    async def scenario():
        occurrences = [
            {"field_id": "t0.calc_a", "kind": "datasource", "position": {"start": 0, "end": 9}}
        ]
        page = SimpleNamespace(
            evaluate=AsyncMock(
                side_effect=[
                    {"dataRequest": {}},
                    {"references": occurrences, "unparsed": unparsed},
                ]
            )
        )
        session = SimpleNamespace(
            config=SimpleNamespace(timeout_ms=1000),
            page=AsyncMock(return_value=page),
            post=AsyncMock(return_value={"column": {"dataType": 0, "enabled": True}}),
        )
        source = {
            "draft": {
                "datasourceBlock": {
                    "fields": [
                        {"field": {"ns": "t0", "name": "calc_a"}},
                    ]
                }
            }
        }
        studio = SimpleNamespace(
            session=session,
            datasource=AsyncMock(
                side_effect=[
                    source,
                    source,
                    source,
                    {"draft": {"changed": True}} if changed else source,
                ]
            ),
        )
        if changed:
            with pytest.raises(StudioError, match="changed during parsing"):
                await Formulas(studio).references("source", "t0.calc_a")
        else:
            result = await Formulas(studio).references("source", "t0.calc_a")
            assert result["references"] == (None if unparsed else occurrences)
            assert result["reference_candidates"] == (occurrences if unparsed else [])
            assert result["position_units"] == "UTF-16 code units"
            assert result["unparsed"] == unparsed
            assert result["status"] == ("partial" if unparsed else "valid")
        assert page.evaluate.await_count == 2
        assert session.post.await_count == 1

    asyncio.run(scenario())
