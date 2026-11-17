"""Saved filter resources and their exact references across the report document."""

from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .browser import StudioError
from .document import fingerprint
from .report_resources import ReportResources, report_content
from .filter_conditions import FilterConditions, FilterNode

if TYPE_CHECKING:
    from .service import Studio


def pointer(path: tuple[str | int, ...]) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)


def filter_inventory(config: dict[str, Any], attached_source_ids: set[str]) -> dict[str, Any]:
    report = config.get("report")
    if not isinstance(report, dict) or not isinstance(config.get("page"), list):
        raise StudioError("INVALID_REPORT", "Full report and pages are required for filter references")
    resources = report.get("resource", {})
    if not isinstance(resources, dict):
        raise StudioError("INVALID_REPORT", "Malformed report resources")
    resource = resources.get("filter", {})
    if not isinstance(resource, dict) or not isinstance(resource.get("entry", []), list):
        raise StudioError("INVALID_REPORT", "Malformed saved filter resource")
    entries = resource.get("entry", [])
    by_id = {}
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("key"), str)
                or not entry["key"] or not isinstance(entry.get("value"), dict)):
            raise StudioError("INVALID_REPORT", "Malformed saved filter entry")
        if entry["key"] in by_id:
            raise StudioError("INVALID_REPORT", "Duplicate saved filter ID")
        by_id[entry["key"]] = entry
    references = {key: [] for key in by_id}
    dangling = []

    def visit(value: Any, path: tuple = (), owner: dict | None = None):
        # A registry key declares a filter; it is not a reference to itself.
        if (len(path) == 5 and path[:3] == ("report", "resource", "filter")
                and path[3] == "entry"):
            for key, item in value.items():
                if key != "key":
                    visit(item, path + (key,), {"filter_id": value.get("key")})
            return
        if isinstance(value, dict):
            context = dict(owner or {})
            for key in ("pageId", "componentId", "groupId"):
                if isinstance(value.get(key), str):
                    context[key] = value[key]
            for key, item in value.items():
                visit(item, path + (key,), context)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, path + (index,), owner)
        elif isinstance(value, str):
            in_filter_list = len(path) >= 2 and path[-2] == "filters" and isinstance(path[-1], int)
            in_blend_filter_list = (path[:3] == ("report", "resource", "dataViewResource")
                                    and path[-3:-1] == ("filterConfig", "filterIds")
                                    and isinstance(path[-1], int))
            match = {"path": pointer(path), "owner": owner or {},
                     "kind": "filter_list" if in_filter_list else (
                         "blend_filter_list" if in_blend_filter_list else "unclassified_exact_reference")}
            if value in references:
                references[value].append(match)
            elif in_filter_list or in_blend_filter_list:
                dangling.append({"filter_id": value, **match})

    visit(config)
    filters = []
    for filter_id, entry in by_id.items():
        source_id = entry["value"].get("datasourceId")
        filters.append({
            "id": filter_id, "definition": copy.deepcopy(entry["value"]),
            "fingerprint": fingerprint(entry),
            "datasource_attached": source_id in attached_source_ids,
            "references": references[filter_id],
            "has_no_exact_references": not references[filter_id],
        })
    return {"filters": filters, "resource_fingerprint": fingerprint(resource),
            "report_content_fingerprint": fingerprint(report),
            "dangling_filter_references": dangling,
            "reference_scope": "Exact string references across full saved report, pages and resources; "
                               "unclassified references are conservative, not proven applications. "
                               "No runtime inheritance or external consumers are inferred."}


