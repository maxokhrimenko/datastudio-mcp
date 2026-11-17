"""Type-switch merge and explicit change inventory for native conversion drafts."""

from __future__ import annotations

import asyncio
import copy
import re
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .browser import StudioError
from .bundle import native_script
from .components import select_top_level
from .document import (
    find_page,
    fingerprint,
    json_equal,
    merge_concept_definitions,
    overlay_native_delta,
)
from .fields import ChartField, FieldSelection, selection_changes
from .formulas import Formulas
from .native import Native

if TYPE_CHECKING:
    from .service import Studio


class TypeChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    component_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    preset: str = "default"
    dimensions: list[ChartField] | None = Field(default=None, max_length=100)
    metrics: list[ChartField] | None = Field(default=None, max_length=100)


def conversion_requests(
    changes: list[TypeChange],
    validated: dict[tuple[str, str], dict] | None = None,
    existing: dict[tuple[str, str], dict] | None = None,
) -> list[dict[str, Any]]:
    """Optional target bindings, reusing validated chart-local field contracts."""
    requests = []
    for change in changes:
        request = change.model_dump(exclude={"dimensions", "metrics"})
        request["properties"] = []
        if change.dimensions is not None or change.metrics is not None:
            selection = FieldSelection(
                component_id=change.component_id,
                dimensions=change.dimensions,
                metrics=change.metrics,
            )
            request["properties"] = [
                p.model_dump()
                for p in selection_changes([selection], validated, existing)[0].properties
            ]
        requests.append(request)
    return requests


