"""Make report-level / page-level, and copy components to another page (cross-page paste).

Report-level components render on every page. A move is two journalled writes, never a
transaction: the target entity gains the component first, then the source loses it, so
a failure in between leaves the component visible twice (reported as partial), never lost.
"""

from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Any, Literal

from .browser import StudioError
from .document import find_page, json_equal
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio


def _take(components: list[dict], ids: list[str]) -> tuple[list[dict], list[dict]]:
    by_id = {c.get("componentId"): c for c in components}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise StudioError("NOT_FOUND", f"Not top-level here: {', '.join(missing)}")
    return [copy.deepcopy(by_id[i]) for i in ids], [
        c for c in components if c.get("componentId") not in ids]


class Scope:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def move(
        self, page_id: str, component_ids: list[str], to: Literal["report", "page"],
        *, dry_run: bool = True,
    ) -> dict[str, Any]:
        if not 1 <= len(component_ids) <= 100 or len(set(component_ids)) != len(component_ids):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct component IDs")
        async with self.studio.mutation_lock():
            raw = await self.studio.report()
            config = raw["reportConfig"]
            frame = find_page(raw, page_id)
            report = report_content(config)
            page = frame["page"]
            grouped = {i for g in page.get("groupConfig", []) for i in g.get("componentId", [])}
            if to == "report" and grouped.intersection(component_ids):
                raise StudioError("GROUPED_COMPONENT", "Ungroup the components first")
            conditions = config.get("chartCondition", []) + frame.get("chartCondition", [])
            if any(c.get("chartId") in component_ids for c in conditions):
                raise StudioError(
                    "CONDITIONAL_FORMATTING_MOVE_UNSUPPORTED",
                    "Components with conditional formatting rules cannot change scope",
                )
            new_report, new_page = copy.deepcopy(report), copy.deepcopy(page)
            if to == "report":
                moved, new_page["componentConfig"] = _take(page.get("componentConfig", []),
                                                           component_ids)
                new_report.setdefault("componentConfig", []).extend(moved)
            else:
                moved, new_report["componentConfig"] = _take(report.get("componentConfig", []),
                                                             component_ids)
                new_page.setdefault("componentConfig", []).extend(moved)
            plan = {"page_id": page_id, "to": to, "component_ids": component_ids}
            if dry_run:
                return {"status": "planned", **plan}
            if to == "report":
                first = await ReportResources(self.studio).write(config, new_report, settings=True)
                steps = {"report": first}
                if first["status"] != "verified":
                    return {"status": first["status"], "steps": steps, **plan}
                fresh = find_page(await self.studio.report(), page_id)
                if not json_equal(fresh["page"], page):
                    return {"status": "partial", "steps": steps, "error": "VERSION_CONFLICT",
                            "note": "The page changed; remove the page copies yourself", **plan}
                steps["page"] = await self.studio._write_page(fresh, new_page)
            else:
                first = await self.studio._write_page(frame, new_page)
                steps = {"page": first}
                if first["status"] != "verified":
                    return {"status": first["status"], "steps": steps, **plan}
                fresh = (await self.studio.report())["reportConfig"]
                if not json_equal(report_content(fresh), report):
                    return {"status": "partial", "steps": steps, "error": "VERSION_CONFLICT",
                            "note": "The report changed; remove the report copies yourself",
                            **plan}
                steps["report"] = await ReportResources(self.studio).write(
                    fresh, new_report, settings=True)
            second = steps["page" if to == "report" else "report"]
            status = "verified" if second["status"] == "verified" else "partial"
            return {"status": status, "steps": steps, **plan}

    async def copy_to_page(
        self, source_page_id: str, component_ids: list[str], target_page_id: str,
        *, dry_run: bool = True,
    ) -> dict[str, Any]:
        """Copy ungrouped top-level components (same position, settings and fields) to
        another page with new IDs, as copy + paste does. Conditional formatting rules
        are not copied. One journalled write of the target page."""
        if source_page_id == target_page_id:
            raise StudioError("SAME_PAGE", "Use components_duplicate on the same page")
        if not 1 <= len(component_ids) <= 100 or len(set(component_ids)) != len(component_ids):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct component IDs")
        async with self.studio.mutation_lock():
            raw = await self.studio.report()
            source = find_page(raw, source_page_id)
            target = find_page(raw, target_page_id)
            grouped = {i for g in source["page"].get("groupConfig", [])
                       for i in g.get("componentId", [])}
            if grouped.intersection(component_ids):
                raise StudioError("GROUPED_COMPONENT", "Ungroup the components first")
            copies, _ = _take(source["page"].get("componentConfig", []), component_ids)
            mapping = {}
            for component in copies:
                new_id = "cd-" + uuid.uuid4().hex[:20]
                mapping[component["componentId"]] = new_id
                component["componentId"] = new_id
                for child in component.get("componentConfig", []):
                    child["componentId"] = "cd-" + uuid.uuid4().hex[:20]
            after = copy.deepcopy(target["page"])
            after.setdefault("componentConfig", []).extend(copies)
            plan = {"source_page_id": source_page_id, "target_page_id": target_page_id,
                    "created_component_ids": mapping}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await self.studio._write_page(target, after), **plan}
