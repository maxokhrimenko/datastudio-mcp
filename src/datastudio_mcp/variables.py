"""Resource > Manage variables (report variables) and Control visibility.

A report variable is one `unifiedParameterResource` entry: {"reportParameterConfig":
{"parameterKey", "parameter": {"parametersModifiedByRoles": [{"key": role, "value": {
dataType, lock, validation, name, options, defaultValue, parameterQueryConfig?}}],
"parameterDisplayConfiguration": {displayName, displayType, name, ...}}}}. The editor writes
the same value under roles 1 and 5; a query variable adds parameterQueryConfig (fields,
dataset, sort, singleCellOption {selectedColumn, selectedRow}).

Control visibility binds a component or page to a variable: attribute
`visibilityParameterSourceKey` = "REPORT_VARIABLE:<parameterKey>" (or
"DATASOURCE_VARIABLE:<datasourceId>:<parameterKey>"); a false value hides it from viewers.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .browser import StudioError
from .document import find_component, find_page, fingerprint, json_equal
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio


class ReportVariable(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    parameter_key: str = Field(pattern=r"^[A-Za-z0-9_]{1,100}$")
    parameter: dict[str, Any] = Field(description="Wire parameter (see module docstring)")


def _entries(content: dict[str, Any]) -> list[dict[str, Any]]:
    return content.get("resource", {}).get("unifiedParameterResource", [])


def _key(entry: dict[str, Any]) -> str | None:
    return (entry.get("reportParameterConfig") or {}).get("parameterKey")


class Variables:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def list(self) -> dict[str, Any]:
        content = report_content((await self.studio.report())["reportConfig"])
        return {"fingerprint": fingerprint(content), "variables": [
            {"parameter_key": _key(e),
             "display_name": e["reportParameterConfig"]["parameter"].get(
                 "parameterDisplayConfiguration", {}).get("displayName"),
             "parameter": e["reportParameterConfig"]["parameter"]}
            for e in _entries(content)]}

    async def write(
        self, upserts: list[ReportVariable], deletes: list[str], expected: str,
        *, dry_run: bool = True,
    ) -> dict[str, Any]:
        if not upserts and not deletes:
            raise StudioError("INVALID_BATCH", "Nothing to change")
        async with self.studio.mutation_lock():
            raw = (await self.studio.report())["reportConfig"]
            before = report_content(raw)
            if fingerprint(before) != expected:
                raise StudioError("VERSION_CONFLICT", "Report changed; list variables again")
            after = copy.deepcopy(before)
            entries = after.setdefault("resource", {}).setdefault("unifiedParameterResource", [])
            known = {_key(e) for e in entries}
            missing = set(deletes) - known
            if missing:
                raise StudioError("NOT_FOUND", f"Unknown variables: {', '.join(sorted(missing))}")
            if deletes:
                text = str({k: v for k, v in raw.items() if k != "report"}) + str(
                    {k: v for k, v in raw["report"].items() if k != "resource"})
                used = [key for key in deletes if f"REPORT_VARIABLE:{key}" in text]
                if used:
                    raise StudioError("VARIABLE_IN_USE", f"Still bound: {', '.join(used)}")
            entries[:] = [e for e in entries if _key(e) not in deletes]
            for variable in upserts:
                entry = {"reportParameterConfig": {"parameterKey": variable.parameter_key,
                                                   "parameter": variable.parameter}}
                index = next((i for i, e in enumerate(entries)
                              if _key(e) == variable.parameter_key), None)
                if index is None:
                    entries.append(entry)
                else:
                    entries[index] = entry
            if json_equal(before, after):
                return {"status": "unchanged"}
            if dry_run:
                return {"status": "planned", "after": _entries(after)}
            return await ReportResources(self.studio).write(raw, after)

    async def visibility(
        self, page_id: str, component_id: str | None, source_key: str | None,
        expected: str, *, dry_run: bool = True,
    ) -> dict[str, Any]:
        async with self.studio.mutation_lock():
            raw = await self.studio.report()
            frame = find_page(raw, page_id)
            if fingerprint(frame["page"]) != expected:
                raise StudioError("VERSION_CONFLICT", "Page changed; read it again")
            if source_key is not None:
                kind, _, rest = source_key.partition(":")
                content = report_content(raw["reportConfig"])
                if kind == "REPORT_VARIABLE" and rest not in {_key(e) for e in _entries(content)}:
                    raise StudioError("NOT_FOUND", "Report variable does not exist")
                if kind not in ("REPORT_VARIABLE", "DATASOURCE_VARIABLE") or not rest:
                    raise StudioError("INVALID_SOURCE_KEY",
                                      "Use REPORT_VARIABLE:<key> or DATASOURCE_VARIABLE:<ds>:<key>")
            after = copy.deepcopy(frame["page"])
            owner, kind = ((after, "pageAttribute") if component_id is None
                           else (find_component(after, component_id), "componentAttribute"))
            current = owner.get("attributeConfig", {}).get(kind, {})
            if current.get("visibilityParameterSourceKey") == source_key:
                return {"status": "unchanged"}
            attributes = owner.setdefault("attributeConfig", {}).setdefault(kind, {})
            if source_key is None:
                attributes.pop("visibilityParameterSourceKey", None)
            else:
                attributes["visibilityParameterSourceKey"] = source_key
            if json_equal(after, frame["page"]):
                return {"status": "unchanged"}
            if dry_run:
                return {"status": "planned", "attributes": attributes}
            return await self.studio._write_page(frame, after)
