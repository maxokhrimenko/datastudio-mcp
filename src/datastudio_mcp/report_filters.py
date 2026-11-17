"""Report-level saved filter IDs; no parent inheritance exists at this scope."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .browser import StudioError
from .document import fingerprint, json_equal
from .filters import filter_inventory
from .page_filters import PageFilters
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio


class ReportFilters:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def patch(self, filter_ids: list[str], expected: str, *, dry_run: bool = True) -> dict:
        if (
            len(filter_ids) > 100
            or len(set(filter_ids)) != len(filter_ids)
            or any(not isinstance(item, str) or not item for item in filter_ids)
        ):
            raise StudioError("INVALID_BATCH", "Provide up to 100 distinct nonempty filter IDs")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = filter_inventory(before, set())
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report/filter resources changed")
            target = report_content(before)
            props = target.get("propertyConfig", {}).get("reportProperty")
            if not isinstance(props, dict):
                raise StudioError("INVALID_REPORT", "Report properties are missing")
            old_ids = props.get("filters", [])
            if not isinstance(old_ids, list) or any(not isinstance(f, str) for f in old_ids):
                raise StudioError("FILTER_CONTRACT_CHANGED", "Malformed report filter IDs")
            known = {item["id"]: item["definition"] for item in inventory["filters"]}
            if any(item not in known for item in filter_ids):
                raise StudioError("NOT_FOUND", "Saved filter ID not found in report")
            sources, filters = {}, []
            for filter_id in filter_ids:
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
            native = await PageFilters(self.studio)._validate(
                {"pageId": None, "version": before["version"]}, old_ids, False, filters
            )
            for source_id, token in sources.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError("VERSION_CONFLICT", "Filter datasource changed")
            if not json_equal((await self.studio.report())["reportConfig"], before):
                raise StudioError("VERSION_CONFLICT", "Report/pages changed during preparation")
            props["filters"] = list(filter_ids)
            plan = {
                "before_filter_ids": old_ids,
                "after_filter_ids": filter_ids,
                "validation": native,
                "atomic_scope": "report",
                "source_guard": "Preflight, not cross-entity CAS",
                "scope": "Report-level binding affects eligible inheriting consumers on all pages; "
                "child settings are preserved, effective inheritance is not inferred",
            }
            if dry_run:
                return {"status": "planned", **plan}
            return {
                **await ReportResources(self.studio).write(
                    before, target, filter_bindings_only=True
                ),
                **plan,
            }
