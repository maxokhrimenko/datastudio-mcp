"""Page-atomic group filter binding batches, preserving hierarchy and controls."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from pydantic import Field

from .browser import StudioError
from .component_filters import FilterBinding
from .document import find_page, fingerprint, json_equal
from .filters import filter_inventory
from .page_filters import PageFilters

if TYPE_CHECKING:
    from .service import Studio


class GroupFilterChange(FilterBinding):
    group_id: str = Field(min_length=1)


class GroupFilters:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def patch(
        self,
        page_id: str,
        changes: list[GroupFilterChange],
        expected_page: str,
        expected_report: str,
        *,
        dry_run: bool = True,
    ) -> dict:
        ids = [change.group_id for change in changes]
        if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct group IDs")
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
            groups = target.get("groupConfig", [])
            by_id = {group["groupId"]: group for group in groups}
            if len(by_id) != len(groups):
                raise StudioError("INVALID_PAGE", "Duplicate group IDs")
            if any(group_id not in by_id for group_id in ids):
                raise StudioError("NOT_FOUND", "Group does not belong to this page")
            known = {f["id"]: f["definition"] for f in inventory["filters"]}
            plans, sources = [], {}
            for change in changes:
                group = by_id[change.group_id]
                props = group.get("propertyConfig", {}).get("groupProperty")
                if not isinstance(props, dict):
                    raise StudioError("INVALID_GROUP", "Group properties missing")
                old_ids, inherit = props.get("filters", []), props.get("inheritFilters")
                if (
                    not isinstance(old_ids, list)
                    or any(not isinstance(f, str) for f in old_ids)
                    or type(inherit) is not bool
                ):
                    raise StudioError("FILTER_CONTRACT_CHANGED", "Malformed group filter binding")
                new_ids = old_ids if change.filter_ids is None else change.filter_ids
                if any(filter_id not in known for filter_id in new_ids):
                    raise StudioError("NOT_FOUND", "Saved filter ID not found in report")
                filters = []
                for filter_id in new_ids:
                    definition = known[filter_id]
                    source_id = definition.get("datasourceId")
                    if not isinstance(source_id, str) or not source_id:
                        raise StudioError("INVALID_FILTER", "Filter datasource ID missing")
                    if source_id not in sources:
                        source = await self.studio.datasource(source_id)
                        if not json_equal(source["draft"], source["published"]):
                            raise StudioError(
                                "UNPUBLISHED_DATASOURCE", "Matching draft/published required"
                            )
                        sources[source_id] = fingerprint(source)
                    filters.append(
                        {"id": filter_id, "sourceId": source_id, "definition": definition}
                    )
                validation = await PageFilters(self.studio)._validate(
                    frame, old_ids, inherit, filters, group_id=change.group_id
                )
                if change.filter_ids is not None:
                    props["filters"] = list(change.filter_ids)
                if change.inherit_filters is not None:
                    props["inheritFilters"] = change.inherit_filters
                plans.append(
                    {
                        "group_id": change.group_id,
                        "before": {"filter_ids": old_ids, "inherit_filters": inherit},
                        "after": {
                            "filter_ids": props.get("filters", []),
                            "inherit_filters": props["inheritFilters"],
                        },
                        "component_ids": group.get("componentId", []),
                        "child_group_ids": group.get("childGroupId", []),
                        "validation": validation,
                    }
                )
            for source_id, token in sources.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError(
                        "VERSION_CONFLICT", "Filter datasource changed during group batch"
                    )
            fresh = await self.studio.report()
            if fingerprint(fresh["reportConfig"]["report"]) != expected_report or not json_equal(
                find_page(fresh, page_id), frame
            ):
                raise StudioError("VERSION_CONFLICT", "Page/report changed during group batch")
            plan = {
                "page_id": page_id,
                "changes": plans,
                "atomic_scope": "page",
                "source_guard": "Preflight, not cross-entity CAS",
                "scope": "Group-local filter bindings only; membership, controlStatePassThrough, "
                "layout and child inheritance settings are preserved",
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
