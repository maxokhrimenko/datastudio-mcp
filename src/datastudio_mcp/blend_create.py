"""Resource > Manage blends > Add a blend: create a report blend (data-view resource).

The editor saves a blend as one `dataViewResource` entry: a left-deep tree of
`join`/`union` nodes over per-table `query` inputs (each a datasource and its selected
concepts), plus output fields. Inputs and joins are built here from the source fields;
the output fields come from the editor's own output-schema builder on a detached
native model, exactly what the blend editor's Save writes (verified against a UI save).
"""

from __future__ import annotations

import copy
import secrets
import string
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .blend_joins import RUNTIME_CODES, JoinType
from .blends import Blends, blend_inventory
from .browser import StudioError
from .document import fingerprint, json_equal
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio

AUTO = 7


def _token(length: int) -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(length))


class BlendMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, description="Source field name from datasource_get")
    aggregation: int | None = Field(default=None, description="Default: the field's own")


class BlendTable(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    datasource_id: str = Field(min_length=1, description="Datasource attached to the report")
    dimensions: list[str] = Field(default_factory=list, max_length=100)
    metrics: list[BlendMetric] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check(self) -> BlendTable:
        names = self.dimensions + [m.name for m in self.metrics]
        if not names:
            raise ValueError("A table needs at least one dimension or metric")
        if len(set(names)) != len(names):
            raise ValueError("A field appears twice in one table")
        return self


class BlendKey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    left_table: int = Field(ge=0, description="Index of an earlier table")
    left_field: str = Field(min_length=1)
    right_field: str = Field(min_length=1, description="Dimension of the table being merged")


class BlendJoin(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    join_type: JoinType = "LEFT_OUTER_JOIN"
    keys: list[BlendKey] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check(self) -> BlendJoin:
        if (self.join_type == "CROSS_JOIN") == bool(self.keys):
            raise ValueError("Cross joins take no keys; every other merge needs keys")
        return self


class NewBlend(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=1000)
    tables: list[BlendTable] = Field(min_length=2, max_length=5)
    joins: list[BlendJoin] = Field(description="One per table after the first, in order")

    @model_validator(mode="after")
    def check(self) -> NewBlend:
        if len(self.joins) != len(self.tables) - 1:
            raise ValueError("Provide exactly one join per table after the first")
        for index, join in enumerate(self.joins, start=1):
            if any(key.left_table >= index for key in join.keys):
                raise ValueError("Join keys reference an earlier table on the left")
        return self


def _concept(field: dict, *, dimension: bool, aggregation: int | None) -> dict[str, Any]:
    name = "qt_" + _token(9)
    data: dict[str, Any] = {"sourceFieldName": field["field"]["name"]}
    if not dimension:
        aggregation = field.get("defaultAggregationType") if aggregation is None else aggregation
        if aggregation not in (None, AUTO):
            data["aggregation"] = aggregation
    semantic = [c["semanticType"] for c in (field.get("outputSemanticInfo") or {}).get(
        "semanticConfig", []) if "semanticType" in c] if dimension else []
    return {"id": {"id": f"t0.{name}", "name": name, "namespace": "t0"},
            "semantic": semantic, "queryTimeTransformation": {"dataTransformation": data}}


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def build_tree(spec: NewBlend, sources: dict[str, dict]) -> tuple[dict, list[dict[str, str]]]:
    """Left-deep tree of the blend and, per table, source field name -> concept name."""
    queries, names = [], []
    for table in spec.tables:
        fields = {f["field"]["name"]: f for f in sources[table.datasource_id][
            "datasourceBlock"]["fields"] if f.get("enabled", True)}
        concepts, by_field = [], {}
        selected = [(n, True, None) for n in table.dimensions] + [
            (m.name, False, m.aggregation) for m in table.metrics]
        for field_name, dimension, aggregation in selected:
            if field_name not in fields:
                raise StudioError("FIELD_NOT_FOUND",
                                  f"{field_name} is not an enabled field of {table.datasource_id}")
            concept = _concept(fields[field_name], dimension=dimension, aggregation=aggregation)
            concepts.append(concept)
            if dimension:
                by_field[field_name] = concept["id"]["name"]
        queries.append({"query": {"datasourceId": table.datasource_id, "concepts": concepts}})
        names.append(by_field)
    tree = queries[0]
    for index, join in enumerate(spec.joins, start=1):
        pairs = []
        for key in join.keys:
            left, right = names[key.left_table].get(key.left_field), names[index].get(
                key.right_field)
            if left is None or right is None:
                raise StudioError("INVALID_BLEND_JOIN",
                                  "Join keys must be dimensions of their tables")
            pairs.append({"leftName": left, "rightName": right})
        merge: dict[str, Any] = {"right": queries[index], "left": tree}
        if join.join_type == "UNION":
            tree = {"union": {**merge, "unionKeyPair": pairs}}
            continue
        if join.join_type != "CROSS_JOIN":
            merge["condition"] = {"and": {"conditions": [
                {"boolean": {"joinKeyPair": pair}} for pair in pairs]}}
        merge["type"] = 3 if join.join_type == "CROSS_JOIN" else RUNTIME_CODES[join.join_type]
        tree = {"join": merge}
    return tree, names


def blend_value(spec: NewBlend, tree: dict) -> dict[str, Any]:
    # The editor also sends delegatedAccessEnabled/isUnlocked/isCacheable/
    # allowNativeFunctions; the server does not store them on a blend.
    return {"blockDatasource": {
        "blocks": [{"id": "block_" + _token(10), "type": 6, "inputBlockIds": [],
                    "outputBlockIds": [], "fields": [], "isExperimental": True,
                    "treeQueryBlockConfig": tree}],
        "datasourceBlock": {"id": "block_" + _token(10), "type": 1, "inputBlockIds": [],
                            "outputBlockIds": [], "fields": [], "name": spec.name}}}


class BlendCreate:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def create(self, spec: NewBlend, expected: str, *, dry_run: bool = True) -> dict:
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            service = Blends(self.studio)
            if blend_inventory(before, set())["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            attached = {s["id"] for s in await self.studio.sources()}
            sources, tokens = {}, {}
            blends = {b["id"] for b in blend_inventory(before, set())["blends"]}
            for table in spec.tables:
                source_id = table.datasource_id
                if source_id in blends:
                    raise StudioError("BLEND_INPUT_UNSUPPORTED",
                                      "A blend cannot be a blend input; use its datasources")
                if source_id not in attached:
                    raise StudioError("DATASOURCE_NOT_ATTACHED",
                                      "Add the datasource to the report first")
                if source_id not in sources:
                    source = await self.studio.datasource(source_id)
                    if not json_equal(source["draft"], source["published"]):
                        raise StudioError("UNPUBLISHED_DATASOURCE",
                                          "Matching draft/published required")
                    sources[source_id], tokens[source_id] = source["published"], fingerprint(
                        source)
            if any(j.join_type == "UNION" for j in spec.joins):
                union = next(t for t in (await service.join_types())["types"]
                             if t["name"] == "UNION")
                if not union["available"]:
                    raise StudioError("BLEND_TYPE_UNAVAILABLE", "Union is disabled in this editor")
            tree, names = build_tree(spec, sources)
            blend_id = "dataViewResource" + _token(10)
            value = blend_value(spec, tree)
            validation = await service.validate_candidate(
                before, blend_id, value, rebuild_output_schema=True, new=True)
            if not validation["structurally_valid"]:
                raise StudioError("INVALID_BLEND", f"Editor rejected the blend: "
                                                   f"{validation['issues']}")
            fields = _without_nulls(validation["rebuilt_output_fields"])
            value["blockDatasource"]["datasourceBlock"]["fields"] = fields
            after = report_content(before)
            after.setdefault("resource", {}).setdefault("dataViewResource", {}).setdefault(
                "entry", []).append({"key": blend_id, "value": copy.deepcopy(value)})
            plan = {"blend_id": blend_id, "name": spec.name,
                    "output_fields": [{"name": f["field"]["name"], "label": f.get("outputName"),
                                       "data_type": f.get("dataType")} for f in fields],
                    "input_concepts": names}
            if dry_run:
                return {"status": "planned", **plan,
                        "note": "blend_id and concept names are regenerated on the real write"}
            for source_id, token in tokens.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError("VERSION_CONFLICT", "Blend datasource changed")
            return {**await ReportResources(self.studio).write(before, after), **plan}

    async def delete(self, blend_ids: list[str], expected: str, *, dry_run: bool = True) -> dict:
        """Manage blends > Remove: refused while anything in the report references it."""
        if not 1 <= len(blend_ids) <= 100 or len(set(blend_ids)) != len(blend_ids):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct blend IDs")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = blend_inventory(before, set())
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            known = {b["id"]: b for b in inventory["blends"]}
            missing = [i for i in blend_ids if i not in known]
            if missing:
                raise StudioError("NOT_FOUND", f"Unknown blends: {', '.join(missing)}")
            used = {i: known[i]["references"] for i in blend_ids if known[i]["references"]}
            if used:
                raise StudioError("BLEND_IN_USE",
                                  f"Still used: {', '.join(used)}; remove those components first")
            after = report_content(before)
            entries = after["resource"]["dataViewResource"]["entry"]
            entries[:] = [e for e in entries if e["key"] not in blend_ids]
            filters = sorted({f for i in blend_ids for q in known[i]["inputs"]
                              for f in q.get("filter_ids") or []})
            plan = {"deleted": blend_ids, **({"input_filters_left_in_report": filters,
                    "note": "These filters stay in the report; filters_delete removes them"}
                    if filters else {})}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await ReportResources(self.studio).write(before, after), **plan}
