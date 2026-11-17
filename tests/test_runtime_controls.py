import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.runtime_controls import (
    RuntimeControls,
    RuntimeDateRange,
    RuntimeFilterValue,
    RuntimeParameterValue,
    control_definition,
)


class Studio:
    def __init__(self, outcome="success"):
        self.config = SimpleNamespace(report_id="r", timeout_ms=1000)
        self.current = {
            "page_id": "p",
            "component_id": "c",
            "datasource_id": "source",
            "document_id": "doc",
            "editor_page_version": "1",
            "page_fingerprint": "saved",
            "interaction_fingerprint": "before",
            "can_reset": False,
            "on_select_value": None,
            "control": {
                "status": "available",
                "enabled": True,
                "is_parameter": False,
                "is_analysis": False,
                "concept": {
                    "name": "qt_control",
                    "namespace": "t0",
                    "queryTimeTransformation": {
                        "dataTransformation": {"sourceFieldName": "_country_"}
                    },
                },
            },
        }
        self.initial = copy.deepcopy(self.current)
        self.records = []
        self.dispatches = 0
        self.outcome = outcome
        self.session = SimpleNamespace(page=self.page)

    @asynccontextmanager
    async def mutation_lock(self):
        yield

    async def page(self):
        return self

    def _journal(self, operation_id, record):
        self.records.append(copy.deepcopy(record))

    async def inspect(self, page_id, component_id):
        return copy.deepcopy(self.current)

    async def evaluate(self, script, args):
        self.dispatches += 1
        if self.outcome != "rejected":
            if args["reset"]:
                self.current = copy.deepcopy(self.initial)
            else:
                self.current["on_select_value"] = copy.deepcopy(args["selection"])
                self.current["can_reset"] = True
                self.current["interaction_fingerprint"] = "after"
        if self.outcome == "timeout":
            raise TimeoutError()
        return {"error": "VERSION_CONFLICT"} if self.outcome == "rejected" else {"applied": True}


def setup(outcome="success"):
    studio = Studio(outcome)
    controls = RuntimeControls(studio)
    controls.interactions = studio
    return studio, controls


async def valid_native(self, arguments):
    return {"valid": True, "status": "valid"}


def test_control_definition_uses_bound_concept_and_rejects_parameter():
    studio, _ = setup()
    definition = control_definition(
        studio.current,
        RuntimeFilterValue(condition="IN", string_values=["UA", "PL"]),
    )
    expression = definition["filterExpression"]
    assert expression["concept"] == {"name": "qt_control", "ns": "t0"}
    assert expression["queryTimeTransformation"]["dataTransformation"][
        "sourceFieldName"
    ] == "_country_"
    assert expression["stringValues"] == ["UA", "PL"]
    studio.current["control"]["is_parameter"] = True
    with pytest.raises(StudioError) as error:
        control_definition(studio.current, RuntimeFilterValue(condition="NU"))
    assert error.value.code == "CONTROL_UNSUPPORTED"


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("success", "verified"),
        ("timeout", "verified"),
        ("rejected", "verification_failed"),
    ],
)
def test_runtime_filter_preview_dispatch_readback_and_restore(monkeypatch, outcome, status):
    async def scenario():
        monkeypatch.setattr(
            "datastudio_mcp.runtime_controls.FilterConditions._native", valid_native
        )
        studio, controls = setup(outcome)
        value = RuntimeFilterValue(condition="IN", string_values=["UA"])
        preview = await controls.set_filter("p", "c", value, "before")
        assert preview["status"] == "planned" and studio.dispatches == 0
        result = await controls.set_filter("p", "c", value, "before", dry_run=False)
        assert result["status"] == status and studio.dispatches == 1
        if status == "verified":
            record = copy.deepcopy(studio.records[-1])
            restored = await controls.restore(record, dry_run=False)
            assert restored["status"] == "verified"
            assert studio.current == studio.initial and studio.dispatches == 2

    asyncio.run(scenario())


def test_runtime_filter_conflict_and_invalid_native_never_dispatch(monkeypatch):
    async def invalid(self, arguments):
        return {"valid": False, "status": "invalid"}

    async def scenario():
        monkeypatch.setattr(
            "datastudio_mcp.runtime_controls.FilterConditions._native", invalid
        )
        studio, controls = setup()
        value = RuntimeFilterValue(condition="IN", string_values=["UA"])
        result = await controls.set_filter("p", "c", value, "before", dry_run=False)
        assert result["status"] == "validation_failed" and studio.dispatches == 0
        with pytest.raises(StudioError) as error:
            await controls.set_filter("p", "c", value, "wrong", dry_run=False)
        assert error.value.code == "VERSION_CONFLICT"

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["parameter", "date_range"])
def test_parameter_and_date_runtime_actions_restore(kind):
    async def scenario():
        studio, controls = setup()
        studio.current["control"] = {"status": "not_applicable"}
        if kind == "parameter":
            studio.current["parameter"] = {
                "status": "available",
                "enabled": True,
                "key": "country_param",
            }
            studio.initial = copy.deepcopy(studio.current)
            result = await controls.set_parameter(
                "p",
                "c",
                [RuntimeParameterValue(stringValue="UA", label="Ukraine")],
                "before",
                dry_run=False,
            )
            assert result["selection"]["connectorConfigParameter"]["parameterOverrides"] == [
                {
                    "key": "country_param",
                    "value": {
                        "parameterValues": [{"label": "Ukraine", "stringValue": "UA"}]
                    },
                }
            ]
        else:
            studio.current["date_range"] = {"status": "available"}
            studio.initial = copy.deepcopy(studio.current)
            result = await controls.set_date_range(
                "p",
                "c",
                RuntimeDateRange(start_anchor="20260101", end_anchor="20260131"),
                "before",
                dry_run=False,
            )
            assert result["selection"] == {
                "dateRange": {"startAnchor": "20260101", "endAnchor": "20260131"}
            }
        assert result["status"] == "verified"
        restored = await controls.restore(copy.deepcopy(studio.records[-1]), dry_run=False)
        assert restored["status"] == "verified" and studio.current == studio.initial

    asyncio.run(scenario())


def test_runtime_parameter_value_requires_one_typed_value():
    with pytest.raises(ValueError):
        RuntimeParameterValue(label="missing")
    with pytest.raises(ValueError):
        RuntimeParameterValue(stringValue="x", boolValue=True)
