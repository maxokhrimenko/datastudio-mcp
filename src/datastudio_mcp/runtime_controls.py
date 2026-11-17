"""Journalled runtime values for rendered filter, parameter and date controls."""

import asyncio
import copy
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .browser import StudioError
from .bundle import native_script
from .checkbox import native_snapshot
from .document import json_equal
from .filter_conditions import FilterConditions
from .interactions import _INSPECT, Interactions


class RuntimeFilterValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    condition: str = Field(pattern=r"^[A-Z]+$", max_length=20)
    include: bool = True
    string_values: list[str] = Field(default_factory=list, max_length=1000)
    number_values: list[float] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def check(self) -> "RuntimeFilterValue":
        if self.string_values and self.number_values:
            raise ValueError("Use string_values or number_values, not both")
        if any(
            type(value) is int and abs(value) > 2**53 - 1 for value in self.number_values
        ):
            raise ValueError("Integer values must fit JavaScript's safe integer range")
        return self


class RuntimeParameterValue(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, populate_by_name=True
    )
    label: str | None = None
    string_value: str | None = Field(default=None, alias="stringValue")
    number_value: float | None = Field(default=None, alias="numberValue")
    bool_value: bool | None = Field(default=None, alias="boolValue")

    @model_validator(mode="after")
    def check(self) -> "RuntimeParameterValue":
        supplied = sum(
            name in self.model_fields_set
            for name in ("string_value", "number_value", "bool_value")
        )
        if supplied != 1:
            raise ValueError("Exactly one typed parameter value is required")
        return self


class RuntimeDateRange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_anchor: str = Field(pattern=r"^[0-9]{8}$")
    end_anchor: str = Field(pattern=r"^[0-9]{8}$")

    @model_validator(mode="after")
    def check(self) -> "RuntimeDateRange":
        if self.start_anchor > self.end_anchor:
            raise ValueError("start_anchor must not be after end_anchor")
        return self


def control_definition(state: dict[str, Any], value: RuntimeFilterValue) -> dict[str, Any]:
    control = state.get("control", {})
    if control.get("status") != "available":
        raise StudioError("CONTROL_UNAVAILABLE", "A rendered dimension control is required")
    if control.get("is_parameter") or control.get("is_analysis"):
        raise StudioError("CONTROL_UNSUPPORTED", "Parameter/analysis controls use another action")
    if not control.get("enabled"):
        raise StudioError("CONTROL_DISABLED", "Control is disabled")
    concept = copy.deepcopy(control.get("concept"))
    if not isinstance(concept, dict):
        raise StudioError("NATIVE_CONTRACT_FAILED", "Bound control concept is unavailable")
    name = concept.get("name") or concept.get("field", {}).get("name")
    namespace = concept.get("namespace") or concept.get("field", {}).get("ns")
    transformation = concept.get("queryTimeTransformation")
    if not isinstance(name, str) or not isinstance(namespace, str) or not isinstance(
        transformation, dict
    ):
        raise StudioError("NATIVE_CONTRACT_FAILED", "Bound control concept is malformed")
    expression = {
        "include": value.include,
        "conceptType": 0,
        "concept": {"name": name, "ns": namespace},
        "queryTimeTransformation": transformation,
        "filterConditionType": value.condition,
    }
    if value.string_values:
        expression["stringValues"] = value.string_values
    if value.number_values:
        expression["numberValues"] = value.number_values
    return {"filterExpression": expression}


