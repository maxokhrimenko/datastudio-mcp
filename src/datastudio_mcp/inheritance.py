"""Saved and native-resolved filter inheritance across a report page."""

import asyncio
from typing import Any

from .browser import StudioError
from .bundle import native_script
from .document import find_page, fingerprint, walk_components
from .filters import filter_inventory


def _binding(props: dict[str, Any], scope: str) -> dict[str, Any]:
    ids = props.get("filters", [])
    inherit = props.get("inheritFilters") if scope != "report" else None
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise StudioError("FILTER_CONTRACT_CHANGED", f"Malformed {scope} filter IDs")
    if scope != "report" and type(inherit) is not bool:
        raise StudioError("FILTER_CONTRACT_CHANGED", f"Malformed {scope} inheritance flag")
    return {"scope": scope, "filter_ids": ids, "inherit_filters": inherit}


def saved_inheritance(config: dict[str, Any], page_id: str) -> dict[str, Any]:
    frame = next((item for item in config.get("page", []) if item.get("pageId") == page_id), None)
    if frame is None:
        raise StudioError("NOT_FOUND", "Page does not belong to report")
    page = frame.get("page")
    report = config.get("report")
    if not isinstance(page, dict) or not isinstance(report, dict):
        raise StudioError("INVALID_REPORT", "Saved report/page content is missing")
    report_props = report.get("propertyConfig", {}).get("reportProperty", {})
    page_props = page.get("propertyConfig", {}).get("pageProperty", {})
    if not isinstance(report_props, dict) or not isinstance(page_props, dict):
        raise StudioError("FILTER_CONTRACT_CHANGED", "Report/page properties are malformed")
    report_binding = _binding(report_props, "report")
    page_binding = _binding(page_props, "page")

    groups = page.get("groupConfig", [])
    if not isinstance(groups, list):
        raise StudioError("INVALID_PAGE", "Group registry is malformed")
    by_group = {group.get("groupId"): group for group in groups if isinstance(group, dict)}
    if len(by_group) != len(groups) or None in by_group:
        raise StudioError("INVALID_PAGE", "Group IDs are missing or duplicated")
    parent_group: dict[str, str] = {}
    component_group: dict[str, str] = {}
    for group_id, group in by_group.items():
        for child in group.get("childGroupId", []):
            if child not in by_group or child in parent_group:
                raise StudioError("INVALID_GROUP", "Child group is missing or multiply owned")
            parent_group[child] = group_id
        for component_id in group.get("componentId", []):
            if component_id in component_group:
                raise StudioError("INVALID_GROUP", "Component belongs to multiple groups")
            component_group[component_id] = group_id

    def group_chain(group_id: str | None) -> list[str]:
        result = []
        seen = set()
        while group_id is not None:
            if group_id in seen:
                raise StudioError("INVALID_GROUP", "Group hierarchy contains a cycle")
            seen.add(group_id)
            result.append(group_id)
            group_id = parent_group.get(group_id)
        return list(reversed(result))

    known_components = list(walk_components(page.get("componentConfig", [])))
    component_ids = {component.get("componentId") for component in known_components}
    if None in component_ids or len(component_ids) != len(known_components):
        raise StudioError("INVALID_PAGE", "Component IDs are missing or duplicated")
    if not set(component_group) <= component_ids:
        raise StudioError("INVALID_GROUP", "Group references a missing component")

    components = []
    for component in known_components:
        component_id = component["componentId"]
        chain = [report_binding, page_binding]
        group_ids = group_chain(component_group.get(component_id))
        for group_id in group_ids:
            props = by_group[group_id].get("propertyConfig", {}).get("groupProperty")
            if not isinstance(props, dict):
                raise StudioError("FILTER_CONTRACT_CHANGED", "Group properties are malformed")
            item = _binding(props, "group")
            item.update(
                {
                    "group_id": group_id,
                    "control_state_pass_through": props.get("controlStatePassThrough", False),
                }
            )
            if type(item["control_state_pass_through"]) is not bool:
                raise StudioError("FILTER_CONTRACT_CHANGED", "Malformed control pass-through")
            chain.append(item)
        props = component.get("propertyConfig", {}).get("componentProperty")
        if not isinstance(props, dict):
            components.append(
                {
                    "component_id": component_id,
                    "type": component.get("type"),
                    "datasource_id": component.get("datasourceId"),
                    "status": "not_applicable",
                    "group_ids": group_ids,
                }
            )
            continue
        chain.append(_binding(props, "component"))
        effective: list[str] = []
        resolved_chain = []
        for item in chain:
            if item["inherit_filters"] is False:
                effective = []
            effective.extend(item["filter_ids"])
            resolved_chain.append({**item, "effective_filter_ids_after_scope": list(effective)})
        scope = "page"
        scope_id = page_id
        if group_ids:
            scope, scope_id = "group", group_ids[-1]
            for group_id in reversed(group_ids):
                props = by_group[group_id]["propertyConfig"]["groupProperty"]
                if not props.get("controlStatePassThrough", False):
                    break
                parent = parent_group.get(group_id)
                scope, scope_id = ("group", parent) if parent else ("page", page_id)
        components.append(
            {
                "component_id": component_id,
                "type": component.get("type"),
                "datasource_id": component.get("datasourceId"),
                "status": "resolved",
                "group_ids": group_ids,
                "chain": resolved_chain,
                "effective_saved_filter_ids": effective,
                "runtime_control_scope": {"scope": scope, "id": scope_id},
            }
        )
    return {
        "page_id": page_id,
        "page_fingerprint": fingerprint(page),
        "components": components,
        "scope": (
            "Saved structural inheritance. Native compatibility/effective runtime layers are "
            "reported separately; source equality alone is not compatibility proof."
        ),
    }


