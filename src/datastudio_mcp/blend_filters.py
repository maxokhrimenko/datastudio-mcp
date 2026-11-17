"""Guarded saved-filter bindings on individual report-local blend inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .blends import blend_inventory
from .browser import StudioError
from .document import fingerprint, json_equal, tokens
from .filters import filter_inventory
from .page_filters import PageFilters
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio


class BlendInputFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    blend_id: str = Field(min_length=1)
    input_path: str = Field(min_length=1)
    filter_ids: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.filter_ids)) != len(self.filter_ids) or any(not f for f in self.filter_ids):
            raise ValueError("Filter IDs must be distinct and nonempty")
        return self


class BlendFilters:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def patch(
        self, changes: list[BlendInputFilters], expected: str, *, dry_run: bool = True
    ) -> dict:
        keys = [(c.blend_id, c.input_path) for c in changes]
        if not 1 <= len(keys) <= 100 or len(set(keys)) != len(keys):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct blend inputs")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = blend_inventory(before, set())
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            views = {b["id"]: b for b in inventory["blends"]}
            known = {f["id"]: f["definition"] for f in filter_inventory(before, set())["filters"]}
            target = report_content(before)
            entries = {e["key"]: e["value"] for e in
                       target.get("resource", {}).get("dataViewResource", {}).get("entry", [])}
            plans, sources, validations = [], {}, {}
            for change in changes:
                view = views.get(change.blend_id)
                if view is None:
                    raise StudioError("NOT_FOUND", "Data-view resource not found")
                inputs = {q["path"]: q for q in view["inputs"]}
                if change.input_path not in inputs:
                    raise StudioError("INVALID_BLEND_INPUT", "Use an exact input path from blends_list")
                query = entries[change.blend_id]
                for token in tokens(change.input_path):
                    query = query[int(token)] if isinstance(query, list) else query[token]
                config = query.get("filterConfig", {})
                old_ids = config.get("filterIds", []) if isinstance(config, dict) else None
                if not isinstance(old_ids, list) or any(not isinstance(f, str) for f in old_ids):
                    raise StudioError("INVALID_BLEND_INPUT", "Malformed input filter configuration")
                source_id = inputs[change.input_path]["datasource_id"]
                for filter_id in change.filter_ids:
                    if filter_id not in known:
                        raise StudioError("NOT_FOUND", "Saved filter ID not found")
                    definition = known[filter_id]
                    if not source_id or definition.get("datasourceId") != source_id:
                        raise StudioError("FILTER_SOURCE_MISMATCH", "Filter must belong to the input datasource")
                    if source_id not in sources:
                        source = await self.studio.datasource(source_id)
                        if not json_equal(source["draft"], source["published"]):
                            raise StudioError("UNPUBLISHED_DATASOURCE", "Matching draft/published required")
                        sources[source_id] = fingerprint(source)
                    validations[filter_id] = {"id": filter_id, "sourceId": source_id,
                                              "definition": definition}
                plans.append({"blend_id": change.blend_id, "input_path": change.input_path,
                              "before_filter_ids": old_ids, "after_filter_ids": change.filter_ids,
                              "references": view["references"]})
                # Clearing an absent binding is a no-op, not a new empty config.
                if old_ids != change.filter_ids:
                    query.setdefault("filterConfig", {})["filterIds"] = list(change.filter_ids)
            props = before["report"].get("propertyConfig", {}).get("reportProperty", {})
            native = await PageFilters(self.studio)._validate(
                {"pageId": None, "version": before["version"]},
                props.get("filters", []), False, list(validations.values()),
            )
            for source_id, token in sources.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError("VERSION_CONFLICT", "Blend input datasource changed")
            if not json_equal((await self.studio.report())["reportConfig"], before):
                raise StudioError("VERSION_CONFLICT", "Report/pages changed during preparation")
            plan = {"changes": plans, "validation": native, "atomic_scope": "report_resources",
                    "source_guard": "Preflight, not cross-entity CAS",
                    "scope": "Saved input filter lists; native validation on each filter source schema. "
                             "Not proof of blend execution or post-join filtering."}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await ReportResources(self.studio).write(before, target), **plan}
