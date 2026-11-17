"""Wire-level edits of report content, for anything without a typed tool.

Typed tools (settings_set, components_*, filters_*, ...) validate through the editor;
these accept RFC 6902 operations on the saved JSON itself, guarded by its fingerprint,
written once with full before-state, read-back and operation_restore. Identity fields
are immutable here: pages, components and datasources have their own lifecycle tools.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .browser import StudioError
from .document import Patch, apply_patches, fingerprint, json_equal
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio


class DimensionColor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str = Field(min_length=1, max_length=1000, description="Dimension value")
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    opacity: float = Field(default=1, ge=0, le=1)


class Patches:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def read(self) -> dict[str, Any]:
        raw = (await self.studio.report())["reportConfig"]
        content = report_content(raw)
        return {"report_id": self.studio.config.report_id, "name": raw["shareable"]["name"],
                "content": content, "fingerprint": fingerprint(content)}

    async def report(
        self, patches: list[Patch], expected_fingerprint: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        if not 1 <= len(patches) <= 200:
            raise StudioError("INVALID_BATCH", "Provide 1-200 operations")
        async with self.studio.mutation_lock():
            raw = (await self.studio.report())["reportConfig"]
            before = report_content(raw)
            if fingerprint(before) != expected_fingerprint:
                raise StudioError("VERSION_CONFLICT", "Report content changed; read it again")
            after = apply_patches(before, patches)
            ids = sorted(c.get("componentId") for c in before.get("componentConfig", []))
            if ids != sorted(c.get("componentId") for c in after.get("componentConfig", [])):
                raise StudioError("IMMUTABLE_ID", "Report-level membership has its own tools")
            return await self._report_write(raw, before, after, dry_run=dry_run)

    async def _report_write(self, raw, before, after, *, dry_run):
        if json_equal(before, after):
            return {"status": "unchanged", "fingerprint": fingerprint(before)}
        if dry_run:
            return {"status": "planned", "before": before, "after": after}
        return await ReportResources(self.studio).write(raw, after, settings=True)

    async def rename(self, name: str, *, dry_run: bool = True) -> dict[str, Any]:
        """The report title (header name field)."""
        if not 1 <= len(name.strip()) <= 1000:
            raise StudioError("INVALID_NAME", "Name must be 1-1000 characters")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]["shareable"]["name"]
            if before == name:
                return {"status": "unchanged", "name": name}
            if dry_run:
                return {"status": "planned", "before": before, "after": name}
            record = self.studio.journal_action("report_rename", before=before, intended=name)
            await self.studio.session.post(
                "updateReport", {"reportId": self.studio.config.report_id, "reportName": name})
            after = (await self.studio.report())["reportConfig"]["shareable"]["name"]
            record.update(state="verified" if after == name else "verification_failed",
                          after=after)
            self.studio._journal(record["operation_id"], record)
            return {"status": record["state"], "operation_id": record["operation_id"],
                    "before": before, "after": after}

    async def dimension_colors(
        self, colors: list[DimensionColor], *, replace: bool = False, dry_run: bool = True
    ) -> dict[str, Any]:
        """Resource > Manage dimension value colors: value -> color for every chart."""
        if len({c.value for c in colors}) != len(colors):
            raise StudioError("INVALID_BATCH", "Dimension values must be unique")
        async with self.studio.mutation_lock():
            raw = (await self.studio.report())["reportConfig"]
            before = report_content(raw)
            after = copy.deepcopy(before)
            resource = after.setdefault("resource", {})
            config = resource.setdefault("colorMappingList", {}).setdefault(
                "colorMappingConfig", [])
            if not config:
                # The server completes a new mapping set with its collection index.
                config.append({"colorMapping": [], "colorCollectionIndex": []})
            current = [] if replace else config[0].get("colorMapping", [])
            wanted = {c.value: c for c in colors}
            merged = [m for m in current if m.get("key", {}).get("value") not in wanted]
            merged += [{"key": {"value": c.value}, "color": c.color,
                        "colorType": {"color": c.color, "opacity": c.opacity}}
                       for c in colors]
            if len(merged) > 1500:
                raise StudioError("TOO_MANY_VALUES", "Data Studio keeps at most 1500 values")
            config[0]["colorMapping"] = merged
            if dry_run:
                # Only the values that change: the whole report content is ~100K characters.
                was = {m.get("key", {}).get("value"): m.get("color")
                       for m in (before.get("resource", {}).get("colorMappingList", {})
                                 .get("colorMappingConfig") or [{}])[0].get("colorMapping", [])}
                now = {m["key"]["value"]: m.get("color") for m in merged}
                changes = [{"value": v, "before": was.get(v), "after": now.get(v)}
                           for v in dict.fromkeys([*was, *now]) if was.get(v) != now.get(v)]
                if not changes:
                    return {"status": "unchanged", "fingerprint": fingerprint(before)}
                return {"status": "planned", "changes": changes, "values_after": len(merged)}
            return await self._report_write(raw, before, after, dry_run=dry_run)