class Inheritance:
    def __init__(self, studio):
        self.studio = studio

    async def inspect(self, page_id: str) -> dict[str, Any]:
        raw = await self.studio.report()
        config = raw["reportConfig"]
        frame = find_page(raw, page_id)
        saved = saved_inheritance(config, page_id)
        inventory = filter_inventory(config, set())
        definitions = {
            item["id"]: item["definition"] for item in inventory["filters"]
        }
        page = await self.studio.session.page()
        try:
            native = await asyncio.wait_for(
                page.evaluate(
                    native_script(_NATIVE),
                    {
                        "reportId": self.studio.config.report_id,
                        "pageId": page_id,
                        "version": str(frame["version"]),
                        "filters": definitions,
                    },
                ),
                timeout=self.studio.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError(
                "NATIVE_CONTRACT_FAILED", "Cannot resolve native filter inheritance"
            ) from exc
        if native.get("error"):
            raise StudioError(native["error"], "Native filter inheritance is unavailable")
        if fingerprint((await self.studio.report())["reportConfig"]) != fingerprint(config):
            raise StudioError("VERSION_CONFLICT", "Report changed during inheritance inspection")
        return {
            **saved,
            "native": native,
            "report_content_fingerprint": inventory["report_content_fingerprint"],
        }


_NATIVE = """({reportId,pageId,version,filters}) => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService'))
        return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport(),page=report?.getPage(pageId);
    if(report?.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    if(!page||String(page.getVersion())!==version)return {error:'EDITOR_STALE'};
    const components=page.getComponents();
    const nodes=new Set([report,page]);
    for(const component of components)
        for(let node=component;node&&!nodes.has(node);node=node.getParent?.())nodes.add(node);
    if([...nodes].some(node=>Object.values(node.isChanged||{}).some(Boolean)))
        return {error:'EDITOR_DIRTY'};
    const clean=value=>value===undefined?null:JSON.parse(JSON.stringify(value));
    const validators=R.candidates('filterValidator');
    if(validators.length!==1)return {error:'FILTER_CONTRACT_CHANGED'};
    const result=[];
    for(const component of components){
        const item={component_id:component.getId(),type:component.getDisplayType(),
            datasource_id:component.getDatasourceOrViewId?.()||null,
            saved_filter_layers:clean(R.stateLayers(component.getState(),'filters')),
            runtime_cross_filters:clean(component.getState().getValue('crossFilters'))};
        if(!component.getDisplayTypeObj().getNeedData()){
            result.push({...item,status:'not_applicable'});continue;
        }
        if(component.isReady$?.value!==true){
            result.push({...item,status:'unavailable'});continue;
        }
        try{
            const schema=component.getSchema();
            item.compatibility=Object.entries(filters).map(([filter_id,definition])=>({
                filter_id,valid:validators[0](structuredClone(definition),schema)}));
            // The bundle no longer exports its effective-filter resolver.
            item.effective_filters=null;
            item.effective_filters_status='not_exposed_by_bundle';
            result.push({...item,status:'available'});
        }catch{result.push({...item,status:'unavailable'})}
    }
    return {components:result,scope:'Native saved provenance, runtime cross-filters, '
        +'and schema compatibility for loaded page components; effective runtime definitions '
        +'are not exposed by the current bundle'};
}"""
