"""Report health audit: everything that silently breaks a report or differs for viewers.

Saved rules are a pure function of the saved report and its attached datasources;
they need no native bundle contract and are offline-testable. The live pass walks
every page in the viewer and reads each data component's public error flags.
Read-only: the only side effect is viewer navigation, restored at the end.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from .blends import blend_inventory
from .browser import StudioError
from .document import json_equal, walk_components
from .filters import filter_inventory

if TYPE_CHECKING:
    from .service import Studio

# rule -> (severity, what it means for the report, how to fix it)
RULES: dict[str, tuple[str, str, str]] = {
    "datasource.unpublished_draft": (
        "warning", "Editors see the draft, viewers the published version",
        "Publish or discard the datasource draft"),
    "datasource.viewer_credentials": (
        "warning", "Charts on it show data only to viewers with their own data access",
        "Use owner or service-account credentials, or grant viewers data access"),
    "datasource.owner_credentials": (
        "warning", "Data access hangs on one person's Google account and breaks with it",
        "Switch to a service account for the project: datasource_credentials_set"),
    "datasource.broken_field": (
        "error", "Field formula references a column or field that no longer exists",
        "Fix or remove the field; refresh fields after a source schema change"),
    "filter.inline_formula": (
        "error", "Filter is built on an ad-hoc formula ('New field'), not a datasource field",
        "Create the field in the datasource and rebuild the condition on it"),
    "filter.missing_field": (
        "error", "Filter condition uses a field absent or disabled in its datasource",
        "Rebuild the condition on an existing enabled field"),
    "filter.detached_datasource": (
        "error", "Filter belongs to a datasource that is not attached to the report",
        "Delete the filter or attach its datasource"),
    "filter.dangling_reference": (
        "error", "A filter list points to a filter that does not exist",
        "Remove the stale filter ID from the component, page, report or blend input"),
    "filter.not_applicable": (
        "error", "Filter bound to a component uses fields its datasource does not have, "
                 "so it is silently not applied",
        "Bind a filter built on the component's datasource"),
    "filter.unused": (
        "info", "Saved filter is not used anywhere", "Delete it if it has no purpose"),
    "component.detached_datasource": (
        "error", "Component reads a datasource or blend that is not attached to the report",
        "Rebind the component or attach the source"),
    "component.missing_field": (
        "error", "Component uses a field absent or disabled in its datasource",
        "Rebind the field or restore it in the datasource"),
    "component.chart_formula": (
        "warning", "Calculated field defined inside the chart, invisible to other charts "
                   "and to datasource review",
        "Move the formula into a datasource field"),
    "component.stale_sort": (
        "warning", "The chart sorts by a field it no longer shows (a leftover of an earlier "
                   "configuration); the Sort panel can show something else",
        "settings_set the chart's sortConcept (field name) and sortDir again: the Sort "
        "panel rebuilds the effective sort"),
    "blend.issue": (
        "error", "Blend definition is structurally malformed", "Rebuild the blend"),
    "blend.detached_input": (
        "error", "Blend input reads a datasource that is not attached to the report",
        "Attach the datasource or replace the input"),
    "blend.dangling_reference": (
        "error", "A component points to a blend that does not exist", "Rebind the component"),
    "blend.unused": (
        "info", "Blend is not used anywhere", "Delete it if it has no purpose"),
    "render.error": (
        "error", "Component renders with an error in the viewer",
        "Read error.title/description and the flags; fix the datasource, access, "
        "filter or configuration it names"),
    "render.not_settled": (
        "info", "Component did not finish loading within the timeout; not verified",
        "Re-run the audit with a longer live_timeout_seconds"),
}

_REFERENCE = re.compile(r"\bt0\.([A-Za-z_][A-Za-z0-9_]*)")


def _plain(value: Any) -> Any:
    """Decode the protobuf-style Value tree of a component's behavior."""
    if isinstance(value, dict):
        if "mapValue" in value:
            return {e.get("key"): _plain(e.get("value", {}))
                    for e in value["mapValue"].get("entry", [])}
        if "arrayValue" in value:
            return [_plain(v) for v in value["arrayValue"].get("value", [])]
        for key in ("strValue", "int64Value", "boolValue", "doubleValue"):
            if key in value:
                return value[key]
    return value


