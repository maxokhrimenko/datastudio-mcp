"""Journaled datasource edits, in a report or on their own; never asset lifecycle."""

from __future__ import annotations

import copy
import os
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .browser import StudioError
from .document import fingerprint, json_equal
from .field_models import Aggregation
from .formulas import Formulas, dependency_summary

if TYPE_CHECKING:
    from .service import Studio


class FieldRename(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field_name: str = Field(min_length=1)
    output_name: str = Field(min_length=1)


class CalculatedField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    output_name: str = Field(min_length=1)
    expression: str = Field(min_length=1, max_length=100_000)
    aggregation: Aggregation


_CALC_ID = re.compile(r"calc_[A-Za-z0-9_]+")


def proposed_cycle(fields: list[dict], replacements: dict[str, str]) -> None:
    """Refuse edits that would make calculated fields reference each other in a loop.

    Edges are every calc_ id a formula mentions, string literals included: an
    over-approximation that can only refuse, never miss a cycle."""
    graph = {}
    for field in fields:
        name = field.get("field", {}).get("name", "")
        if _CALC_ID.fullmatch(name):
            graph[name] = replacements.get(name, field.get("textFormula") or "")
    graph = {name: sorted({ref for ref in _CALC_ID.findall(text) if ref in graph})
             for name, text in graph.items()}
    cycle = dependency_summary(graph)["cycle"]
    if cycle:
        raise StudioError("FORMULA_CYCLE", f"Calculated fields would reference each other: {cycle}")


class CalculatedFieldEdit(CalculatedField):
    field_name: str = Field(pattern=r"^calc_[A-Za-z0-9_]+$")


class FieldMetadataEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field_name: str = Field(min_length=1)
    output_name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    data_type: int | None = Field(default=None, ge=0, le=100)
    semantic_config: list[dict[str, Any]] | None = Field(
        default=None, min_length=1, max_length=20
    )
    default_aggregation: Aggregation | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def check(self) -> FieldMetadataEdit:
        editable = {
            "output_name",
            "description",
            "data_type",
            "semantic_config",
            "default_aggregation",
            "enabled",
        }
        if not (self.model_fields_set & editable):
            raise ValueError("At least one metadata property is required")
        if self.output_name is None and "output_name" in self.model_fields_set:
            raise ValueError("output_name cannot be null")
        if ("data_type" in self.model_fields_set) != (
            "semantic_config" in self.model_fields_set
        ):
            raise ValueError("data_type and semantic_config must be changed together")
        if self.data_type is None and "data_type" in self.model_fields_set:
            raise ValueError("data_type cannot be null")
        if self.semantic_config is None and "semantic_config" in self.model_fields_set:
            raise ValueError("semantic_config cannot be null")
        if self.default_aggregation is None and "default_aggregation" in self.model_fields_set:
            raise ValueError("default_aggregation cannot be null")
        if self.enabled is None and "enabled" in self.model_fields_set:
            raise ValueError("enabled cannot be null")
        return self


class DatasourceParameter(BaseModel):
    """Lossless parameter wire owned by Data Studio's datasource resource."""

    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_]+$")
    definition: dict[str, Any]

    @model_validator(mode="after")
    def check(self) -> DatasourceParameter:
        roles = self.definition.get("parametersModifiedByRoles")
        display = self.definition.get("parameterDisplayConfiguration")
        if not isinstance(roles, list) or not roles or not isinstance(display, dict):
            raise ValueError("Parameter roles and display configuration are required")
        role_ids: set[int] = set()
        for role in roles:
            if not isinstance(role, dict) or type(role.get("key")) is not int:
                raise ValueError("Every parameter role needs an integer key")
            value = role.get("value")
            if role["key"] in role_ids or not isinstance(value, dict):
                raise ValueError("Parameter roles must be unique objects")
            role_ids.add(role["key"])
            if value.get("name") != self.key or type(value.get("dataType")) is not int:
                raise ValueError("Every parameter role must use the key and declare dataType")
            for list_key in ("options", "defaultValue"):
                if list_key in value and not isinstance(value[list_key], list):
                    raise ValueError(f"{list_key} must be a list")
            if "validation" in value and not isinstance(value["validation"], dict):
                raise ValueError("validation must be an object")
        if display.get("name") != self.key or not isinstance(display.get("displayName"), str):
            raise ValueError("Display configuration must use the key and a displayName")
        return self


def _pointer(path: tuple[str | int, ...]) -> str:
    return "/" + "/".join(
        str(part).replace("~", "~0").replace("/", "~1") for part in path
    )


NO_REPORT_SCOPE = (
    "No report in scope: the datasource was edited on its own, so no report that uses it "
    "was scanned."
)


def field_reference_inventory(
    config: dict[str, Any] | None, source_id: str, field_names: set[str]
) -> dict[str, Any]:
    """Find saved report references without pretending unscoped data is safe.

    Component and blend query trees normally carry a nearest ``datasourceId``.
    Some protobuf-shaped resources do not, so token matches without an owner are
    conservative blockers instead of being silently ignored. Without a report
    (``config`` None) nothing is scanned, and the result says so.
    """
    if config is None:
        empty = {name: [] for name in field_names}
        return {
            "references": empty,
            "other_source_matches": dict(empty),
            "reference_scope": NO_REPORT_SCOPE,
        }
    if not isinstance(config.get("report"), dict) or not isinstance(config.get("page"), list):
        raise StudioError("INVALID_REPORT", "Full report and pages are required")
    references = {name: [] for name in field_names}
    other_sources = {name: [] for name in field_names}
    patterns = {
        name: re.compile(rf"(?<![A-Za-z0-9_])(?:t0\.)?{re.escape(name)}(?![A-Za-z0-9_])")
        for name in field_names
    }

    def visit(value: Any, path: tuple[str | int, ...] = (), owner: str | None = None):
        if isinstance(value, dict):
            local_owner = owner
            for key in ("datasourceId", "dataSourceId"):
                if isinstance(value.get(key), str):
                    local_owner = value[key]
                    break
            for key, child in value.items():
                visit(child, path + (key,), local_owner)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + (index,), owner)
            return
        if not isinstance(value, str):
            return
        for name, pattern in patterns.items():
            if not pattern.search(value):
                continue
            match = {
                "path": _pointer(path),
                "value": value,
                "owner_datasource_id": owner,
                "classification": "scoped" if owner == source_id else "unscoped",
            }
            if owner is not None and owner != source_id:
                match["classification"] = "other_source"
                other_sources[name].append(match)
            else:
                references[name].append(match)

    visit(config)
    return {
        "references": references,
        "other_source_matches": other_sources,
        "reference_scope": (
            "Identifier-token matches across the full saved report/pages/resources. "
            "Matches scoped to another datasource are informational; ownerless matches "
            "are conservative blockers."
        ),
    }


