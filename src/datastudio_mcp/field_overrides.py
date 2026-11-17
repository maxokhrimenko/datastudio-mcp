"""Resource > Manage field names and IDs (report field overrides).

Each override is one `fieldOverridesResource` entry, exactly as the dialog saves it:
{displayNameOverride, idOverride, originalId, originalName, datasourceId}. A shared ID
override lets one filter apply across datasources whose fields carry the same ID.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .browser import StudioError
from .document import fingerprint, json_equal
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio


class FieldOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    datasource_id: str = Field(min_length=1)
    field: str = Field(min_length=1, description="Source field name (original ID)")
    display_name: str | None = Field(default=None, min_length=1, max_length=1000,
                                     description="Omit to keep the current value")
    id_override: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,200}$")

    @model_validator(mode="after")
    def check(self) -> FieldOverride:
        if self.display_name is None and self.id_override is None:
            raise ValueError("Set display_name, id_override or both")
        return self


class FieldRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    datasource_id: str = Field(min_length=1)
    field: str = Field(min_length=1)


def _key(entry: dict) -> tuple[str, str]:
    return entry.get("datasourceId", ""), entry.get("originalId", "")


class FieldOverrides:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def list(self) -> dict[str, Any]:
        content = report_content((await self.studio.report())["reportConfig"])
        return {"fingerprint": fingerprint(content),
                "overrides": content.get("resource", {}).get("fieldOverridesResource", [])}

    async def write(self, upserts: list[FieldOverride], deletes: list[FieldRef], expected: str,
                    *, dry_run: bool = True) -> dict[str, Any]:
        keys = [(u.datasource_id, u.field) for u in upserts] + [
            (d.datasource_id, d.field) for d in deletes]
        if not keys or len(set(keys)) != len(keys):
            raise StudioError("INVALID_BATCH", "Provide distinct fields to change")
        async with self.studio.mutation_lock():
            raw = (await self.studio.report())["reportConfig"]
            before = report_content(raw)
            if fingerprint(before) != expected:
                raise StudioError("VERSION_CONFLICT", "Report changed; list overrides again")
            after = copy.deepcopy(before)
            entries = after.setdefault("resource", {}).setdefault("fieldOverridesResource", [])
            known = {_key(e) for e in entries}
            missing = [f"{d.datasource_id}:{d.field}" for d in deletes
                       if (d.datasource_id, d.field) not in known]
            if missing:
                raise StudioError("NOT_FOUND", f"No override for {', '.join(missing)}")
            attached = {s["id"] for s in await self.studio.sources()}
            fields: dict[str, dict] = {}
            for change in upserts:
                if change.datasource_id not in attached:
                    raise StudioError("DATASOURCE_NOT_ATTACHED", change.datasource_id)
                if change.datasource_id not in fields:
                    source = await self.studio.datasource(change.datasource_id)
                    fields[change.datasource_id] = {
                        f["field"]["name"]: f for f in source["published"][
                            "datasourceBlock"]["fields"]}
            removed = {(d.datasource_id, d.field) for d in deletes}
            entries[:] = [e for e in entries if _key(e) not in removed]
            for change in upserts:
                source_field = fields[change.datasource_id].get(change.field)
                if source_field is None:
                    raise StudioError("FIELD_NOT_FOUND", change.field)
                index = next((i for i, e in enumerate(entries)
                              if _key(e) == (change.datasource_id, change.field)), None)
                # Partial update: unnamed fields of an existing override are kept.
                entry = copy.deepcopy(entries[index]) if index is not None else {
                    "originalId": change.field,
                    "originalName": source_field.get("outputName", change.field),
                    "datasourceId": change.datasource_id}
                if change.display_name is not None:
                    entry["displayNameOverride"] = change.display_name
                if change.id_override is not None:
                    entry["idOverride"] = change.id_override
                if index is None:
                    entries.append(entry)
                else:
                    entries[index] = entry
            for source_id in {e.get("datasourceId") for e in entries}:
                ids = [e.get("idOverride") or e.get("originalId") for e in entries
                       if e.get("datasourceId") == source_id]
                taken = set(fields.get(source_id, {})) - {
                    e.get("originalId") for e in entries if e.get("datasourceId") == source_id}
                if len(set(ids)) != len(ids) or taken.intersection(ids):
                    raise StudioError("DUPLICATE_FIELD_ID",
                                      f"Two fields of {source_id} would share one ID")
            if json_equal(before, after):
                return {"status": "unchanged"}
            if dry_run:
                return {"status": "planned", "after": entries}
            return await ReportResources(self.studio).write(raw, after)