class RuntimeControls:
    def __init__(self, studio):
        self.studio = studio
        self.interactions = Interactions(studio)

    async def set_filter(
        self,
        page_id: str,
        component_id: str,
        value: RuntimeFilterValue,
        expected: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        async with self.studio.mutation_lock():
            before = await self.interactions.inspect(page_id, component_id)
            if before["interaction_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Viewer interaction state changed")
            definition = control_definition(before, value)
            validation = await FilterConditions(self.studio)._native(
                {
                    "datasourceId": before.get("datasource_id"),
                    "definition": definition,
                    "physicalTypes": {},
                    "catalogOnly": False,
                }
            )
            if not validation.get("valid"):
                return {
                    "status": "validation_failed",
                    "validation": validation,
                    "writes_dispatched": 0,
                }
            return await self._write(
                before, {"filterDefinition": definition}, "filter", dry_run=dry_run
            )

    async def set_parameter(
        self,
        page_id: str,
        component_id: str,
        values: list[RuntimeParameterValue],
        expected: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not 1 <= len(values) <= 1000:
            raise StudioError("INVALID_VALUE", "Provide 1-1000 parameter values")
        async with self.studio.mutation_lock():
            before = await self.interactions.inspect(page_id, component_id)
            if before["interaction_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Viewer interaction state changed")
            parameter = before.get("parameter", {})
            if parameter.get("status") != "available" or not parameter.get("enabled"):
                raise StudioError("CONTROL_UNAVAILABLE", "A bound parameter control is required")
            selection = {
                "connectorConfigParameter": {
                    "parameterOverrides": [
                        {
                            "key": parameter["key"],
                            "value": {
                                "parameterValues": [
                                    value.model_dump(by_alias=True, exclude_none=True)
                                    for value in values
                                ]
                            },
                        }
                    ]
                }
            }
            return await self._write(before, selection, "parameter", dry_run=dry_run)

    async def set_date_range(
        self,
        page_id: str,
        component_id: str,
        value: RuntimeDateRange,
        expected: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        async with self.studio.mutation_lock():
            before = await self.interactions.inspect(page_id, component_id)
            if before["interaction_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Viewer interaction state changed")
            if before.get("date_range", {}).get("status") != "available":
                raise StudioError("CONTROL_UNAVAILABLE", "A date range control is required")
            selection = {
                "dateRange": {
                    "startAnchor": value.start_anchor,
                    "endAnchor": value.end_anchor,
                }
            }
            return await self._write(before, selection, "date_range", dry_run=dry_run)

    async def reset(
        self,
        page_id: str,
        component_id: str,
        expected: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        async with self.studio.mutation_lock():
            before = await self.interactions.inspect(page_id, component_id)
            if before["interaction_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Viewer interaction state changed")
            kind = self._control_kind(before)
            return await self._write(before, None, kind, dry_run=dry_run, reset=True)

    async def restore(self, record: dict, *, dry_run: bool = True) -> dict[str, Any]:
        before = record["before"]
        current = await self.interactions.inspect(before["page_id"], before["component_id"])
        if current["document_id"] != before["document_id"]:
            raise StudioError("RUNTIME_SESSION_EXPIRED", "Original browser document was replaced")
        if json_equal(current, before):
            return {"status": "unchanged", "restored_operation_id": record["operation_id"]}
        if "after" not in record or not json_equal(current, record["after"]):
            raise StudioError("VERSION_CONFLICT", "Viewer changed after this operation")
        kind = record.get("control_kind", "filter")
        target = before.get("on_select_value")
        result = await self._write(
            current,
            target,
            kind,
            dry_run=dry_run,
            reset=not before.get("can_reset", False),
            restore_target=before,
        )
        return {**result, "restored_operation_id": record["operation_id"]}

    async def _write(
        self,
        before: dict[str, Any],
        selection: dict[str, Any] | None,
        kind: str,
        *,
        dry_run: bool,
        reset: bool = False,
        restore_target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = {
            "page_id": before["page_id"],
            "component_id": before["component_id"],
            "control_kind": kind,
            "selection": selection,
            "reset_to_saved_default": reset,
            "persistence": "browser_document_only",
        }
        if dry_run:
            return {"status": "planned", **plan}
        operation_id = str(uuid.uuid4())
        record = {
            "kind": "control_filter_interaction",
            "control_kind": kind,
            "operation_id": operation_id,
            "report_id": self.studio.config.report_id,
            "started_at": datetime.now(UTC).isoformat(),
            "state": "prepared",
            "before": before,
            "intended": plan,
        }
        self.studio._journal(operation_id, record)
        current = await self.interactions.inspect(before["page_id"], before["component_id"])
        if not json_equal(current, before):
            record["state"] = "conflict"
            self.studio._journal(operation_id, record)
            raise StudioError("VERSION_CONFLICT", "Viewer changed before dispatch")
        page = await self.studio.session.page()
        record["state"] = "dispatched"
        self.studio._journal(operation_id, record)
        try:
            response = await asyncio.wait_for(
                page.evaluate(
                    native_script(_APPLY.replace("__INSPECT__", _INSPECT)),
                    {
                        "reportId": self.studio.config.report_id,
                        "pageId": before["page_id"],
                        "componentId": before["component_id"],
                        "version": before["editor_page_version"],
                        "expected": native_snapshot(before),
                        "selection": selection,
                        "kind": kind,
                        "reset": reset,
                    },
                ),
                timeout=self.studio.config.timeout_ms / 1000,
            )
            if response.get("error"):
                record["dispatch_error"] = response["error"]
        except Exception:
            record["dispatch_error"] = "TRANSPORT_FAILURE"
        try:
            after = await self.interactions.inspect(before["page_id"], before["component_id"])
            record["after"] = after
            valid = (
                after["document_id"] == before["document_id"]
                and after["page_fingerprint"] == before["page_fingerprint"]
                and (
                    after.get("can_reset") is False
                    if reset
                    else json_equal(after.get("on_select_value"), selection)
                )
            )
            if restore_target is not None:
                valid = valid and json_equal(after, restore_target)
            record["state"] = "verified" if valid else "verification_failed"
        except Exception:
            record["state"] = "outcome_unknown"
        self.studio._journal(operation_id, record)
        return {
            "status": record["state"],
            "operation_id": operation_id,
            "dispatch_error": record.get("dispatch_error"),
            "after": record.get("after"),
            "verification_scope": "Control state and saved page; not consumer query results",
            **plan,
        }

    @staticmethod
    def _control_kind(state: dict[str, Any]) -> str:
        if state.get("date_range", {}).get("status") == "available":
            return "date_range"
        if state.get("parameter", {}).get("status") == "available":
            return "parameter"
        control_definition(state, RuntimeFilterValue(condition="NU"))
        return "filter"


_APPLY = """args => {
    const inspect=__INSPECT__;
    const viewPath=new RegExp('^(?:/u/[0-9]+)?/reporting/'+args.reportId+'(?:/page/[^/]+)?/?$');
    if(!viewPath.test(location.pathname))return {error:'EDITOR_MODE_UNSAFE'};
    const actual=inspect(args);
    if(actual.error)return actual;
    const canonical=value=>JSON.stringify(value&&typeof value==='object'?
        (Array.isArray(value)?value.map(v=>JSON.parse(canonical(v))):
        Object.fromEntries(Object.keys(value).sort().map(k=>[k,JSON.parse(canonical(value[k]))]))):value);
    if(canonical(actual)!==canonical(args.expected))return {error:'VERSION_CONFLICT'};
    const supported=args.kind==='date_range'?actual.date_range?.status==='available':
        args.kind==='parameter'?actual.parameter?.status==='available'&&actual.parameter.enabled:
        actual.control?.status==='available'&&!actual.control.is_parameter&&
            !actual.control.is_analysis&&actual.control.enabled;
    if(!supported)return {error:'CONTROL_UNSUPPORTED'};
    const i=window.angular.element(document.body).injector();
    const component=i.get('reportContextService').getReport().getPage(args.pageId)
        .getComponent(args.componentId);
    if(args.reset)R.resetControl(i.get('stateActionService'),component);
    else R.applyAction(i.get('stateActionService'),component,'onSelect',args.selection);
    i.get('$rootScope').$evalAsync();
    return {applied:true};
}"""