class Switching:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def patch(
        self,
        page_id: str,
        changes: list[TypeChange],
        expected: str,
        *,
        dry_run: bool = True,
        allow_loss: bool = False,
    ) -> dict[str, Any]:
        ids = [c.component_id for c in changes]
        if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
            raise StudioError("INVALID_BATCH", "Switch 1–100 distinct components")
        # Validate explicit metric aggregation before accessing the browser.
        for change in changes:
            if any(field.aggregation is None for field in change.metrics or []):
                raise StudioError("MISSING_AGGREGATION", "Each metric needs explicit aggregation")
        async with self.studio.mutation_lock():
            frame = find_page(await self.studio.report(), page_id)
            if fingerprint(frame["page"]) != expected:
                raise StudioError("VERSION_CONFLICT", "Page changed before conversion")
            selected = select_top_level(frame["page"], ids)
            if any(c.get("componentConfig") for c in selected):
                raise StudioError(
                    "COMPOSITE_OPERATION_REQUIRED", "Composite conversion needs child remapping"
                )
            if any(c.get("chartId") in ids for c in frame.get("chartCondition", [])):
                raise StudioError(
                    "CONDITIONAL_FORMATTING_REQUIRES_REMAP",
                    "Inspect conditional formatting before conversion",
                )
            sources = {}
            for source_id in {c.get("datasourceId") for c in selected}:
                if not source_id:
                    raise StudioError(
                        "MISSING_DATASOURCE", "This conversion path requires bound chart sources"
                    )
                source = await self.studio.datasource(source_id)
                if not json_equal(source["draft"], source["published"]):
                    raise StudioError(
                        "UNPUBLISHED_DATASOURCE",
                        "Native conversion requires matching draft/published schema",
                    )
                sources[source_id] = fingerprint(source)
            validated, existing = {}, {}
            for change, saved in zip(changes, selected, strict=True):
                fields = (change.dimensions or []) + (change.metrics or [])
                retained = [field.concept_name for field in fields if field.concept_name]
                if len(set(retained)) != len(retained):
                    raise StudioError(
                        "DUPLICATE_CONCEPT_EDIT", "Edit each retained concept once per batch"
                    )
                for definition in saved.get("conceptDefs", []):
                    existing[(change.component_id, definition["name"])] = definition
                for field in fields:
                    if (
                        field.expression is None
                        or (change.component_id, field.expression) in validated
                    ):
                        continue
                    result = await Formulas(self.studio).validate(
                        saved["datasourceId"], field.expression
                    )
                    if not result["valid"]:
                        raise StudioError("FORMULA_VALIDATION_FAILED", str(result["diagnostics"]))
                    validated[(change.component_id, field.expression)] = result["column"]
            requests = conversion_requests(changes, validated, existing)
            # Do not prepare against a source revision newer than the one inspected.
            for source_id, token in sources.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError(
                        "VERSION_CONFLICT", "Datasource changed during formula validation"
                    )
            edits = await self._prepare(frame, changes, requests=requests)
            if len(edits) != len(changes):
                raise StudioError("NATIVE_CONTRACT_FAILED", "Conversion batch size differs")
            after = copy.deepcopy(frame["page"])
            targets = {c["componentId"]: c for c in after["componentConfig"]}
            previews = []
            for spec, saved, edit in zip(changes, selected, edits, strict=True):
                if (
                    edit["after"].get("type") != spec.type
                    or edit["after"].get("preset") != spec.preset
                ):
                    raise StudioError(
                        "NATIVE_CONTRACT_FAILED", "Converter returned another target type/preset"
                    )
                merged, preview = merge_type_switch(edit["before"], edit["after"], saved)
                targets[spec.component_id].clear()
                targets[spec.component_id].update(merged)
                previews.append({"component_id": spec.component_id, **preview})
            for source_id, token in sources.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError("VERSION_CONFLICT", "Datasource changed during conversion")
            plan = {"page_id": page_id, "conversions": previews, "atomic_scope": "page"}
            if json_equal(frame["page"], after):
                return {"status": "unchanged", **plan}
            # Type-specific removals are normal but must be an explicit caller decision.
            loss = any(requires_loss_approval(p) for p in previews)
            if dry_run:
                return {
                    "status": "planned",
                    "requires_allow_loss": loss,
                    "updated_components": [targets[i] for i in ids],
                    **plan,
                }
            if loss and not allow_loss:
                raise StudioError(
                    "LOSS_APPROVAL_REQUIRED",
                    "Inspect preview and explicitly allow lost/changed settings",
                )
            return {**await self.studio._write_page(frame, after), **plan}

    async def _prepare(
        self,
        frame: dict[str, Any],
        changes: list[TypeChange],
        *,
        requests: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        session = self.studio.session
        await Native(session).require_static_page(frame)
        page = await session.page()
        client = await page.context.new_cdp_session(page)
        object_group = "datastudio-switch-" + uuid.uuid4().hex
        failed = True
        try:
            async with asyncio.timeout(session.config.timeout_ms / 1000):
                fn = await client.send(
                    "Runtime.evaluate",
                    {
                        "expression": "window.angular.element(document.body).injector().get('vizSwitchService').switchTo",
                        "objectGroup": object_group,
                    },
                )
                fn_id = fn.get("result", {}).get("objectId")
                if not fn_id or fn.get("exceptionDetails"):
                    raise StudioError("NATIVE_CONTRACT_FAILED", "Switch service unavailable")
                source = await client.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": fn_id,
                        "functionDeclaration": "function(){return String(this)}",
                        "returnByValue": True,
                    },
                )
                match = re.search(r"(\w+)\(this,a,b,c\)", source.get("result", {}).get("value", ""))
                if not match:
                    raise StudioError("NATIVE_CONTRACT_FAILED", "Converter call-site changed")
                props = await client.send(
                    "Runtime.getProperties", {"objectId": fn_id, "ownProperties": True}
                )
                scopes = next(
                    (
                        p["value"]["objectId"]
                        for p in props.get("internalProperties", [])
                        if p["name"] == "[[Scopes]]"
                    ),
                    None,
                )
                if not scopes:
                    raise StudioError("NATIVE_CONTRACT_FAILED", "Converter scope unavailable")
                scope_list = await client.send(
                    "Runtime.getProperties", {"objectId": scopes, "ownProperties": True}
                )
                found = []
                for scope in scope_list["result"]:
                    scope_id = scope.get("value", {}).get("objectId")
                    if not scope_id:
                        continue
                    contents = await client.send(
                        "Runtime.getProperties", {"objectId": scope_id, "ownProperties": True}
                    )
                    found.extend(
                        p["value"]["objectId"]
                        for p in contents["result"]
                        if p["name"] == match[1] and p.get("value", {}).get("type") == "function"
                    )
                if len(found) != 1:
                    raise StudioError("NATIVE_CONTRACT_FAILED", "Converter scope is ambiguous")
                result = await client.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": found[0],
                        "functionDeclaration": native_script(_CONVERT),
                        "awaitPromise": True,
                        "returnByValue": True,
                        "arguments": [
                            {
                                "value": {
                                    "reportId": session.config.report_id,
                                    "pageId": frame["pageId"],
                                    "version": str(frame["version"]),
                                    "changes": conversion_requests(changes)
                                    if requests is None
                                    else requests,
                                    "timeout": max(1, session.config.timeout_ms - 3000),
                                }
                            }
                        ],
                    },
                )
                if result.get("exceptionDetails"):
                    raise StudioError("NATIVE_CONTRACT_FAILED", "Isolated type conversion failed")
                value = result.get("result", {}).get("value", {})
                if value.get("error"):
                    raise StudioError(value["error"], "Native conversion rejected the draft")
                if value.get("shared_state_unchanged") is not True or not isinstance(
                    value.get("edits"), list
                ):
                    raise StudioError(
                        "NATIVE_CONTRACT_FAILED", "Conversion isolation was not verified"
                    )
                failed = False
                return value["edits"]
        except StudioError:
            raise
        except Exception as exc:
            raise StudioError("NATIVE_CONTRACT_FAILED", "Type conversion transport failed") from exc
        finally:
            cleanup_error = None
            for cleanup in (
                lambda: client.send("Runtime.releaseObjectGroup", {"objectGroup": object_group}),
                client.detach,
            ):
                try:
                    await asyncio.wait_for(cleanup(), 3)
                except Exception as exc:
                    cleanup_error = exc
            # Keep the original conversion/cancellation error; cleanup must not
            # replace it with a raw transport exception. On success, fail closed.
            if cleanup_error is not None and not failed:
                raise StudioError(
                    "NATIVE_CONTRACT_FAILED", "Type conversion cleanup failed"
                ) from cleanup_error