def parameter_reference_inventory(
    config: dict[str, Any] | None, source_draft: dict[str, Any], keys: set[str]
) -> dict[str, list[dict[str, Any]]]:
    """Conservatively inventory formula/report references to datasource parameters."""
    references = {key: [] for key in keys}
    patterns = {
        key: re.compile(rf"(?:\ue000|(?<![A-Za-z0-9_])){re.escape(key)}(?![A-Za-z0-9_])")
        for key in keys
    }

    def visit(value: Any, path: tuple[str | int, ...], scope: str) -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                visit(child, path + (name,), scope)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + (index,), scope)
        elif isinstance(value, str):
            for key, pattern in patterns.items():
                if pattern.search(value):
                    references[key].append(
                        {"scope": scope, "path": _pointer(path), "value": value}
                    )

    visit(source_draft.get("datasourceBlock", {}).get("fields", []), ("fields",), "field")
    if config is not None:
        visit(config, (), "report")
    return references


# Optional team convention: one fixed reader account per BigQuery project, for example
# "datastudio-viewer@{project}.iam.gserviceaccount.com". Without it, pass the account.
ACCOUNT_TEMPLATE_ENV = "DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE"


def canonical_account(draft: dict[str, Any]) -> str:
    """The configured reader account for the BigQuery project a source runs in."""
    template = os.environ.get(ACCOUNT_TEMPLATE_ENV, "")
    if "{project}" not in template:
        raise StudioError(
            "ACCOUNT_REQUIRED",
            f"Pass service_account, or set {ACCOUNT_TEMPLATE_ENV} "
            "(e.g. datastudio-viewer@{project}.iam.gserviceaccount.com)",
        )
    configs = [
        block.get("connectorBlockConfig", {}).get("connectorConfig", {}).get(
            "bigQueryConnectorConfig"
        )
        for block in draft.get("blocks", [])
    ]
    projects = {
        config.get("billingProjectId") or config.get("projectId")
        for config in configs
        if isinstance(config, dict)
    }
    if len(projects) != 1 or None in projects:
        raise StudioError(
            "ACCOUNT_REQUIRED",
            "No single BigQuery project to derive the canonical account from; pass it",
        )
    return template.format(project=projects.pop())


def source_state(source: dict[str, Any]) -> dict[str, Any]:
    return {"draft": source["draft"], "published": source["published"]}


