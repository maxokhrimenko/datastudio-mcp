"""Journalled checkbox viewer selections; never edits saved report defaults."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from .browser import StudioError
from .bundle import native_script
from .document import json_equal
from .interactions import Interactions, _INSPECT


def native_snapshot(state: dict) -> dict:
    return {key: value for key, value in state.items() if key not in {
        "page_fingerprint", "interaction_fingerprint", "scope"}}


def check_checkbox(state: dict) -> None:
    checkbox = state.get("checkbox", {})
    if checkbox.get("status") != "available":
        raise StudioError("CONTROL_UNAVAILABLE", "A rendered checkbox is required")
    if checkbox.get("is_parameter") or checkbox.get("is_analysis"):
        raise StudioError("CONTROL_UNSUPPORTED", "Parameter/analysis checkbox not characterized")
    if not checkbox.get("enabled"):
        raise StudioError("CONTROL_DISABLED", "Checkbox is disabled")


class Checkbox:
    def __init__(self, studio):
        self.studio = studio
        self.interactions = Interactions(studio)

    async def set(self, page_id: str, component_id: str, value: bool | None,
                  expected: str, *, dry_run: bool = True) -> dict[str, Any]:
        if value is not None and type(value) is not bool:
            raise StudioError("INVALID_VALUE", "Checkbox value must be boolean or null")
        async with self.studio.mutation_lock():
            before = await self.interactions.inspect(page_id, component_id)
            if before["interaction_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Viewer interaction state changed")
            check_checkbox(before)
            return await self._write(before, value, dry_run=dry_run)

    async def restore(self, record: dict, *, dry_run: bool = True) -> dict:
        before = record["before"]
        current = await self.interactions.inspect(before["page_id"], before["component_id"])
        if current["document_id"] != before["document_id"]:
            raise StudioError("RUNTIME_SESSION_EXPIRED", "Original browser document was replaced")
        if json_equal(current, before):
            return {"status": "unchanged", "restored_operation_id": record["operation_id"]}
        if "after" not in record:
            raise StudioError("UNRESOLVED_OPERATION", "Runtime read-back missing; inspect before recovery")
        if not json_equal(current, record["after"]):
            raise StudioError("VERSION_CONFLICT", "Viewer changed after this operation")
        check_checkbox(current)
        result = await self._write(current, before["checkbox"]["value"], dry_run=dry_run,
                                   reset=not before["can_reset"], restore_target=before)
        return {**result, "restored_operation_id": record["operation_id"]}

    async def _write(self, before: dict, value: bool | None, *, dry_run: bool,
                     reset: bool = False, restore_target: dict | None = None) -> dict:
        plan = {"page_id": before["page_id"], "component_id": before["component_id"],
                "value": value, "reset_to_default": reset, "persistence": "browser_document_only"}
        if dry_run:
            return {"status": "planned", **plan}
        if before["checkbox"]["value"] == value and not reset and restore_target is None:
            return {"status": "unchanged", **plan}
        operation_id = str(uuid.uuid4())
        record = {"kind": "checkbox_interaction", "operation_id": operation_id,
                  "report_id": self.studio.config.report_id, "started_at": datetime.now(UTC).isoformat(),
                  "state": "prepared", "before": before, "intended": plan}
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
            response = await asyncio.wait_for(page.evaluate(native_script(_APPLY.replace("__INSPECT__", _INSPECT)), {
                "reportId": self.studio.config.report_id, "pageId": before["page_id"],
                "componentId": before["component_id"], "version": before["editor_page_version"],
                "expected": native_snapshot(before), "value": value, "reset": reset,
            }), timeout=self.studio.config.timeout_ms / 1000)
            if response.get("error"):
                record["dispatch_error"] = response["error"]
        except Exception:
            record["dispatch_error"] = "TRANSPORT_FAILURE"
        try:
            after = await self.interactions.inspect(before["page_id"], before["component_id"])
            record["after"] = after
            definition = (after.get("on_select_value") or {}).get("filterDefinition")
            expected_condition = "T" if value is True else "F"
            selection_matches = (not definition if value is None else
                                 (definition or {}).get("filterExpression", {}).get(
                                     "filterConditionType") == expected_condition)
            valid = (after["document_id"] == before["document_id"]
                     and after["page_fingerprint"] == before["page_fingerprint"]
                     and after.get("checkbox", {}).get("status") == "available"
                     and after["checkbox"]["value"] == value and selection_matches)
            if restore_target is not None:
                valid = valid and json_equal(after, restore_target)
            record["state"] = "verified" if valid else "verification_failed"
        except Exception:
            record["state"] = "outcome_unknown"
        self.studio._journal(operation_id, record)
        return {"status": record["state"], "operation_id": operation_id,
                "dispatch_error": record.get("dispatch_error"), "after": record.get("after"),
                "verification_scope": "Control state and saved page; not consumer query results", **plan}


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
    const checkbox=actual.checkbox;
    if(checkbox.status!=='available'||checkbox.is_parameter||checkbox.is_analysis||!checkbox.enabled)
        return {error:'CONTROL_UNSUPPORTED'};
    const i=window.angular.element(document.body).injector();
    const component=i.get('reportContextService').getReport().getPage(args.pageId).getComponent(args.componentId);
    const controllers=[...document.querySelectorAll('canvas-control-editor')]
        .map(node=>window.angular.element(node).controller('canvasControlEditor'))
        .filter(c=>c?.componentId===args.componentId);
    if(controllers.length!==1||typeof controllers[0].applyCheckboxValue!=='function')
        return {error:'NATIVE_CONTRACT_FAILED'};
    if(args.reset)R.resetControl(i.get('stateActionService'),component);
    else controllers[0].applyCheckboxValue({value:args.value===null?undefined:args.value});
    i.get('$rootScope').$evalAsync();
    return {applied:true};
}"""
