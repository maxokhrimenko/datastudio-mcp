"""Named property edits, preserving saved fields outside the native delta."""

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .browser import StudioError
from .bundle import native_script
from .components import Components, select_top_level
from .document import fingerprint, json_equal, merge_concept_definitions, overlay_native_delta

if TYPE_CHECKING:
    from .service import Studio


class PropertyChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    key: str = Field(min_length=1)
    value: JsonValue


class ComponentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    component_id: str = Field(min_length=1)
    properties: list[PropertyChange] = Field(min_length=1, max_length=100)


class Settings:
    def __init__(self, studio: Studio, *, field_mode: bool = False):
        self.studio = studio
        self.field_mode = field_mode

    async def patch(
        self,
        page_id: str,
        changes: list[ComponentSettings],
        expected: str,
        *,
        dry_run: bool = True,
        expected_sources: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        ids = [c.component_id for c in changes]
        if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct components")
        if any(len({p.key for p in c.properties}) != len(c.properties) for c in changes):
            raise StudioError("INVALID_BATCH", "Property keys must be unique per component")
        async with self.studio.mutation_lock():
            _, frame = await Components(self.studio)._checked(page_id, expected)
            selected = select_top_level(frame["page"], ids)
            source_tokens = {}
            if self.field_mode:
                for source_id in {c.get("datasourceId") for c in selected}:
                    if not source_id:
                        raise StudioError("MISSING_DATASOURCE", "Component has no datasource")
                    source = await self.studio.datasource(source_id)
                    if not json_equal(source["draft"], source["published"]):
                        raise StudioError(
                            "UNPUBLISHED_DATASOURCE",
                            "Field edits require matching draft/published schema",
                        )
                    source_tokens[source_id] = fingerprint(source)
                    if source_id in (expected_sources or {}) and (
                        fingerprint(source["draft"]) != expected_sources[source_id]
                        or not json_equal(source["draft"], source["published"])
                    ):
                        raise StudioError("VERSION_CONFLICT", "Validated formula source changed")
            result = await self._prepare(frame, changes)
            for source_id, token in source_tokens.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError(
                        "VERSION_CONFLICT", "Datasource changed during field preparation"
                    )
            if len(result["edits"]) != len(selected):
                raise StudioError("CODEC_CONTRACT_CHANGED", "Native edit count differs")
            updated = {}
            for saved, edit in zip(selected, result["edits"], strict=True):
                original, after = edit["before"], edit["after"]
                if original["componentId"] != after["componentId"]:
                    raise StudioError("CODEC_CONTRACT_CHANGED", "Property edit changed identity")
                if self.field_mode:
                    merged = overlay_native_delta(
                        {k: v for k, v in original.items() if k != "conceptDefs"},
                        {k: v for k, v in after.items() if k != "conceptDefs"},
                        saved,
                    )
                    merged["conceptDefs"] = merge_concept_definitions(
                        original.get("conceptDefs", []),
                        after.get("conceptDefs", []),
                        saved.get("conceptDefs", []),
                    )
                else:
                    merged = overlay_native_delta(original, after, saved)
                if any(
                    not json_equal(saved.get(k), merged.get(k))
                    for k in ("componentId", "type", "preset", "datasourceId")
                    + (() if self.field_mode else ("conceptDefs",))
                ):
                    raise StudioError("UNEXPECTED_SIDE_EFFECT", "Style edit changed field/identity")
                updated[saved["componentId"]] = merged
            after_page = copy.deepcopy(frame["page"])
            after_page["componentConfig"] = [
                updated.get(c["componentId"], c) for c in after_page["componentConfig"]
            ]
            plan = {"page_id": page_id, "properties": result["properties"], "atomic_scope": "page"}
            if dry_run:
                return {"status": "planned", "updated_components": list(updated.values()), **plan}
            if json_equal(frame["page"], after_page):
                return {"status": "unchanged", **plan}
            for source_id, token in (expected_sources or {}).items():
                source = await self.studio.datasource(source_id)
                if fingerprint(source["draft"]) != token or not json_equal(
                    source["draft"], source["published"]
                ):
                    raise StudioError("VERSION_CONFLICT", "Formula source changed before write")
            return {**await self.studio._write_page(frame, after_page), **plan}

    async def _prepare(self, frame: dict[str, Any], changes: list[ComponentSettings]):
        session = self.studio.session
        page = await session.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    native_script(_EDIT),
                    {
                        "reportId": session.config.report_id,
                        "pageId": frame["pageId"],
                        "version": str(frame["version"]),
                        "changes": [c.model_dump() for c in changes],
                        "timeout": session.config.timeout_ms - 1000,
                        "fieldMode": self.field_mode,
                    },
                ),
                timeout=session.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError("NATIVE_CONTRACT_FAILED", "Property preparation failed") from exc
        if result.get("error"):
            raise StudioError(result["error"], str(result.get("details", "Property edit refused")))
        return result