class Datasources:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def parameters(self, source_id: str) -> dict[str, Any]:
        source = await self.studio.datasource(source_id)
        draft = self._parameter_entries(source["draft"])
        published = self._parameter_entries(source["published"])
        return {
            "datasource_id": source_id,
            "parameters": copy.deepcopy(draft),
            "draft_matches_published": json_equal(draft, published),
            "datasource_fingerprint": source["fingerprint"],
            "source_mode": (
                "embedded" if source["source"].get("isEmbedded") is True else "shared"
            ),
        }

    async def upsert_parameters(
        self,
        source_id: str,
        parameters: list[DatasourceParameter],
        expected: str,
        *,
        dry_run: bool = True,
        allow_shared_source: bool = False,
        allow_in_use: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= len(parameters) <= 100 or len({item.key for item in parameters}) != len(
            parameters
        ):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct parameters")
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            source_mode = self._editable(source, allow_shared_source=allow_shared_source)
            if fingerprint(source_state(source)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed since inspection")
            report = await self.studio.consumers()
            report_fingerprint = fingerprint(report) if report is not None else None
            after = copy.deepcopy(source["draft"])
            entries = self._parameter_entries(after, create=True)
            by_key = {entry.get("key"): entry for entry in entries}
            if len(by_key) != len(entries) or None in by_key:
                raise StudioError("INVALID_RESPONSE", "Datasource parameter identities are invalid")
            existing_changes = {
                item.key
                for item in parameters
                if item.key in by_key
                and not json_equal(by_key[item.key].get("value"), item.definition)
            }
            references = parameter_reference_inventory(report, source["draft"], existing_changes)
            blockers = {key: refs for key, refs in references.items() if refs}
            if blockers and not allow_in_use:
                return {
                    "status": "blocked",
                    "reason": "PARAMETER_IN_USE_APPROVAL_REQUIRED",
                    "datasource_id": source_id,
                    "references": blockers,
                    "writes_dispatched": 0,
                }
            edits = []
            for item in parameters:
                replacement = {"key": item.key, "value": copy.deepcopy(item.definition)}
                before = copy.deepcopy(by_key.get(item.key))
                if item.key in by_key:
                    entries[entries.index(by_key[item.key])] = replacement
                else:
                    entries.append(replacement)
                edits.append({"key": item.key, "before": before, "after": replacement})
            plan = {
                "datasource_id": source_id,
                "parameters": edits,
                "references": references,
                "source_mode": source_mode,
                "impact_scope": self._impact_scope(source_mode),
                "publication": "publishes_full_source_draft",
                "atomic_scope": "datasource",
            }
            if dry_run:
                return {**plan, "status": "planned"}
            if json_equal(after, source["draft"]):
                return {**plan, "status": "unchanged"}
            return {
                **plan,
                **await self._publish(
                    source_id,
                    source,
                    after,
                    expected_report_fingerprint=report_fingerprint,
                    allow_shared_source=allow_shared_source,
                ),
            }

    async def delete_parameters(
        self,
        source_id: str,
        keys: list[str],
        expected: str,
        *,
        dry_run: bool = True,
        allow_shared_source: bool = False,
        allow_unscanned_shared_consumers: bool = False,
    ) -> dict[str, Any]:
        if (
            not 1 <= len(keys) <= 100
            or len(set(keys)) != len(keys)
            or any(re.fullmatch(r"[A-Za-z0-9_]+", key) is None for key in keys)
        ):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct parameter keys")
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            source_mode = self._editable(source, allow_shared_source=allow_shared_source)
            if source_mode == "shared" and not allow_unscanned_shared_consumers:
                raise StudioError(
                    "SHARED_CONSUMERS_UNSCANNED",
                    "Shared parameter deletion requires acceptance of external consumers",
                )
            if fingerprint(source_state(source)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed since inspection")
            after = copy.deepcopy(source["draft"])
            entries = self._parameter_entries(after)
            by_key = {entry.get("key"): entry for entry in entries}
            if not set(keys) <= by_key.keys():
                raise StudioError("PARAMETER_NOT_FOUND", "Parameter is not in datasource")
            report = await self.studio.consumers()
            report_fingerprint = fingerprint(report) if report is not None else None
            references = parameter_reference_inventory(report, source["draft"], set(keys))
            blockers = {key: refs for key, refs in references.items() if refs}
            if blockers:
                return {
                    "status": "blocked",
                    "reason": "PARAMETER_IN_USE",
                    "datasource_id": source_id,
                    "references": blockers,
                    "writes_dispatched": 0,
                }
            entries[:] = [entry for entry in entries if entry["key"] not in set(keys)]
            plan = {
                "datasource_id": source_id,
                "deleted_parameters": [copy.deepcopy(by_key[key]) for key in keys],
                "references": references,
                "source_mode": source_mode,
                "impact_scope": self._impact_scope(source_mode),
                "publication": "publishes_full_source_draft",
                "atomic_scope": "datasource",
            }
            if dry_run:
                return {**plan, "status": "planned"}
            return {
                **plan,
                **await self._publish(
                    source_id,
                    source,
                    after,
                    expected_report_fingerprint=report_fingerprint,
                    allow_shared_source=allow_shared_source,
                ),
            }

    @staticmethod
    def _parameter_entries(draft: dict[str, Any] | None, *, create: bool = False) -> list:
        if not isinstance(draft, dict):
            raise StudioError("INVALID_RESPONSE", "Datasource definition is missing")
        if create:
            resource = draft.setdefault("resource", {})
            if not isinstance(resource, dict):
                raise StudioError("INVALID_RESPONSE", "Datasource resource is malformed")
            configuration = resource.setdefault("parameterConfiguration", {})
            if not isinstance(configuration, dict):
                raise StudioError("INVALID_RESPONSE", "Parameter configuration is malformed")
            parameters = configuration.setdefault("parameters", [])
        else:
            parameters = (
                draft.get("resource", {}).get("parameterConfiguration", {}).get("parameters", [])
            )
        if not isinstance(parameters, list) or any(
            not isinstance(item, dict) for item in parameters
        ):
            raise StudioError("INVALID_RESPONSE", "Datasource parameters are malformed")
        return parameters

    async def create_calculated_fields(
        self,
        source_id: str,
        changes: list[CalculatedField],
        expected: str,
        *,
        dry_run: bool = True,
        allow_shared_source: bool = False,
    ) -> dict[str, Any]:
        return await self._calculated_fields(
            source_id,
            changes,
            expected,
            dry_run=dry_run,
            allow_shared_source=allow_shared_source,
        )

    async def edit_calculated_fields(
        self,
        source_id: str,
        changes: list[CalculatedFieldEdit],
        expected: str,
        *,
        dry_run: bool = True,
        allow_shared_source: bool = False,
    ) -> dict[str, Any]:
        names = [change.field_name for change in changes]
        if len(set(names)) != len(names):
            raise StudioError("INVALID_BATCH", "Each calculated field can be edited once")
        if any("calc_" in change.expression for change in changes):
            # Revision-0 validation sees the existing graph, not the proposed edits,
            # so a reference to another calculated field is checked for a cycle here.
            source = await self.studio.datasource(source_id)
            proposed_cycle(
                source["draft"].get("datasourceBlock", {}).get("fields") or [],
                {change.field_name: change.expression for change in changes},
            )
        return await self._calculated_fields(
            source_id,
            changes,
            expected,
            dry_run=dry_run,
            allow_shared_source=allow_shared_source,
        )

    async def delete_calculated_fields(
        self,
        source_id: str,
        field_names: list[str],
        expected: str,
        *,
        dry_run: bool = True,
        allow_shared_source: bool = False,
        allow_unscanned_shared_consumers: bool = False,
    ) -> dict[str, Any]:
        if (
            not 1 <= len(field_names) <= 100
            or len(set(field_names)) != len(field_names)
            or any(re.fullmatch(r"calc_[A-Za-z0-9_]+", name) is None for name in field_names)
        ):
            raise StudioError(
                "INVALID_BATCH", "Provide 1-100 distinct calculated field IDs"
            )
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            source_mode = self._editable(source, allow_shared_source=allow_shared_source)
            if source_mode == "shared" and not allow_unscanned_shared_consumers:
                raise StudioError(
                    "SHARED_CONSUMERS_UNSCANNED",
                    "Shared field deletion requires explicit acceptance of "
                    "unscanned external consumers",
                )
            if fingerprint(source_state(source)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed since inspection")
            fields = source["draft"].get("datasourceBlock", {}).get("fields")
            if not isinstance(fields, list):
                raise StudioError("INVALID_RESPONSE", "Datasource fields are missing")
            by_name = {field.get("field", {}).get("name"): field for field in fields}
            if len(by_name) != len(fields) or None in by_name:
                raise StudioError("INVALID_RESPONSE", "Datasource field identity is invalid")
            if not set(field_names) <= by_name.keys():
                raise StudioError("FIELD_NOT_FOUND", "Calculated field is not in datasource")
            for name in field_names:
                field = by_name[name]
                if field.get("createdBy") != 2 or not field.get("textFormula"):
                    raise StudioError(
                        "NOT_CALCULATED_FIELD", "Only user-created calculated fields can be deleted"
                    )

            graph = await Formulas(self.studio).dependencies(source_id)
            if graph["status"] != "inspected" or graph["diagnostics"]:
                raise StudioError(
                    "DEPENDENCY_GRAPH_INCOMPLETE",
                    "Cannot prove deletion safety while formula parsing is incomplete",
                )
            targets = {f"t0.{name}" for name in field_names}
            formula_blockers = {
                target: [
                    owner
                    for owner in graph["dependents"].get(target, [])
                    if owner not in targets
                ]
                for target in targets
            }
            formula_blockers = {key: value for key, value in formula_blockers.items() if value}

            report = await self.studio.consumers()
            report_fingerprint = fingerprint(report) if report is not None else None
            saved = field_reference_inventory(report, source_id, set(field_names))
            saved_blockers = {
                name: refs for name, refs in saved["references"].items() if refs
            }
            blockers = {
                "formula_dependents": formula_blockers,
                "saved_report_references": saved_blockers,
            }
            if formula_blockers or saved_blockers:
                return {
                    "status": "blocked",
                    "reason": "FIELD_IN_USE",
                    "datasource_id": source_id,
                    "field_names": field_names,
                    "dependency_check": blockers,
                    "saved_reference_check": saved,
                    "writes_dispatched": 0,
                }

            after = copy.deepcopy(source["draft"])
            after_fields = after["datasourceBlock"]["fields"]
            after["datasourceBlock"]["fields"] = [
                field for field in after_fields if field["field"]["name"] not in set(field_names)
            ]
            plan = {
                "datasource_id": source_id,
                "deleted_fields": [copy.deepcopy(by_name[name]) for name in field_names],
                "dependency_check": blockers,
                "saved_reference_check": saved,
                "publication": "publishes_full_source_draft",
                "atomic_scope": "datasource",
                "source_mode": source_mode,
                "impact_scope": self._impact_scope(source_mode),
            }
            if dry_run:
                return {**plan, "status": "planned"}
            return {
                **plan,
                **await self._publish(
                    source_id,
                    source,
                    after,
                    expected_report_fingerprint=report_fingerprint,
                    allow_shared_source=allow_shared_source,
                ),
            }

    async def edit_field_metadata(
        self,
        source_id: str,
        changes: list[FieldMetadataEdit],
        expected: str,
        *,
        dry_run: bool = True,
        allow_shared_source: bool = False,
        allow_in_use: bool = False,
    ) -> dict[str, Any]:
        names = [change.field_name for change in changes]
        if not 1 <= len(names) <= 100 or len(set(names)) != len(names):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct field edits")
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            source_mode = self._editable(source, allow_shared_source=allow_shared_source)
            if fingerprint(source_state(source)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed since inspection")
            after = copy.deepcopy(source["draft"])
            fields = after.get("datasourceBlock", {}).get("fields")
            if not isinstance(fields, list):
                raise StudioError("INVALID_RESPONSE", "Datasource fields are missing")
            by_name = {field.get("field", {}).get("name"): field for field in fields}
            if len(by_name) != len(fields) or None in by_name:
                raise StudioError("INVALID_RESPONSE", "Datasource field identity is invalid")
            if not set(names) <= by_name.keys():
                raise StudioError("FIELD_NOT_FOUND", "Field is not in datasourceBlock.fields")

            report = await self.studio.consumers()
            report_fingerprint = fingerprint(report) if report is not None else None
            saved = field_reference_inventory(report, source_id, set(names))
            usage_sensitive = {
                change.field_name
                for change in changes
                if (
                    "data_type" in change.model_fields_set
                    or "default_aggregation" in change.model_fields_set
                    or change.enabled is False
                )
            }
            formula_dependents: dict[str, list[str]] = {}
            if usage_sensitive:
                graph = await Formulas(self.studio).dependencies(source_id)
                if graph["status"] != "inspected" or graph["diagnostics"]:
                    raise StudioError(
                        "DEPENDENCY_GRAPH_INCOMPLETE",
                        "Cannot assess metadata impact while formula parsing is incomplete",
                    )
                formula_dependents = {
                    name: graph["dependents"].get(f"t0.{name}", [])
                    for name in usage_sensitive
                    if graph["dependents"].get(f"t0.{name}")
                }
            saved_sensitive = {
                name: saved["references"][name]
                for name in usage_sensitive
                if saved["references"][name]
            }
            if not allow_in_use and (formula_dependents or saved_sensitive):
                return {
                    "status": "blocked",
                    "reason": "FIELD_IN_USE_APPROVAL_REQUIRED",
                    "datasource_id": source_id,
                    "formula_dependents": formula_dependents,
                    "saved_report_references": saved_sensitive,
                    "writes_dispatched": 0,
                }

            edits = []
            for change in changes:
                target = by_name[change.field_name]
                before_field = copy.deepcopy(target)
                if "output_name" in change.model_fields_set:
                    target["outputName"] = change.output_name
                if "description" in change.model_fields_set:
                    if change.description is None:
                        target.pop("description", None)
                    else:
                        target["description"] = change.description
                if "data_type" in change.model_fields_set:
                    target["dataType"] = change.data_type
                    semantic = copy.deepcopy(target.get("outputSemanticInfo", {}))
                    if not isinstance(semantic, dict):
                        raise StudioError(
                            "INVALID_RESPONSE", "Field outputSemanticInfo is malformed"
                        )
                    semantic["semanticConfig"] = copy.deepcopy(change.semantic_config)
                    target["outputSemanticInfo"] = semantic
                if "default_aggregation" in change.model_fields_set:
                    target["defaultAggregationType"] = change.default_aggregation
                if "enabled" in change.model_fields_set:
                    target["enabled"] = change.enabled
                edits.append(
                    {
                        "field_name": change.field_name,
                        "before": before_field,
                        "after": copy.deepcopy(target),
                    }
                )

            plan = {
                "datasource_id": source_id,
                "edits": edits,
                "saved_reference_check": saved,
                "formula_dependents": formula_dependents,
                "source_mode": source_mode,
                "impact_scope": self._impact_scope(source_mode),
                "publication": "publishes_full_source_draft",
                "atomic_scope": "datasource",
            }
            if dry_run:
                return {**plan, "status": "planned"}
            if json_equal(after, source["draft"]):
                return {**plan, "status": "unchanged"}
            return {
                **plan,
                **await self._publish(
                    source_id,
                    source,
                    after,
                    expected_report_fingerprint=report_fingerprint,
                    allow_shared_source=allow_shared_source,
                ),
            }

    async def _calculated_fields(
        self,
        source_id: str,
        changes: list[CalculatedField] | list[CalculatedFieldEdit],
        expected: str,
        *,
        dry_run: bool,
        allow_shared_source: bool,
    ) -> dict[str, Any]:
        if not 1 <= len(changes) <= 100:
            raise StudioError("INVALID_BATCH", "Provide 1-100 calculated fields")
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            source_mode = self._editable(source, allow_shared_source=allow_shared_source)
            if fingerprint(source_state(source)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed since inspection")
            after = copy.deepcopy(source["draft"])
            fields = after.get("datasourceBlock", {}).get("fields")
            if not isinstance(fields, list):
                raise StudioError("INVALID_RESPONSE", "Datasource fields are missing")
            names = {field["field"]["name"] for field in fields}
            if len(names) != len(fields):
                raise StudioError("INVALID_RESPONSE", "Duplicate field names in source")
            by_name = {field["field"]["name"]: field for field in fields}
            created = []
            validator = Formulas(self.studio)
            for change in changes:
                existing = None
                if isinstance(change, CalculatedFieldEdit):
                    existing = by_name.get(change.field_name)
                    if existing is None:
                        raise StudioError(
                            "FIELD_NOT_FOUND", "Calculated field is not in datasource"
                        )
                    if not existing.get("textFormula") or existing["field"].get("ns") != "t0":
                        raise StudioError(
                            "NOT_CALCULATED_FIELD", "Only t0 calculated fields editable"
                        )
                validation = await validator.validate(source_id, change.expression)
                if not validation["valid"]:
                    return {
                        "status": "validation_failed",
                        "datasource_id": source_id,
                        "output_name": change.output_name,
                        "validation": validation,
                        "writes_dispatched": 0,
                    }
                if validation["source_draft_fingerprint"] != fingerprint(source["draft"]):
                    raise StudioError("VERSION_CONFLICT", "Datasource changed before validation")
                column = copy.deepcopy(validation["column"])
                if column.get("type") and change.aggregation != 7:
                    raise StudioError(
                        "INVALID_AGGREGATION", "Aggregate expressions require AUTO (7)"
                    )
                name = existing["field"]["name"] if existing else "calc_" + uuid.uuid4().hex[:20]
                if existing is None and name in names:
                    raise StudioError("ID_COLLISION", "Generated field identity already exists")
                names.add(name)
                if existing is not None:
                    # Replace validation-owned derived metadata, preserve opaque
                    # and user-authored fields. Never carry obsolete semantics
                    # across a formula change when validation omits them.
                    merged = copy.deepcopy(existing)
                    for key in (
                        "semanticConfig",
                        "outputSemanticInfo",
                        "columnType",
                        "type",
                        "conceptType",
                        "dataType",
                        "params",
                        "property",
                        "ancestors",
                        "lookerFilterOnlyFieldAllowedValues",
                    ):
                        merged.pop(key, None)
                    merged.update(column)
                    column = merged
                column.update(
                    {
                        "field": {"ns": "t0", "name": name},
                        "outputName": change.output_name,
                        "textFormula": change.expression,
                        "frontendTextFormula": change.expression,
                        "defaultAggregationType": change.aggregation,
                        "createdBy": 2,
                    }
                )
                if existing is None:
                    fields.append(column)
                else:
                    fields[fields.index(existing)] = column
                created.append(column)
            # Every validation is read-only against the existing source, not a
            # partially constructed draft. Never publish concurrent human changes.
            fresh = await self.studio.datasource(source_id)
            self._editable(fresh, allow_shared_source=allow_shared_source)
            if fingerprint(source_state(fresh)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed during preparation")
            plan = {
                "datasource_id": source_id,
                "edited_fields"
                if isinstance(changes[0], CalculatedFieldEdit)
                else "created_fields": created,
                "publication": "publishes_full_source_draft",
                "atomic_scope": "datasource",
                "dependencies": "existing fields only; no within-batch references",
                "source_mode": source_mode,
                "impact_scope": self._impact_scope(source_mode),
            }
            if dry_run:
                return {
                    **plan,
                    "status": "planned",
                    "ids_are_provisional": not isinstance(changes[0], CalculatedFieldEdit),
                }
            return {
                **plan,
                **await self._publish(
                    source_id,
                    source,
                    after,
                    allow_shared_source=allow_shared_source,
                ),
            }

    def _editable(self, source: dict[str, Any], *, allow_shared_source: bool = False) -> str:
        if source.get("editable") is not True:
            raise StudioError("READ_ONLY_DATASOURCE", "Source is not editable")
        if source["source"].get("isEmbedded") is True:
            if source.get("embedded_in_report_id") != self.studio.config.report_id:
                raise StudioError(
                    "OUT_OF_SCOPE",
                    "Embedded datasource belongs to report "
                    f"{source.get('embedded_in_report_id')}; pass that report_id",
                )
            source_mode = "embedded"
        elif not allow_shared_source:
            raise StudioError(
                "SHARED_SOURCE_APPROVAL_REQUIRED",
                "Shared datasource mutation requires allow_shared_source=true",
            )
        else:
            source_mode = "shared"
        if not json_equal(source["draft"], source["published"]):
            raise StudioError(
                "UNPUBLISHED_SOURCE_CHANGES", "Refusing to publish unrelated draft changes"
            )
        return source_mode

    @staticmethod
    def _impact_scope(source_mode: str) -> str:
        return (
            "configured report only"
            if source_mode == "embedded"
            else "all consumers of shared datasource; external report inventory is unavailable"
        )

    async def rename(
        self,
        source_id: str,
        changes: list[FieldRename],
        expected: str,
        *,
        dry_run: bool = True,
        allow_shared_source: bool = False,
    ) -> dict[str, Any]:
        names = [c.field_name for c in changes]
        if not 1 <= len(names) <= 100 or len(set(names)) != len(names):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct field names")
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            source_mode = self._editable(source, allow_shared_source=allow_shared_source)
            if fingerprint(source_state(source)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed since inspection")
            after = copy.deepcopy(source["draft"])
            fields = after.get("datasourceBlock", {}).get("fields")
            if not isinstance(fields, list):
                raise StudioError("INVALID_RESPONSE", "Datasource fields are missing")
            by_name = {f["field"]["name"]: f for f in fields}
            if len(by_name) != len(fields):
                raise StudioError("INVALID_RESPONSE", "Duplicate field names in source")
            if not set(names) <= by_name.keys():
                raise StudioError("FIELD_NOT_FOUND", "Field must belong to datasourceBlock.fields")
            plan = []
            for change in changes:
                field = by_name[change.field_name]
                plan.append(
                    {
                        "field_name": change.field_name,
                        "before": field.get("outputName"),
                        "after": change.output_name,
                    }
                )
                field["outputName"] = change.output_name
            if dry_run:
                return {
                    "status": "planned",
                    "datasource_id": source_id,
                    "renames": plan,
                    "publication": "publishes_full_source_draft",
                    "atomic_scope": "datasource",
                    "source_mode": source_mode,
                    "impact_scope": self._impact_scope(source_mode),
                }
            if json_equal(after, source["draft"]):
                return {"status": "unchanged", "datasource_id": source_id}
            return {
                **await self._publish(
                    source_id,
                    source,
                    after,
                    allow_shared_source=allow_shared_source,
                ),
                "renames": plan,
                "source_mode": source_mode,
                "impact_scope": self._impact_scope(source_mode),
            }

    async def settings(
        self,
        source_id: str,
        expected: str,
        *,
        cache_hours: int | None = None,
        field_editing_in_reports: bool | None = None,
        community_viz_access: bool | None = None,
        native_functions: bool | None = None,
        dry_run: bool = True,
        allow_shared_source: bool = False,
    ) -> dict[str, Any]:
        """Datasource editor toggles: data freshness, field editing in reports, community
        visualizations access, native (SQL) functions."""
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            source_mode = self._editable(source, allow_shared_source=allow_shared_source)
            if fingerprint(source_state(source)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed since inspection")
            after = copy.deepcopy(source["draft"])
            if cache_hours is not None:
                if cache_hours not in (0, 1, 4, 12):
                    raise StudioError("INVALID_FRESHNESS", "cache_hours: 0 (15 min), 1, 4 or 12")
                after["queryCachePolicy"] = (
                    {"shelfLife": {"amount": 15, "chronoUnit": 1}, "isDefaultPolicy": False}
                    if cache_hours == 0 else
                    {"shelfLife": {"amount": cache_hours, "chronoUnit": 2},
                     "isDefaultPolicy": cache_hours == 12})
            for key, value in (("isUnlocked", field_editing_in_reports),
                               ("allowThirdPartyViz", community_viz_access),
                               ("allowNativeFunctions", native_functions)):
                if value is not None:
                    after[key] = value
            if json_equal(after, source["draft"]):
                return {"status": "unchanged", "datasource_id": source_id}
            changed = {k: after.get(k) for k in after if not json_equal(
                after.get(k), source["draft"].get(k))}
            if dry_run:
                return {"status": "planned", "datasource_id": source_id, "changes": changed,
                        "source_mode": source_mode,
                        "impact_scope": self._impact_scope(source_mode)}
            return {**await self._publish(source_id, source, after,
                                          allow_shared_source=allow_shared_source),
                    "changes": changed, "source_mode": source_mode}

    async def rename_source(
        self, source_id: str, name: str, expected: str, *, dry_run: bool = True,
        allow_shared_source: bool = False,
    ) -> dict[str, Any]:
        """The datasource editor's title. The server owns the name: publishing a block name
        is ignored; renameDatasource sets the title and the draft/published block name."""
        if not name.strip() or len(name) > 1000:
            raise StudioError("INVALID_NAME", "1-1000 characters")
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            source_mode = self._editable(source, allow_shared_source=allow_shared_source)
            if fingerprint(source_state(source)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed since inspection")
            before = source["draft"].get("datasourceBlock", {}).get("name")
            plan = {"datasource_id": source_id, "before": before, "after": name,
                    "source_mode": source_mode, "impact_scope": self._impact_scope(source_mode)}
            if before == name:
                return {"status": "unchanged", **plan}
            if dry_run:
                return {"status": "planned", **plan}
            record = self.studio.journal_action("datasource_rename", **plan)
            await self.studio.session.post(
                "renameDatasource", {"datasourceId": source_id, "title": name})
            fresh = await self.studio.datasource(source_id)
            names = {state: (fresh.get(state) or {}).get("datasourceBlock", {}).get("name")
                     for state in ("draft", "published")}
            record["state"] = "verified" if set(names.values()) == {name} else "verification_failed"
            record["after_names"] = names
            self.studio._journal(record["operation_id"], record)
            return {"status": record["state"], "operation_id": record["operation_id"],
                    "names": names, **plan}

    async def refresh_fields(
        self,
        source_id: str,
        expected: str,
        *,
        dry_run: bool = True,
        allow_shared_source: bool = False,
        allow_in_use: bool = False,
    ) -> dict[str, Any]:
        """The editor's Refresh fields: take the connector's current columns.

        New columns become fields; fields whose column is gone are removed; every surviving
        field keeps its labels, types, aggregation and description as edited. Calculated
        fields and parameters are untouched.
        """
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            source_mode = self._editable(source, allow_shared_source=allow_shared_source)
            if fingerprint(source_state(source)) != expected:
                raise StudioError("VERSION_CONFLICT", "Datasource changed since inspection")
            after = copy.deepcopy(source["draft"])
            connectors = [
                block for block in after.get("blocks", []) if "connectorBlockConfig" in block
            ]
            fields = after.get("datasourceBlock", {}).get("fields")
            if len(connectors) != 1 or not isinstance(fields, list):
                raise StudioError(
                    "UNSUPPORTED_SOURCE_SHAPE",
                    "Refresh needs a datasource with exactly one connector block",
                )
            connector = connectors[0]
            columns = await self.studio.session.columns(
                source_id, connector["connectorBlockConfig"]
            )
            # The connector answers with two layers per column: the connector field
            # (mutableVersion points up) and the datasource field over it (immutableVersion
            # points down), plus the system Record Count.
            connector_fields = [column for column in columns if "mutableVersion" in column]
            offered = {
                column["immutableVersion"]["name"]: column
                for column in columns
                if "immutableVersion" in column
            }
            if not connector_fields or len(offered) != len(connector_fields):
                raise StudioError("INVALID_RESPONSE", "Connector columns have an unknown shape")

            def column_of(field: dict[str, Any]) -> str | None:
                physical = field.get("immutableVersion", {}).get("name")
                return physical if field.get("createdBy") == 1 and physical else None

            existing = {column_of(field): field for field in fields if column_of(field)}
            missing = sorted(set(existing) - set(offered))
            added = [name for name in offered if name not in existing]
            retyped = sorted(
                name
                for name in set(existing) & set(offered)
                if existing[name].get("underlyingConnectorDataType")
                != offered[name].get("underlyingConnectorDataType")
            )
            removed_ids = {existing[name]["field"]["name"] for name in missing}

            dependents: dict[str, list[str]] = {}
            saved: dict[str, Any] = {"references": {}}
            report_fingerprint = None
            if removed_ids:
                graph = await Formulas(self.studio).dependencies(source_id)
                if graph["status"] != "inspected" or graph["diagnostics"]:
                    raise StudioError(
                        "DEPENDENCY_GRAPH_INCOMPLETE",
                        "Cannot assess removed columns while formula parsing is incomplete",
                    )
                dependents = {
                    name: graph["dependents"][f"t0.{name}"]
                    for name in removed_ids
                    if graph["dependents"].get(f"t0.{name}")
                }
                report = await self.studio.consumers()
                report_fingerprint = fingerprint(report) if report is not None else None
                saved = field_reference_inventory(report, source_id, removed_ids)
                in_use = {name: refs for name, refs in saved["references"].items() if refs}
                if (dependents or in_use) and not allow_in_use:
                    return {
                        "status": "blocked",
                        "reason": "FIELD_IN_USE_APPROVAL_REQUIRED",
                        "datasource_id": source_id,
                        "missing_columns": missing,
                        "formula_dependents": dependents,
                        "saved_report_references": in_use,
                        "writes_dispatched": 0,
                    }

            connector["fields"] = connector_fields
            after["datasourceBlock"]["fields"] = [
                field for field in fields if field["field"]["name"] not in removed_ids
            ] + [copy.deepcopy(offered[name]) for name in added]
            plan = {
                "datasource_id": source_id,
                "added_columns": added,
                "missing_columns": missing,
                "retyped_columns": retyped,
                "retyped_note": "Retyped columns keep their field type; review them by hand."
                if retyped
                else None,
                "formula_dependents": dependents,
                "saved_reference_check": saved,
                "source_mode": source_mode,
                "impact_scope": self._impact_scope(source_mode),
                "publication": "publishes_full_source_draft",
                "atomic_scope": "datasource",
            }
            if dry_run:
                return {**plan, "status": "planned"}
            if json_equal(after, source["draft"]):
                return {**plan, "status": "unchanged"}
            return {
                **plan,
                **await self._publish(
                    source_id,
                    source,
                    after,
                    expected_report_fingerprint=report_fingerprint,
                    allow_shared_source=allow_shared_source,
                ),
            }

    async def set_credentials(
        self,
        source_id: str,
        service_account: str | None = None,
        *,
        dry_run: bool = True,
        allow_shared_source: bool = False,
    ) -> dict[str, Any]:
        """Point the datasource at a service account (Service Account Credentials).

        Without an explicit account, DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE names it, with
        {project} = the BigQuery project the source's queries run and are billed in.
        """
        async with self.studio.mutation_lock():
            source = await self.studio.datasource(source_id)
            if source.get("editable") is not True:
                raise StudioError("READ_ONLY_DATASOURCE", "Source is not editable")
            embedded = source["source"].get("isEmbedded") is True
            if embedded and source.get("embedded_in_report_id") != self.studio.config.report_id:
                raise StudioError(
                    "OUT_OF_SCOPE",
                    "Embedded datasource belongs to report "
                    f"{source.get('embedded_in_report_id')}; pass that report_id",
                )
            if not embedded and not allow_shared_source:
                raise StudioError(
                    "SHARED_SOURCE_APPROVAL_REQUIRED",
                    "Shared datasource credentials require allow_shared_source=true",
                )
            account = service_account or canonical_account(source["published"] or source["draft"])
            if not re.fullmatch(r"[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com", account):
                raise StudioError("INVALID_ACCOUNT", "Provide a service account email")
            before = source["credentials"]
            target = {"type": "service_account", "account": account}
            plan = {
                "datasource_id": source_id,
                "before": before,
                "after": target,
                "account_source": "explicit" if service_account else "template",
                "source_mode": "embedded" if embedded else "shared",
                "impact_scope": self._impact_scope("embedded" if embedded else "shared"),
            }
            if before == target:
                return {**plan, "status": "unchanged"}
            if dry_run:
                return {**plan, "status": "planned"}
            operation_id = str(uuid.uuid4())
            record = {
                "kind": "datasource_credentials",
                "operation_id": operation_id,
                "report_id": self.studio.config.report_id,
                "datasource_id": source_id,
                "started_at": datetime.now(UTC).isoformat(),
                "state": "dispatched",
                "before": before,
                "intended": target,
            }
            self.studio._journal(operation_id, record)
            try:
                await self.studio.session.post(
                    "updateDataAccessControl",
                    {
                        "datasourceId": source_id,
                        "reportId": self.studio.config.report_id or "",
                        "delegatedAccessEnabled": True,
                        "serviceAccountEmail": account,
                        "oauthType": 1,
                    },
                )
            except StudioError as exc:
                record["dispatch_error"] = exc.code
            try:
                actual = (await self.studio.datasource(source_id))["credentials"]
                record["after"] = actual
                record["state"] = "verified" if actual == target else "verification_failed"
            except Exception:
                record["state"] = "outcome_unknown"
            self.studio._journal(operation_id, record)
            return {
                **plan,
                "status": record["state"],
                "operation_id": operation_id,
                "actual": record.get("after"),
                "dispatch_error": record.get("dispatch_error"),
            }

    async def _publish(
        self,
        source_id: str,
        before: dict,
        draft: dict,
        *,
        expected_report_fingerprint: str | None = None,
        allow_shared_source: bool = False,
    ) -> dict[str, Any]:
        operation_id = str(uuid.uuid4())
        source_mode = "embedded" if before["source"].get("isEmbedded") is True else "shared"
        record = {
            "kind": "datasource_edit",
            "operation_id": operation_id,
            "report_id": self.studio.config.report_id,
            "datasource_id": source_id,
            "started_at": datetime.now(UTC).isoformat(),
            "state": "prepared",
            "before": source_state(before),
            "intended_draft": draft,
            "expected_report_fingerprint": expected_report_fingerprint,
            "source_mode": source_mode,
        }
        self.studio._journal(operation_id, record)
        fresh = await self.studio.datasource(source_id)
        self._editable(fresh, allow_shared_source=allow_shared_source)
        if not json_equal(source_state(fresh), record["before"]):
            record["state"] = "conflict"
            self.studio._journal(operation_id, record)
            raise StudioError("VERSION_CONFLICT", "Datasource changed before publication")
        if expected_report_fingerprint is not None:
            current_report = (await self.studio.report())["reportConfig"]
            if fingerprint(current_report) != expected_report_fingerprint:
                record["state"] = "conflict"
                self.studio._journal(operation_id, record)
                raise StudioError(
                    "VERSION_CONFLICT", "Report consumers changed before datasource publication"
                )
        record["state"] = "dispatched"
        self.studio._journal(operation_id, record)
        try:
            body = {"draft": draft, "clearThumbnails": False}
            if self.studio.report_scoped:
                body["reportId"] = self.studio.config.report_id
            await self.studio.session.post("publishDatasource", body)
        except StudioError as exc:
            record["dispatch_error"] = exc.code
        try:
            actual = source_state(await self.studio.datasource(source_id))
            record["after"] = actual
            mismatches = [
                key for key in ("draft", "published") if not json_equal(draft, actual[key])
            ]
            record["mismatches"] = mismatches
            record["state"] = "verification_failed" if mismatches else "verified"
        except Exception:
            record["state"] = "outcome_unknown"
        self.studio._journal(operation_id, record)
        return {
            "status": record["state"],
            "operation_id": operation_id,
            "datasource_id": source_id,
            "mismatches": record.get("mismatches", []),
            "dispatch_error": record.get("dispatch_error"),
            "fingerprint": fingerprint(record["after"]) if "after" in record else None,
            "publication": "published" if record["state"] == "verified" else "unverified",
            "concurrency": "preflight_only; API offers no characterized server CAS",
        }

    async def restore(self, record: dict, *, dry_run: bool = True) -> dict[str, Any]:
        source_id = record["datasource_id"]
        current = await self.studio.datasource(source_id)
        allow_shared_source = record.get("source_mode") == "shared"
        self._editable(current, allow_shared_source=allow_shared_source)
        if json_equal(source_state(current), record["before"]):
            return {"status": "unchanged", "restored_operation_id": record["operation_id"]}
        if "after" not in record:
            raise StudioError("UNRESOLVED_OPERATION", "Datasource read-back is missing")
        if not json_equal(source_state(current), record["after"]):
            raise StudioError("VERSION_CONFLICT", "Datasource changed after operation")
        if dry_run:
            return {
                "status": "planned",
                "datasource_id": source_id,
                "restored_operation_id": record["operation_id"],
                "draft_after": record["before"]["draft"],
            }
        result = await self._publish(
            source_id,
            current,
            record["before"]["draft"],
            allow_shared_source=allow_shared_source,
        )
        return {**result, "restored_operation_id": record["operation_id"]}
