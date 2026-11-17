"""Page-local saved filter bindings and their potential consumer compatibility."""

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING

from .browser import StudioError
from .bundle import native_script
from .component_filters import FilterBinding
from .document import find_page, fingerprint, json_equal
from .filters import filter_inventory

if TYPE_CHECKING:
    from .service import Studio


class PageFilters:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def patch(
        self,
        page_id: str,
        change: FilterBinding,
        expected_page: str,
        expected_report: str,
        *,
        dry_run: bool = True,
    ) -> dict:
        async with self.studio.mutation_lock():
            raw = await self.studio.report()
            frame = find_page(raw, page_id)
            inventory = filter_inventory(raw["reportConfig"], set())
            if (
                fingerprint(frame["page"]) != expected_page
                or inventory["report_content_fingerprint"] != expected_report
            ):
                raise StudioError("VERSION_CONFLICT", "Page/report resources changed")
            target = copy.deepcopy(frame["page"])
            props = target.get("propertyConfig", {}).get("pageProperty")
            if not isinstance(props, dict):
                raise StudioError("INVALID_PAGE", "Page properties missing")
            old_ids, old_inherit = props.get("filters", []), props.get("inheritFilters")
            if (
                not isinstance(old_ids, list)
                or any(not isinstance(f, str) for f in old_ids)
                or type(old_inherit) is not bool
            ):
                raise StudioError("FILTER_CONTRACT_CHANGED", "Malformed saved page filter binding")
            ids = old_ids if change.filter_ids is None else change.filter_ids
            known = {f["id"]: f["definition"] for f in inventory["filters"]}
            if any(item not in known for item in ids):
                raise StudioError("NOT_FOUND", "Saved filter ID not found in report")
            filters, sources = [], {}
            for filter_id in ids:
                definition = known[filter_id]
                source_id = definition.get("datasourceId")
                if not isinstance(source_id, str) or not source_id:
                    raise StudioError("INVALID_FILTER", "Filter source ID missing")
                if source_id not in sources:
                    source = await self.studio.datasource(source_id)
                    if not json_equal(source["draft"], source["published"]):
                        raise StudioError(
                            "UNPUBLISHED_DATASOURCE", "Matching draft/published required"
                        )
                    sources[source_id] = fingerprint(source)
                filters.append({"id": filter_id, "sourceId": source_id, "definition": definition})
            native = await self._validate(frame, old_ids, old_inherit, filters)
            for source_id, token in sources.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError("VERSION_CONFLICT", "Filter datasource changed")
            fresh = await self.studio.report()
            if fingerprint(fresh["reportConfig"]["report"]) != expected_report or not json_equal(
                find_page(fresh, page_id), frame
            ):
                raise StudioError("VERSION_CONFLICT", "Page/report changed during preparation")
            if change.filter_ids is not None:
                props["filters"] = list(change.filter_ids)
            if change.inherit_filters is not None:
                props["inheritFilters"] = change.inherit_filters
            plan = {
                "page_id": page_id,
                "before": {"filter_ids": old_ids, "inherit_filters": old_inherit},
                "after": {
                    "filter_ids": props.get("filters", []),
                    "inherit_filters": props["inheritFilters"],
                },
                "validation": native,
                "atomic_scope": "page",
                "source_guard": "Preflight, not cross-entity CAS",
            }
            if dry_run:
                return {"status": "planned", **plan}
            if json_equal(frame["page"], target):
                return {"status": "unchanged", **plan}
            return {
                **await self.studio._write_page(
                    frame, target, expected_report_fingerprint=expected_report
                ),
                **plan,
            }

    async def _validate(
        self, frame: dict, ids: list[str], inherit: bool, filters: list[dict],
        group_id: str | None = None,
    ):
        page = await self.studio.session.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    native_script(_VALIDATE),
                    {
                        "reportId": self.studio.config.report_id,
                        "pageId": frame["pageId"],
                        "version": str(frame["version"]),
                        "beforeIds": ids,
                        "beforeInherit": inherit,
                        "filters": filters,
                        "groupId": group_id,
                    },
                ),
                timeout=self.studio.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError(
                "NATIVE_CONTRACT_FAILED", "Page filter validation unavailable"
            ) from exc
        if result.get("error"):
            raise StudioError(result["error"], "Page filter validation refused")
        return result


_VALIDATE = """({reportId,pageId,version,beforeIds,beforeInherit,filters,groupId}) => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService')||!i.has('schemaService'))return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport();
    if(report?.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    const versionOwner=pageId===null?report:report.getPage(pageId);
    if(!versionOwner||String(versionOwner.getVersion())!==version)return {error:'EDITOR_STALE'};
    const owner=groupId?versionOwner.getGroup(groupId):versionOwner;
    if(!owner||owner.isReady$?.value!==true)return {error:'EDITOR_NOT_READY'};
    const components=owner.getComponents();
    const ancestors=new Set([report,versionOwner,...components]);
    for(let node=owner;node&&!ancestors.has(node);node=node.getParent?.())ancestors.add(node);
    if([...ancestors].some(c=>Object.values(c.isChanged||{}).some(Boolean)))
        return {error:'EDITOR_DIRTY'};
    const equal=(a,b)=>{
        if(a===b)return true;
        if(!a||!b||typeof a!=='object'||typeof b!=='object'||Array.isArray(a)!==Array.isArray(b))return false;
        const keys=Object.keys(a);
        return keys.length===Object.keys(b).length&&keys.every(k=>Object.hasOwn(b,k)&&equal(a[k],b[k]));
    };
    const key=owner.getAllDataSourceKeys().filters;
    if(key?.stateKey!=='filters'||![3,4].includes(key.dominant))return {error:'FILTER_CONTRACT_CHANGED'};
    if((key.dominant===3)!==beforeInherit||!equal(owner.getDataSourceKeyValue('filters')||[],beforeIds))
        return {error:'EDITOR_STALE'};
    const validators=R.candidates('filterValidator');
    if(validators.length!==1)return {error:'FILTER_CONTRACT_CHANGED'};
    for(const item of filters){
        if(!equal(report.getResourceById('filter',item.id),item.definition))return {error:'EDITOR_STALE'};
        const schema=i.get('schemaService').getSchema(item.sourceId);
        if(!schema||schema.isDummy())return {error:'SCHEMA_NOT_READY'};
        if(!validators[0](structuredClone(item.definition),schema))return {error:'INVALID_FILTER'};
    }
    const consumers=components.map(c=>{
        const item={component_id:c.getId(),type:c.getDisplayType()};
        if(!c.getDisplayTypeObj().getNeedData())return {...item,status:'not_applicable'};
        if(c.isReady$?.value!==true)return {...item,status:'unavailable'};
        try{
            const schema=c.getSchema();
            if(!schema||schema.isDummy())return {...item,status:'unavailable'};
            return {...item,status:'available',compatibility:filters.map(f=>({filter_id:f.id,
                valid:validators[0](structuredClone(f.definition),schema)}))};
        }catch{return {...item,status:'unavailable'}}
    });
    return {consumers,owner_scope:groupId?'group':pageId===null?'report':'page',
        scope:'Potential compatibility on owner.getComponents() schemas only; '
        +'not effective inheritance, child-page/group/blend-input traversal or query-result proof'};
}"""
