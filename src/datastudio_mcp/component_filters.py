"""Saved component filter bindings, distinct from transient viewer interactions."""

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .browser import StudioError
from .bundle import native_script
from .document import find_page, fingerprint, json_equal, walk_components
from .filters import filter_inventory

if TYPE_CHECKING:
    from .service import Studio


class FilterBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filter_ids: list[str] | None = Field(default=None, max_length=100)
    inherit_filters: bool | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.filter_ids is None and self.inherit_filters is None:
            raise ValueError("Provide filter_ids and/or inherit_filters")
        if self.filter_ids is not None and (
            any(not item for item in self.filter_ids)
            or len(set(self.filter_ids)) != len(self.filter_ids)
        ):
            raise ValueError("Filter IDs must be nonempty and unique")
        return self


class ComponentFilterChange(FilterBinding):
    component_id: str = Field(min_length=1)


class ComponentFilters:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def patch(
        self,
        page_id: str,
        changes: list[ComponentFilterChange],
        expected_page: str,
        expected_report: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        ids = [change.component_id for change in changes]
        if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct component IDs")
        async with self.studio.mutation_lock():
            raw = await self.studio.report()
            frame = find_page(raw, page_id)
            if fingerprint(frame["page"]) != expected_page:
                raise StudioError("VERSION_CONFLICT", "Page changed before filter binding")
            inventory = filter_inventory(raw["reportConfig"], set())
            if inventory["report_content_fingerprint"] != expected_report:
                raise StudioError("VERSION_CONFLICT", "Report/filter resources changed")
            filters = {item["id"]: item["definition"] for item in inventory["filters"]}
            target = copy.deepcopy(frame["page"])
            components = list(walk_components(target.get("componentConfig", [])))
            by_id = {component["componentId"]: component for component in components}
            if len(by_id) != len(components):
                raise StudioError("INVALID_PAGE", "Duplicate component IDs")
            if any(item not in by_id for item in ids):
                raise StudioError("NOT_FOUND", "Component does not belong to this page")
            plans, native_changes = [], []
            for change in changes:
                component = by_id[change.component_id]
                props = component.get("propertyConfig", {}).get("componentProperty")
                if not isinstance(props, dict):
                    raise StudioError("INVALID_COMPONENT", "Component properties are missing")
                old_ids, inherit = props.get("filters", []), props.get("inheritFilters")
                if (
                    not isinstance(old_ids, list)
                    or any(not isinstance(f, str) for f in old_ids)
                    or type(inherit) is not bool
                ):
                    raise StudioError("FILTER_CONTRACT_CHANGED", "Malformed saved filter binding")
                new_ids = old_ids if change.filter_ids is None else change.filter_ids
                if any(filter_id not in filters for filter_id in new_ids):
                    raise StudioError("NOT_FOUND", "Saved filter ID not found in this report")
                dataset = props.get("dataset", {})
                source_id = dataset.get("datasetId")
                if (
                    dataset.get("datasetType") != 1
                    or not isinstance(source_id, str)
                    or not source_id
                ):
                    raise StudioError(
                        "FILTER_DATASET_UNSUPPORTED", "Direct datasource binding required"
                    )
                next_inherit = inherit if change.inherit_filters is None else change.inherit_filters
                plans.append(
                    {
                        "component_id": change.component_id,
                        "before": {"filter_ids": old_ids, "inherit_filters": inherit},
                        "after": {"filter_ids": new_ids, "inherit_filters": next_inherit},
                    }
                )
                native_changes.append(
                    {
                        "componentId": change.component_id,
                        "datasourceId": source_id,
                        "beforeIds": old_ids,
                        "beforeInherit": inherit,
                        "filters": [{"id": f, "definition": filters[f]} for f in new_ids],
                    }
                )
                if change.filter_ids is not None:
                    props["filters"] = list(change.filter_ids)
                if change.inherit_filters is not None:
                    props["inheritFilters"] = change.inherit_filters
            source_tokens = {}
            for source_id in {item["datasourceId"] for item in native_changes}:
                source = await self.studio.datasource(source_id)
                if not json_equal(source["draft"], source["published"]):
                    raise StudioError(
                        "UNPUBLISHED_DATASOURCE", "Filter bindings require matching draft/published"
                    )
                source_tokens[source_id] = fingerprint(source)
            native = await self._validate(frame, native_changes)
            for source_id, token in source_tokens.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError(
                        "VERSION_CONFLICT", "Datasource changed during binding validation"
                    )
            fresh = await self.studio.report()
            if fingerprint(fresh["reportConfig"]["report"]) != expected_report or not json_equal(
                find_page(fresh, page_id), frame
            ):
                raise StudioError(
                    "VERSION_CONFLICT", "Report/page changed during binding validation"
                )
            plan = {
                "page_id": page_id,
                "changes": plans,
                "validation": native,
                "atomic_scope": "page",
                "source_guard": "Preflight, not cross-entity CAS",
                "scope": "Saved component-local filter IDs and inheritance; no filter creation, "
                "runtime control changes or inference of query results",
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

    async def _validate(self, frame: dict, changes: list[dict]) -> dict:
        page = await self.studio.session.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    native_script(_VALIDATE),
                    {
                        "reportId": self.studio.config.report_id,
                        "pageId": frame["pageId"],
                        "version": str(frame["version"]),
                        "changes": changes,
                    },
                ),
                timeout=self.studio.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError(
                "NATIVE_CONTRACT_FAILED", "Filter binding validation unavailable"
            ) from exc
        if result.get("error"):
            raise StudioError(result["error"], result.get("details", "Filter binding refused"))
        return result


_VALIDATE = """({reportId,pageId,version,changes}) => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService'))return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport();
    if(report?.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    const owner=report.getPage(pageId);
    if(!owner||owner.isReady$?.value!==true)return {error:'EDITOR_NOT_READY'};
    if(String(owner.getVersion())!==version)return {error:'EDITOR_STALE'};
    const validators=R.candidates('filterValidator');
    if(validators.length!==1)return {error:'FILTER_CONTRACT_CHANGED'};
    const equal=(a,b)=>{
        if(a===b)return true;
        if(!a||!b||typeof a!=='object'||typeof b!=='object'||Array.isArray(a)!==Array.isArray(b))return false;
        const keys=Object.keys(a);
        return keys.length===Object.keys(b).length&&keys.every(k=>Object.hasOwn(b,k)&&equal(a[k],b[k]));
    };
    const validated=[];
    for(const change of changes){
        const component=owner.getComponent(change.componentId);
        if(!component||component.isReady$?.value!==true)return {error:'EDITOR_NOT_READY'};
        const ancestors=new Set([report,owner]);
        for(let node=component;node&&!ancestors.has(node);node=node.getParent?.())ancestors.add(node);
        if([...ancestors].some(node=>Object.values(node.isChanged||{}).some(Boolean)))
            return {error:'EDITOR_DIRTY'};
        if(!component.getDisplayTypeObj().getNeedData())return {error:'FILTER_NOT_APPLICABLE'};
        const key=component.getAllDataSourceKeys().filters;
        if(key?.stateKey!=='filters'||![3,4].includes(key.dominant))return {error:'FILTER_CONTRACT_CHANGED'};
        if(!equal(component.getDataSourceKeyValue('filters')||[],change.beforeIds)||
            (key.dominant===3)!==change.beforeInherit)return {error:'EDITOR_STALE'};
        if(component.getDatasourceOrViewId()!==change.datasourceId)return {error:'EDITOR_STALE'};
        const schema=component.getSchema();
        if(!schema||schema.isDummy())return {error:'SCHEMA_NOT_READY'};
        for(const item of change.filters){
            if(!equal(report.getResourceById('filter',item.id),item.definition))
                return {error:'EDITOR_STALE',details:'Filter resource differs; report_refresh required'};
            if(!validators[0](structuredClone(item.definition),schema))
                return {error:'FILTER_INCOMPATIBLE',details:'Filter '+item.id+' is invalid for '+change.componentId};
        }
        validated.push({component_id:change.componentId,filter_ids:change.filters.map(item=>item.id),
                        datasource_id:change.datasourceId});
    }
    return {components:validated,scope:'Native validation on each component schema; not query-result proof'};
}"""