_EDIT = """async ({reportId,pageId,version,changes,timeout,fieldMode}) => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService'))return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport();
    if(report?.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    const owner=report.getPage(pageId);
    if(owner?.isReady$?.value!==true)return {error:'EDITOR_NOT_READY'};
    if(String(owner.getVersion())!==version)return {error:'EDITOR_STALE'};
    if(owner.isResponsiveLayout())return {error:'RESPONSIVE_LAYOUT_REQUIRES_SEPARATE_OPERATION'};
    if([report,owner,...owner.getComponents()].some(m=>Object.values(m.isChanged||{}).some(Boolean)))
        return {error:'EDITOR_DIRTY'};
    const registry=i.get('legoMainRegistry');
    const serializers=R.candidates('pageConfigBuilder');
    if(serializers.length!==1)return {error:'CODEC_CONTRACT_CHANGED'};
    const clean=v=>v===undefined?null:JSON.parse(JSON.stringify(v));
    const equal=(a,b)=>{
        if(a===b)return true;
        if(!a||!b||typeof a!=='object'||typeof b!=='object')return false;
        if(Array.isArray(a)!==Array.isArray(b))return false;
        const keys=Object.keys(a);
        return keys.length===Object.keys(b).length&&
            keys.every(k=>Object.hasOwn(b,k)&&equal(a[k],b[k]));
    };
    const edits=[],properties=[],start=Date.now();
    for(const change of changes){
        const source=owner.getComponent(change.component_id);
        if(!source||source.isReady$?.value!==true)return {error:'EDITOR_NOT_READY'};
        if(registry.getDisplayTypeGroup(source.getDisplayType())==='composite')
            return {error:'COMPOSITE_EDIT_REQUIRES_CHILDREN'};
        const cfg=structuredClone(source.getConfig());
        cfg.id='cd-'+crypto.randomUUID().replaceAll('-','').slice(0,20);
        let c,timer;
        try{
            c=owner.createChildComponent(cfg);
            await Promise.race([c.getReadyAsync(),new Promise((_,reject)=>{
                timer=setTimeout(()=>reject(new Error('DRAFT_TIMEOUT')),
                    Math.max(1,timeout-(Date.now()-start)));
            })]);
            c.syncState();c.getSchema().getConcepts();
            const serialize=()=>clean(serializers[0](
                {...owner.getConfig(),component:[c.getConfig()],group:[]},
                {displayName:'draft'},undefined,registry).page.componentConfig[0]);
            const before=serialize();
            for(const {key:requestedKey,value} of change.properties){
                const role=requestedKey==='metrics'?'metrics':requestedKey==='actionConceptList'?'dimensions':null;
                const key=fieldMode&&c.getDisplayType()==='dimension-filter'?
                    ({actionConceptList:'controlField',metrics:'filterMetric'}[requestedKey]||requestedKey):requestedKey;
                const inputs=c.getDisplayTypeObj().getPropertyInputConfig(c);
                const input=Object.hasOwn(inputs,key)?inputs[key]:null,b=input?.bindingMethods;
                if(!b?.writeToConfig||!b?.readFromConfig)
                    return {error:'PROPERTY_NOT_WRITABLE',details:key};
                if(fieldMode&&(!role||!b.bindingKeys?.some(k=>k.type==='datasource'&&k.key===role)))
                    return {error:'FIELD_BINDING_UNSUPPORTED',details:key};
                const existing=fieldMode?b.readFromConfig(c):null;
                if(fieldMode&&value.length===0&&Array.isArray(existing)&&existing.length===0){
                    properties.push({component_id:change.component_id,key,before:[],after:[]});
                    continue;
                }
                if(!b.bindingKeys?.length||(!fieldMode&&b.bindingKeys.some(k=>k.type!=='display'))||
                    b.customBinding||b.bindingObservable||b.skipSaveWrapper)
                    return {error:'PROPERTY_REQUIRES_SPECIAL_EDITOR',details:key};
                const hooks=b.onConfigChange||{};
                // affectOtherLayout only reshapes the property panel (nothing is saved);
                // the other hooks change further properties and need the full UI flow.
                if(hooks.affectOtherProperty||hooks.finally||input.onValueChange)
                    return {error:'PROPERTY_REQUIRES_UI_DEPENDENCIES',details:key};
                if(input.disabled===true)return {error:'PROPERTY_DISABLED',details:key};
                if(fieldMode){
                    for(const def of value){
                        const transform=def.queryTimeTransformation.dataTransformation;
                        if(transform.sourceType===1&&typeof transform.textFormula==='string')
                            continue;
                        const field=transform.sourceFieldName;
                        if(!R.schemaField(c.getSchema(),field).isValid())
                            return {error:'UNKNOWN_FIELD',details:field};
                    }
                }
                let shape;const ruleShape=()=>shape||(shape=R.ruleShape());
                const condition=rule=>{
                    if(typeof rule==='boolean')return rule;
                    if(rule?.every)return rule.every.every(condition);
                    if(rule?.some)return rule.some.some(condition);
                    if(rule?.not)return !condition(rule.not);
                    if(typeof rule?.callback==='function'){
                        const dependencies=ruleShape().arguments.flatMap(field=>rule[field]||[]);
                        const args=dependencies.map(k=>
                            inputs[k]?.bindingMethods?.readFromConfig?.(c));
                        if(rule[ruleShape().context])args.unshift({wrapper:c,getDisplayType:()=>c.getDisplayType(),
                            getSchema:()=>c.getSchema(),getState:()=>c.getState(),
                            getNeedData:()=>c.needData(),getConcepts:k=>c.getConcepts(k),
                            getDatasourceOrViewId:()=>c.getDatasourceOrViewId()});
                        const result=rule.callback.apply(rule,args);
                        if(result===undefined||result===null)return false;
                        if(typeof result==='boolean')return result;
                    }
                    throw new Error('UNSUPPORTED_PROPERTY_CONDITION');
                };
                try{
                    if((input.enabledBy&&!condition(input.enabledBy))||
                       (input.enabledByEvery&&!input.enabledByEvery.every(condition))||
                       (input.enabledBySome&&!input.enabledBySome.some(condition)))
                        return {error:'PROPERTY_DISABLED',details:key};
                }catch{return {error:'PROPERTY_REQUIRES_UI_DEPENDENCIES',details:key}}
                if(input.type==='toggleswitch'&&typeof value!=='boolean')
                    return {error:'INVALID_PROPERTY_VALUE',details:key};
                if(Array.isArray(input.options)&&input.options.length&&
                    !input.options.some(o=>equal(o.value,value)))
                    return {error:'INVALID_PROPERTY_OPTION',details:key};
                const prior=clean(b.readFromConfig(c)),event={...input,key,value,oldValue:prior};
                const pending=b.writeToConfig(c,event);
                if(pending&&typeof pending.then==='function')
                    return {error:'PROPERTY_REQUIRES_ASYNC_EDITOR',details:key};
                c.syncState();hooks.affectOtherConfig?.(c,event);c.syncState();
                if(fieldMode)c.getSchema().getConcepts();
                const actual=clean(b.readFromConfig(c));
                if(!equal(actual,value))return {error:'PROPERTY_VALUE_NOT_APPLIED',details:key};
                properties.push({component_id:change.component_id,key,before:prior,after:actual});
            }
            c.validate();
            if(Object.values(c.getConfigErrors()).some(v=>Object.keys(v||{}).length))
                return {error:'INVALID_COMPONENT_CONFIGURATION',details:c.getConfigErrors()};
            edits.push({before,after:serialize()});
        }finally{clearTimeout(timer);c?.dispose()}
    }
    return {edits,properties};
}"""