class Filters:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def inventory(self) -> dict[str, Any]:
        config = (await self.studio.report())["reportConfig"]
        sources = {source["id"] for source in await self.studio.sources()}
        return {"report_id": self.studio.config.report_id, "report_version": config["version"],
                **filter_inventory(config, sources)}

    async def rename(self, changes: list[FilterRename], expected: str, *, dry_run: bool = True) -> dict:
        ids = [change.filter_id for change in changes]
        if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct saved filter IDs")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = filter_inventory(before, set())
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            known = {f["id"]: f for f in inventory["filters"]}
            if any(filter_id not in known for filter_id in ids):
                raise StudioError("NOT_FOUND", "Saved filter ID not found in this report")
            target = report_content(before)
            names = {change.filter_id: change.name for change in changes}
            for entry in target["resource"]["filter"]["entry"]:
                if entry["key"] in names:
                    entry["value"]["name"] = names[entry["key"]]
            plan = {"changes": [{"filter_id": change.filter_id,
                                "before": known[change.filter_id]["definition"].get("name"),
                                "after": change.name, "references": known[change.filter_id]["references"]}
                               for change in changes], "atomic_scope": "report_resources"}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await ReportResources(self.studio).write(before, target), **plan}

    async def delete(self, filter_ids: list[str], expected: str, *, dry_run: bool = True) -> dict:
        if (not 1 <= len(filter_ids) <= 100 or len(set(filter_ids)) != len(filter_ids)
                or any(not isinstance(filter_id, str) or not filter_id for filter_id in filter_ids)):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct saved filter IDs")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = filter_inventory(before, set())
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            known = {item["id"]: item for item in inventory["filters"]}
            if any(filter_id not in known for filter_id in filter_ids):
                raise StudioError("NOT_FOUND", "Saved filter ID not found in this report")
            if any(known[filter_id]["references"] for filter_id in filter_ids):
                raise StudioError(
                    "FILTER_IN_USE", "Saved filter has references; inspect filters_list before detaching"
                )
            target = report_content(before)
            selected = set(filter_ids)
            target["resource"]["filter"]["entry"] = [
                entry for entry in target["resource"]["filter"]["entry"] if entry["key"] not in selected
            ]
            plan = {"deleted_filter_ids": filter_ids,
                    "deleted_definitions": [known[filter_id]["definition"] for filter_id in filter_ids],
                    "atomic_scope": "report_resources",
                    "reference_scope": inventory["reference_scope"]}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await ReportResources(self.studio).write(before, target), **plan}

    async def create(self, filters: list[NewFilter], expected: str, *, dry_run: bool = True) -> dict:
        if not 1 <= len(filters) <= 100:
            raise StudioError("INVALID_BATCH", "Provide 1-100 saved filters")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = filter_inventory(before, set())
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            target = report_content(before)
            entries = target.setdefault("resource", {}).setdefault("filter", {}).setdefault("entry", [])
            existing_ids = {entry["key"] for entry in entries}
            created, sources = [], {}
            for spec in filters:
                validation = await FilterConditions(self.studio).validate(spec.datasource_id, spec.tree)
                if not validation["valid"]:
                    raise StudioError("INVALID_FILTER", "Filter/formula failed validation; use filter_validate for details")
                token = validation["source_fingerprint"]
                if spec.datasource_id in sources and sources[spec.datasource_id] != token:
                    raise StudioError("VERSION_CONFLICT", "Datasource changed within filter batch")
                sources[spec.datasource_id] = token
                while (filter_id := "filter" + uuid.uuid4().hex[:20]) in existing_ids:
                    pass
                existing_ids.add(filter_id)
                entry = {"key": filter_id, "value": {"name": spec.name, "datasourceId": spec.datasource_id,
                         "filterDefinition": validation["definition"], "version": 3}}
                created.append(entry)
                entries.append(entry)
            for source_id, token in sources.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError("VERSION_CONFLICT", "Datasource changed before filter creation")
            plan = {"created_filters": created, "created_filter_ids": [e["key"] for e in created],
                    "atomic_scope": "report_resources", "source_guard": "Preflight, not cross-entity CAS"}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await ReportResources(self.studio).write(before, target), **plan}

    async def update_conditions(
        self, changes: list[FilterConditionChange], expected: str, *, dry_run: bool = True
    ) -> dict:
        ids = [change.filter_id for change in changes]
        if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct saved filter IDs")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = filter_inventory(before, set())
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            known = {item["id"]: item for item in inventory["filters"]}
            if any(filter_id not in known for filter_id in ids):
                raise StudioError("NOT_FOUND", "Saved filter ID not found in this report")
            target = report_content(before)
            entries = {entry["key"]: entry for entry in target["resource"]["filter"]["entry"]}
            plans, sources = [], {}
            for change in changes:
                item = known[change.filter_id]
                source_id = item["definition"].get("datasourceId")
                if not isinstance(source_id, str) or not source_id:
                    raise StudioError("INVALID_FILTER", "Saved filter has no datasource ID")
                validation = await FilterConditions(self.studio).validate(source_id, change.tree)
                if not validation["valid"]:
                    raise StudioError(
                        "INVALID_FILTER", "Filter/formula failed validation; use filter_validate for details"
                    )
                token = validation["source_fingerprint"]
                if source_id in sources and sources[source_id] != token:
                    raise StudioError("VERSION_CONFLICT", "Datasource changed within filter batch")
                sources[source_id] = token
                # The requested tree is a full replacement. Everything outside that
                # tree and its format version (including entry metadata) survives.
                value = entries[change.filter_id]["value"]
                value["filterDefinition"] = validation["definition"]
                value["version"] = validation["filter_version"]
                plans.append({"filter_id": change.filter_id, "before": item["definition"],
                              "after": copy.deepcopy(value), "references": item["references"]})
            for source_id, token in sources.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError("VERSION_CONFLICT", "Datasource changed before filter update")
            plan = {"changes": plans, "atomic_scope": "report_resources",
                    "source_guard": "Preflight, not cross-entity CAS",
                    "consumer_scope": "Existing bindings preserved; changed conditions affect their consumers. "
                                      "Per-consumer query behavior is not inferred from source validation."}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await ReportResources(self.studio).write(before, target), **plan}


class FilterRename(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filter_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=1000)


class NewFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=1000)
    datasource_id: str = Field(min_length=1)
    tree: FilterNode


class FilterConditionChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filter_id: str = Field(min_length=1)
    tree: FilterNode