def stale_sort(component: dict[str, Any]) -> list[str]:
    """Columns of the effective (behavior) sort that are neither shown nor the panel's sort."""
    prop = component.get("propertyConfig", {}).get("componentProperty", {})
    shown = {name for role in ("dimensions", "metrics")
             for item in (prop.get(role) or {}).get("labeledConcepts", [])
             for name in item.get("value", {}).get("conceptNames", [])}
    # Sorting by a field the chart does not show is legitimate when the panel says so.
    shown |= {item.get("sortColumn") for item in prop.get("sort") or []}
    behavior = _plain(component.get("behavior") or {})
    if not isinstance(behavior, dict) or not shown:
        return []
    columns = [entry.get("sortColumn", {}).get("name")
               for action in behavior.get("onSort") or []
               for entry in ((action.get("init") or {}).get("sortOptions") or {}).get(
                   "sortData") or []]
    return sorted({c for c in columns if c and c not in shown})


def finding(rule: str, location: dict[str, Any], **details: Any) -> dict[str, Any]:
    severity, meaning, fix = RULES[rule]
    return {"rule": rule, "severity": severity, "meaning": meaning, "fix": fix,
            "location": location, **details}


def _transformation(node: dict[str, Any]) -> dict[str, Any]:
    return node.get("queryTimeTransformation", {}).get("dataTransformation", {}) or {}


def _expressions(definition: Any):
    """Every leaf filterExpression of a filter definition, whatever its combinators."""
    if isinstance(definition, dict):
        if isinstance(definition.get("filterExpression"), dict):
            yield definition["filterExpression"]
        for key, value in definition.items():
            if key != "filterExpression":
                yield from _expressions(value)
    elif isinstance(definition, list):
        for item in definition:
            yield from _expressions(item)