_CONVERT = """async function({reportId,pageId,version,changes,timeout}) {
    const i=window.angular.element(document.body).injector();
    const report=i.get('reportContextService').getReport();
    if(report?.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    const owner=report.getPage(pageId);
    if(!owner?.isReady$?.value||String(owner.getVersion())!==version)return {error:'EDITOR_STALE'};
    if(owner.isResponsiveLayout())return {error:'RESPONSIVE_LAYOUT_REQUIRES_SEPARATE_OPERATION'};
    if([report,owner,...owner.getComponents()].some(m=>Object.values(m.isChanged||{}).some(Boolean)))
        return {error:'EDITOR_DIRTY'};
    const registry=i.get('legoMainRegistry');
    const serializers=R.candidates('pageConfigBuilder');
    if(serializers.length!==1)return {error:'CODEC_CONTRACT_CHANGED'};
    const service=i.get('vizSwitchService'),cache=i.get('globalVariableService');
    const cacheKeys=Object.keys(service).filter(k=>service[k]===cache);
    if(cacheKeys.length!==1)return {error:'CODEC_CONTRACT_CHANGED'};
    const clone=x=>JSON.parse(JSON.stringify(x));
    const snapshot=()=>JSON.stringify({report:report.getConfig(),page:owner.getConfig(),queues:service.queues,cache});
    const baseline=snapshot(),deadline=Date.now()+timeout,edits=[];
    const wire=c=>clone(serializers[0]({...owner.getConfig(),component:[c.getConfig()],group:[]},
        {displayName:'draft'},undefined,registry).page.componentConfig[0]);
    for(const spec of changes){
        const original=owner.getComponents().find(c=>c.getId()===spec.component_id);
        if(!original)return {error:'NOT_FOUND'};
        const targetType=registry.getDisplayTypeObj(spec.type,spec.preset);
        if(!targetType||!targetType.getPresetConfigNames().includes(spec.preset))return {error:'UNKNOWN_TYPE_OR_PRESET'};
        if(registry.getDisplayTypeGroup(spec.type)==='composite')return {error:'COMPOSITE_OPERATION_REQUIRED'};
        const before=wire(original);
        if(before.type===spec.type&&before.preset===spec.preset&&!spec.properties?.length){
            edits.push({before,after:before});continue;
        }
        const isolated=Object.assign(Object.create(Object.getPrototypeOf(service)),service);
        isolated.queues={};
        isolated[cacheKeys[0]]=Object.assign(Object.create(Object.getPrototypeOf(cache)),clone(cache));
        let sourceDraft,targetDraft,timer,failure;
        const ready=async c=>{try{await Promise.race([c.getReadyAsync(),new Promise((_,reject)=>{
            timer=setTimeout(()=>reject(Error('DRAFT_TIMEOUT')),Math.max(1,deadline-Date.now()));
        })]);}finally{clearTimeout(timer);}};
        try{
            const cfg=clone(original.getConfig());cfg.id='cd-'+crypto.randomUUID().replaceAll('-','');
            sourceDraft=owner.createChildComponent(cfg);await ready(sourceDraft);sourceDraft.syncState();
            const target=this(isolated,sourceDraft,spec.type,spec.preset);
            target.id='cd-'+crypto.randomUUID().replaceAll('-','');
            targetDraft=owner.createChildComponent(target);await ready(targetDraft);targetDraft.syncState();
            for(const {key:requestedKey,value} of spec.properties||[]){
                const role=requestedKey==='metrics'?'metrics':requestedKey==='actionConceptList'?'dimensions':null;
                const key=spec.type==='dimension-filter'?
                    ({actionConceptList:'controlField',metrics:'filterMetric'}[requestedKey]||requestedKey):requestedKey;
                const inputs=targetDraft.getDisplayTypeObj().getPropertyInputConfig(targetDraft);
                const input=Object.hasOwn(inputs,key)?inputs[key]:null,b=input?.bindingMethods;
                const reject=code=>{throw Object.assign(Error(code),{conversionCode:code});};
                if(!role||!b?.writeToConfig||!b?.readFromConfig||
                    !b.bindingKeys?.some(k=>k.type==='datasource'&&k.key===role))
                    reject('FIELD_BINDING_UNSUPPORTED');
                // Clearing an already empty role needs no disabled UI setter.
                const existing=b.readFromConfig(targetDraft);
                if(value.length===0&&Array.isArray(existing)&&existing.length===0)continue;
                const hooks=b.onConfigChange||{};
                if(b.customBinding||b.bindingObservable||b.skipSaveWrapper||
                    hooks.affectOtherProperty||hooks.affectOtherLayout||hooks.finally||input.onValueChange)
                    reject('PROPERTY_REQUIRES_UI_DEPENDENCIES');
                if(input.disabled===true)reject('PROPERTY_DISABLED');
                let shape;const ruleShape=()=>shape||(shape=R.ruleShape());
                const condition=rule=>{
                    if(typeof rule==='boolean')return rule;
                    if(rule?.every)return rule.every.every(condition);
                    if(rule?.some)return rule.some.some(condition);
                    if(rule?.not)return !condition(rule.not);
                    if(typeof rule?.callback==='function'){
                        const args=ruleShape().arguments.flatMap(field=>rule[field]||[]).map(k=>
                            inputs[k]?.bindingMethods?.readFromConfig?.(targetDraft));
                        if(rule[ruleShape().context])args.unshift({wrapper:targetDraft,
                            getDisplayType:()=>targetDraft.getDisplayType(),
                            getSchema:()=>targetDraft.getSchema(),getState:()=>targetDraft.getState(),
                            getNeedData:()=>targetDraft.needData(),getConcepts:k=>targetDraft.getConcepts(k),
                            getDatasourceOrViewId:()=>targetDraft.getDatasourceOrViewId()});
                        const result=rule.callback.apply(rule,args);
                        if(result===undefined||result===null)return false;
                        if(typeof result==='boolean')return result;
                    }
                    reject('PROPERTY_REQUIRES_UI_DEPENDENCIES');
                };
                if((input.enabledBy&&!condition(input.enabledBy))||
                    (input.enabledByEvery&&!input.enabledByEvery.every(condition))||
                    (input.enabledBySome&&!input.enabledBySome.some(condition)))
                    reject('PROPERTY_DISABLED');
                targetDraft.getSchema().getConcepts();
                for(const def of value){
                    const transform=def.queryTimeTransformation.dataTransformation;
                    if(transform.sourceType===1&&typeof transform.textFormula==='string')continue;
                    const field=transform.sourceFieldName;
                    if(!R.schemaField(targetDraft.getSchema(),field).isValid())reject('UNKNOWN_FIELD');
                }
                const event={...input,key,value,oldValue:clone(b.readFromConfig(targetDraft))};
                const pending=b.writeToConfig(targetDraft,event);
                if(pending&&typeof pending.then==='function')await ready({getReadyAsync:()=>pending});
                targetDraft.syncState();
                const hook=hooks.affectOtherConfig?.(targetDraft,event);
                if(hook&&typeof hook.then==='function')await ready({getReadyAsync:()=>hook});
                targetDraft.syncState();targetDraft.getSchema().getConcepts();
                // Native concept objects can reorder keys; compare structural JSON.
                const canonical=x=>Array.isArray(x)?x.map(canonical):x&&typeof x==='object'?
                    Object.fromEntries(Object.keys(x).sort().map(k=>[k,canonical(x[k])])):x;
                if(JSON.stringify(canonical(b.readFromConfig(targetDraft)))!==JSON.stringify(canonical(value)))
                    reject('PROPERTY_VALUE_NOT_APPLIED');
            }
            // Chart conversion can drop onSelect; controls require their own native
            // action, generated after the final field/parameter bindings are set.
            if(spec.type==='dimension-filter'){
                const behavior=targetDraft.getDisplayTypeObj().getCustomBehavior(targetDraft.getConfig(),'onSelect');
                if(!Array.isArray(behavior)||!behavior.length||behavior.some(b=>b.isControl!==true))
                    throw Error('Missing native control behavior');
                targetDraft.setBehavior('onSelect',clone(behavior));targetDraft.syncState();
            }
            targetDraft.validate();
            if(Object.values(targetDraft.getConfigErrors()).some(v=>Object.keys(v||{}).length))
                failure='INVALID_COMPONENT_CONFIGURATION';
            else {
                const after=wire(targetDraft);after.componentId=before.componentId;
                edits.push({before,after});
            }
        }catch(error){
            failure=error?.conversionCode||(error?.message==='DRAFT_TIMEOUT'?'DRAFT_TIMEOUT':'NATIVE_CONTRACT_FAILED');
        }finally{
            for(const draft of [targetDraft,sourceDraft]){
                try{draft?.dispose();}catch{failure||='NATIVE_CONTRACT_FAILED';}
            }
        }
        if(snapshot()!==baseline)return {error:'UNEXPECTED_SIDE_EFFECT'};
        if(failure)return {error:failure};
        if(Date.now()>deadline)return {error:'DRAFT_TIMEOUT'};
    }
    return {edits,shared_state_unchanged:snapshot()===baseline};
}"""


