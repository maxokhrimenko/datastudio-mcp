"""Community visualizations (Insert > Community visualizations, Resource > Manage
community visualizations).

A report lists the visualizations it uses in `communityVizResource` ([{vizId, value:
[{}]}], as the editor stores it). A component of that type has `type` = the viz ID
(manifest path, a non-breaking space, component ID), e.g.
"google://devrel/googlecharts\\u00a0gauge". components_create loads the viz registration
and registers its type in the editor (as the Insert gallery does) before creating it.
"""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any

from .browser import StudioError
from .document import fingerprint, json_equal, walk_components
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio

VIZ_ID = re.compile(r"^(?:gs|google)://[^\s ]+(?: [A-Za-z0-9_.-]+)?$")


def _entries(content: dict) -> list[dict]:
    return content.get("resource", {}).get("communityVizResource", [])


def _users(config: dict, viz_id: str) -> list[str]:
    """Components of this viz anywhere in the report (pages, groups, report level). An
    entry without a component part covers every component of its manifest."""
    roots = [config["report"].get("componentConfig", [])] + [
        frame["page"].get("componentConfig", []) for frame in config["page"]]
    return [c["componentId"] for root in roots for c in walk_components(root)
            if c.get("type") == viz_id or c.get("type", "").startswith(viz_id + "\u00a0")]


class CommunityViz:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def list(self) -> dict[str, Any]:
        config = (await self.studio.report())["reportConfig"]
        content = report_content(config)
        return {"fingerprint": fingerprint(content), "visualizations": [
            {"viz_id": e.get("vizId"), "components": _users(config, e.get("vizId", ""))}
            for e in _entries(content)]}

    async def write(self, viz_id: str, register: bool, expected: str,
                    *, dry_run: bool = True) -> dict[str, Any]:
        if not VIZ_ID.fullmatch(viz_id):
            raise StudioError("INVALID_VIZ_ID", "gs://... or google://... manifest path, "
                              "optionally + U+00A0 + component ID")
        async with self.studio.mutation_lock():
            raw = await self.studio.report()
            before = report_content(raw["reportConfig"])
            if fingerprint(before) != expected:
                raise StudioError("VERSION_CONFLICT", "Report changed; list again")
            after = copy.deepcopy(before)
            entries = after.setdefault("resource", {}).setdefault("communityVizResource", [])
            present = any(e.get("vizId") == viz_id for e in entries)
            if register and not present:
                entries.append({"vizId": viz_id, "value": [{}]})
            if not register and present:
                used = _users(raw["reportConfig"], viz_id)
                if used:
                    raise StudioError("VIZ_IN_USE", f"Delete its components first: {used}")
                entries[:] = [e for e in entries if e.get("vizId") != viz_id]
            if json_equal(before, after):
                return {"status": "unchanged", "viz_id": viz_id}
            if dry_run:
                return {"status": "planned", "viz_id": viz_id, "after": entries}
            return {**await ReportResources(self.studio).write(raw["reportConfig"], after),
                    "viz_id": viz_id}