def _fields(block: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    fields = (block or {}).get("datasourceBlock", {}).get("fields", [])
    return {f["field"]["name"]: f for f in fields
            if isinstance(f, dict) and isinstance(f.get("field"), dict)}


def _usable(fields: dict[str, dict[str, Any]], name: str) -> bool:
    return name in fields and fields[name].get("enabled") is not False


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*map(_strings, value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*map(_strings, value)) if value else set()
    return set()


def audit_datasource(source: dict[str, Any]) -> list[dict[str, Any]]:
    location = {"datasource_id": source["source"]["id"], "datasource": source["source"].get("name")}
    published = source.get("published") or {}
    findings = []
    if not json_equal(source["draft"], published):
        findings.append(finding("datasource.unpublished_draft", location))
    if published.get("delegatedAccessEnabled") is False:
        findings.append(finding("datasource.viewer_credentials", location))
    elif (source.get("credentials") or {}).get("type") == "owner":
        findings.append(finding("datasource.owner_credentials", location))
    fields = _fields(published)
    columns = {f["field"]["name"] for block in published.get("blocks", [])
               for f in block.get("fields", []) if isinstance(f.get("field"), dict)}
    for name, field in fields.items():
        if field.get("enabled") is False or not isinstance(field.get("textFormula"), str):
            continue
        # System fields (createdBy 1) read connector columns; user fields read fields.
        known = columns if field.get("createdBy") == 1 else fields.keys()
        missing = sorted({ref for ref in _REFERENCE.findall(field["textFormula"])
                          if ref not in known or (field.get("createdBy") != 1
                                                  and not _usable(fields, ref))})
        if missing and (field.get("createdBy") != 1 or columns):
            findings.append(finding("datasource.broken_field",
                                    {**location, "field": name,
                                     "field_label": field.get("outputName")},
                                    missing_references=missing))
    return findings


def audit_saved(config: dict[str, Any], sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Saved-state rules. `sources` maps every attached datasource ID to datasource()."""
    attached = set(sources)
    filters = filter_inventory(config, attached)
    blends = blend_inventory(config, attached, include_definitions=True)
    blend_fields = {b["id"]: _fields(b["definition"].get("blockDatasource"))
                    for b in blends["blends"]}
    published = {sid: _fields(s.get("published")) for sid, s in sources.items()}
    fields_of = {**published, **blend_fields}
    findings = [item for source in sources.values() for item in audit_datasource(source)]

    filter_fields = {}
    for item in filters["filters"]:
        location = {"filter_id": item["id"], "filter": item["definition"].get("name")}
        source_id = item["definition"].get("datasourceId")
        names, inline = set(), False
        for expression in _expressions(item["definition"].get("filterDefinition")):
            data = _transformation(expression)
            if data.get("sourceType") == 1 or isinstance(data.get("textFormula"), str):
                inline = True
            elif isinstance(data.get("sourceFieldName"), str):
                names.add(data["sourceFieldName"])
        filter_fields[item["id"]] = (inline, names)
        if inline:
            findings.append(finding("filter.inline_formula", location))
        if source_id not in fields_of:
            findings.append(finding("filter.detached_datasource",
                                    {**location, "datasource_id": source_id},
                                    referenced=not item["has_no_exact_references"]))
        else:
            missing = sorted(n for n in names if not _usable(fields_of[source_id], n))
            if missing:
                findings.append(finding("filter.missing_field", location, fields=missing))
        if item["has_no_exact_references"]:
            findings.append(finding("filter.unused", location))
    for ref in filters["dangling_filter_references"]:
        findings.append(finding("filter.dangling_reference", {
            **ref["owner"], "filter_id": ref["filter_id"], "path": ref["path"]}))

    for blend in blends["blends"]:
        location = {"blend_id": blend["id"], "blend": blend["name"]}
        for issue in blend["issues"]:
            findings.append(finding("blend.issue", location, issue=issue))
        for item in blend["inputs"]:
            # An input without any datasource ID is already a blend.issue.
            if item["datasource_id"] and not item["datasource_attached"]:
                findings.append(finding("blend.detached_input",
                                        {**location, "datasource_id": item["datasource_id"]}))
        if blend["has_no_exact_references"]:
            findings.append(finding("blend.unused", location))
    for ref in blends["dangling_dataset_references"]:
        findings.append(finding("blend.dangling_reference",
                                {**ref["owner"], "blend_id": ref["blend_id"], "path": ref["path"]}))

    known_filters = {item["id"] for item in filters["filters"]}
    scopes = [("report", None, config["report"].get("componentConfig", []))]
    scopes += [("page", page["pageId"], page["page"].get("componentConfig", []))
               for page in config["page"]]
    for _scope, page_id, components in scopes:
        for component in walk_components(components):
            prop = component.get("propertyConfig", {}).get("componentProperty", {})
            dataset = prop.get("dataset") or {}
            source_id = dataset.get("datasetId") or component.get("datasourceId")
            if not source_id:
                continue
            location = {"page_id": page_id, "component_id": component["componentId"],
                        "type": component.get("type"), "datasource_id": source_id}
            if source_id not in fields_of:
                # A missing blend is already reported as a dangling blend reference.
                if dataset.get("datasetType") != 2:
                    findings.append(finding("component.detached_datasource", location))
                continue
            fields = fields_of[source_id]
            # Saved conceptDefs keep orphans no property uses; the editor ignores them.
            used = _strings([component.get("propertyConfig"), component.get("behavior")])
            concepts = [c for c in component.get("conceptDefs", []) if c.get("name") in used]
            local = {c.get("name") for c in component.get("conceptDefs", [])}
            missing, affected, formulas = set(), [], []
            for concept in concepts:
                data = _transformation(concept)
                if isinstance(data.get("textFormula"), str):
                    formulas.append(concept.get("name"))
                    absent = {r for r in _REFERENCE.findall(data["textFormula"])
                              if r not in local and not _usable(fields, r)}
                elif isinstance(data.get("sourceFieldName"), str) and not _usable(
                        fields, data["sourceFieldName"]):
                    absent = {data["sourceFieldName"]}
                else:
                    absent = set()
                if absent:
                    missing |= absent
                    affected.append(concept.get("name"))
            if missing:
                findings.append(finding("component.missing_field", location,
                                        fields=sorted(missing), concepts=affected))
            if formulas:
                findings.append(finding("component.chart_formula", location, concepts=formulas))
            if stale := stale_sort(component):
                findings.append(finding("component.stale_sort", location, concepts=stale))
            for filter_id in prop.get("filters", []):
                if filter_id not in known_filters or filter_id not in filter_fields:
                    continue
                inline, names = filter_fields[filter_id]
                absent = sorted(n for n in names if not _usable(fields, n))
                if absent and not inline:
                    findings.append(finding("filter.not_applicable",
                                            {**location, "filter_id": filter_id}, fields=absent))
    return findings


_LIVE = """async ({reportId,pageId,includeReportLevel,timeout}) => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService'))return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport();
    if(report?.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    const page=report.getPage(pageId);
    if(!page)return {error:'NOT_FOUND'};
    const flatten=list=>list.flatMap(c=>[c,...flatten(c.getComponents?.()||[])]);
    const owned=flatten(page.getComponents());
    const global=includeReportLevel?flatten(report.getComponents?.()||[]):[];
    // The instance flag, not the type's: a button's type declares data it never loads.
    const data=[...global,...owned].filter((c,index,all)=>all.indexOf(c)===index&&
        (c.needData?.()??c.getDisplayTypeObj?.().getNeedData?.()));
    const settled=c=>c.hasError?.()||c.hasDataTableModel?.();
    const start=Date.now();
    while(Date.now()-start<timeout&&!data.every(settled))
        await new Promise(resolve=>setTimeout(resolve,250));
    // The user-facing error the viewer shows; stacks and links are dropped.
    const plain=value=>{
        if(value===null||value===undefined)return null;
        if(typeof value!=='object')return {message:String(value)};
        const out={};
        for(const key of ['reason','title','description','errorDialogText','message'])
            if(['string','number'].includes(typeof value[key]))out[key]=value[key];
        return out;
    };
    return {components:data.map(c=>({
        component_id:c.getId(),type:c.getDisplayType(),scope:global.includes(c)?'report':'page',
        datasource_id:c.getDatasourceOrViewId?.()||null,settled:!!settled(c),
        has_error:!!c.hasError?.(),category:c.getErrorCategory?.()??null,
        flags:{datasource:!!c.hasDatasourceError?.(),access_denied:!!c.hasAccessDeniedError?.(),
            configuration:!!c.hasConfigurationError?.(),system:!!c.hasSystemError?.(),
            viewers_credentials:!!c.hasViewersCredsDatasource?.()},
        error:plain(c.getError?.())}))};
}"""


def live_findings(page_id: str, components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    for item in components:
        location = {"page_id": page_id if item["scope"] == "page" else None,
                    "component_id": item["component_id"], "type": item["type"],
                    "datasource_id": item["datasource_id"]}
        if item["has_error"]:
            findings.append(finding("render.error", location, category=item["category"],
                                    flags=item["flags"], error=item["error"]))
        elif not item["settled"]:
            findings.append(finding("render.not_settled", location))
    return findings


class Audit:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def run(self, *, live: bool = True, live_timeout_seconds: int = 30,
                  page_ids: list[str] | None = None) -> dict[str, Any]:
        raw = await self.studio.report()
        config = raw["reportConfig"]
        sources = {s["id"]: await self.studio.datasource(s["id"])
                   for s in await self.studio.sources()}
        findings = audit_saved(config, sources)
        coverage: dict[str, Any] = {"saved": "all pages, filters, blends and attached datasources"}
        if live:
            if page_ids:
                unknown = set(page_ids) - {p["pageId"] for p in config["page"]}
                if unknown:
                    raise StudioError("NOT_FOUND", f"Unknown pages: {', '.join(sorted(unknown))}")
            coverage["live"] = await self._live(config, findings, live_timeout_seconds, page_ids)
        else:
            coverage["live"] = {"status": "skipped"}
        order = {"error": 0, "warning": 1, "info": 2}
        findings.sort(key=lambda f: (order[f["severity"]], f["rule"]))
        summary = {level: sum(f["severity"] == level for f in findings) for level in order}
        return {
            "report_id": self.studio.config.report_id,
            "healthy": summary["error"] == 0,
            "summary": summary,
            "findings": findings,
            "coverage": coverage,
            "rules": {rule: {"severity": s, "meaning": m} for rule, (s, m, _) in RULES.items()},
            "limits": "Live rendering runs as the MCP account; viewer-only failures are covered "
                      "by the saved rules (inline formulas, credentials, drafts). Sharing "
                      "permissions and query result values are not inspected.",
        }

    async def _live(self, config: dict, findings: list, wait_seconds: int,
                    page_ids: list[str] | None = None) -> dict[str, Any]:
        session = self.studio.session
        start = (await session.page()).url
        pages, components = [], 0
        try:
            chosen = [p for p in config["page"] if not page_ids or p["pageId"] in page_ids]
            for index, page in enumerate(chosen):
                try:
                    await self.studio.refresh(page["pageId"])
                except StudioError as exc:
                    return {"status": "unavailable", "reason": f"{exc.code}: {exc}",
                            "pages_checked": pages}
                result = await asyncio.wait_for(
                    (await session.page()).evaluate(_LIVE, {
                        "reportId": self.studio.config.report_id, "pageId": page["pageId"],
                        "includeReportLevel": index == 0, "timeout": wait_seconds * 1000}),
                    timeout=wait_seconds + session.config.timeout_ms / 1000)
                if result.get("error"):
                    return {"status": "unavailable", "reason": result["error"],
                            "pages_checked": pages}
                findings.extend(live_findings(page["pageId"], result["components"]))
                components += len(result["components"])
                pages.append(page["pageId"])
        finally:
            page_id = re.search(r"/page/([^/?#]+)", start)
            if page_id and pages:
                try:
                    await self.studio.refresh(page_id.group(1))
                except StudioError:
                    pass
        return {"status": "checked", "pages_checked": pages, "data_components_checked": components}