def requires_loss_approval(preview: dict[str, Any]) -> bool:
    structural = {
        "/type",
        "/preset",
        "/conceptDefs",
        "/attributeConfig/componentAttribute/displayConfigVersion",
    }
    return bool(
        preview["removed_paths"]
        or preview["removed_concept_names"]
        or preview["changed_concept_names"]
        or not json_equal(preview["bindings_before"], preview["bindings_after"])
        or any(c["kind"] == "changed" and c["path"] not in structural for c in preview["changes"])
    )


def switch_changes(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Loss/change preview; arrays are reported whole, never falsely matched by index."""
    if json_equal(before, after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(before.keys() | after.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in after:
                changes.append(
                    {"path": child, "kind": "removed", "before": copy.deepcopy(before[key])}
                )
            elif key not in before:
                changes.append({"path": child, "kind": "added", "after": copy.deepcopy(after[key])})
            else:
                changes.extend(switch_changes(before[key], after[key], child))
        return changes
    return [
        {
            "path": path or "/",
            "kind": "changed",
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
        }
    ]


def merge_type_switch(
    native_before: dict[str, Any], native_after: dict[str, Any], saved: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge an identity-preserving same-source native type conversion into full saved JSON.

    Caller restores the original component ID on both native draft wires before
    entering this boundary. Preview includes removed old-type settings, even when
    their serialized parent contained opaque values not visible to the editor.
    """
    for key in ("componentId", "type", "preset", "datasourceId"):
        if not json_equal(native_before.get(key), saved.get(key)):
            raise StudioError(
                "EDITOR_STALE", "Native source identity/type/binding differs from saved component"
            )
    if not isinstance(saved.get("componentId"), str) or not isinstance(
        native_after.get("type"), str
    ):
        raise StudioError("CODEC_CONTRACT_CHANGED", "Type conversion identity is incomplete")
    for key in ("componentId", "datasourceId"):
        if not json_equal(native_before.get(key), native_after.get(key)):
            raise StudioError(
                "UNEXPECTED_SIDE_EFFECT", "Type conversion changed component identity or datasource"
            )
    merged = overlay_native_delta(
        {k: v for k, v in native_before.items() if k != "conceptDefs"},
        {k: v for k, v in native_after.items() if k != "conceptDefs"},
        saved,
    )
    merged["conceptDefs"] = merge_concept_definitions(
        native_before.get("conceptDefs", []),
        native_after.get("conceptDefs", []),
        saved.get("conceptDefs", []),
    )
    changes = switch_changes(saved, merged)
    old_defs = {c["name"]: c for c in saved.get("conceptDefs", [])}
    new_defs = {c["name"]: c for c in merged["conceptDefs"]}
    old_props = saved.get("propertyConfig", {}).get("componentProperty", {})
    new_props = merged.get("propertyConfig", {}).get("componentProperty", {})
    preview = {
        "from": {"type": saved.get("type"), "preset": saved.get("preset")},
        "to": {"type": merged.get("type"), "preset": merged.get("preset")},
        "changes": changes,
        "removed_paths": [c["path"] for c in changes if c["kind"] == "removed"],
        "removed_concept_names": sorted(old_defs.keys() - new_defs.keys()),
        "added_concept_names": sorted(new_defs.keys() - old_defs.keys()),
        "changed_concept_names": sorted(
            name
            for name in old_defs.keys() & new_defs.keys()
            if not json_equal(old_defs[name], new_defs[name])
        ),
        "bindings_before": {
            k: copy.deepcopy(old_props[k])
            for k in ("dimensions", "metrics", "sort")
            if k in old_props
        },
        "bindings_after": {
            k: copy.deepcopy(new_props[k])
            for k in ("dimensions", "metrics", "sort")
            if k in new_props
        },
        "scope": "Saved JSON changes, not proof of equivalent rendering or numeric results",
    }
    return merged, preview
