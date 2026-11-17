"""Typed join edits on saved blends, validated with the native detached model."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .blends import Blends, blend_inventory
from .browser import StudioError
from .document import fingerprint, json_equal, tokens
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio

JoinType = Literal["INNER_JOIN", "FULL_OUTER_JOIN", "LEFT_OUTER_JOIN", "RIGHT_OUTER_JOIN", "CROSS_JOIN", "UNION"]
RUNTIME_CODES = {"INNER_JOIN": 1, "FULL_OUTER_JOIN": 2, "LEFT_OUTER_JOIN": 3,
                 "RIGHT_OUTER_JOIN": 4, "CROSS_JOIN": 101, "UNION": 5}


class JoinKeyPair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    left_name: str = Field(min_length=1)
    right_name: str = Field(min_length=1)


class BlendJoinChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    blend_id: str = Field(min_length=1)
    join_path: str = Field(min_length=1)
    join_type: JoinType
    key_pairs: list[JoinKeyPair] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_pairs(self):
        if self.join_type == "CROSS_JOIN" and self.key_pairs:
            raise ValueError("Cross joins cannot have key pairs")
        if self.key_pairs is not None:
            names = [name for pair in self.key_pairs for name in (pair.left_name, pair.right_name)]
            if len(set(names)) != len(names):
                raise ValueError("Reusing fields in join conditions is unsupported")
        return self


def patch_join(definition: dict, change: BlendJoinChange) -> dict:
    """Change only merge type/keys; preserve children and unknown merge metadata."""
    result = copy.deepcopy(definition)
    path = tokens(change.join_path)
    parent = result
    try:
        for token in path[:-1]:
            parent = parent[int(token)] if isinstance(parent, list) else parent[token]
        kind = path[-1]
        if kind not in {"join", "union"} or not isinstance(parent.get(kind), dict):
            raise KeyError(kind)
        merge = parent[kind]
        if not isinstance(merge.get("left"), dict) or not isinstance(merge.get("right"), dict):
            raise KeyError("children")
        if change.key_pairs is None:
            if kind == "union":
                pairs = copy.deepcopy(merge.get("unionKeyPair", []))
            else:
                conditions = merge.get("condition", {}).get("and", {}).get("conditions", [])
                pairs = [copy.deepcopy(c["boolean"]["joinKeyPair"]) for c in conditions]
        else:
            pairs = [{"leftName": p.left_name, "rightName": p.right_name} for p in change.key_pairs]
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
        raise StudioError("INVALID_BLEND_JOIN", "Malformed join or key definition") from exc
    if change.join_type != "CROSS_JOIN" and not pairs:
        raise StudioError("INVALID_BLEND_JOIN", "Non-cross merges require key pairs")
    if change.join_type == "UNION":
        merge.pop("type", None)
        merge.pop("condition", None)
        merge["unionKeyPair"] = pairs
        if kind != "union":
            parent["union"] = parent.pop("join")
    else:
        merge.pop("unionKeyPair", None)
        merge["type"] = 3 if change.join_type == "CROSS_JOIN" else RUNTIME_CODES[change.join_type]
        if change.join_type == "CROSS_JOIN":
            merge.pop("condition", None)
        elif change.key_pairs is not None or kind == "union":
            merge["condition"] = {"and": {"conditions": [
                {"boolean": {"joinKeyPair": pair}} for pair in pairs]}}
        if kind != "join":
            parent["join"] = parent.pop("union")
    return result


class BlendJoins:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def patch(self, changes: list[BlendJoinChange], expected: str, *, dry_run: bool = True) -> dict:
        keys = [(c.blend_id, c.join_path) for c in changes]
        if not 1 <= len(keys) <= 100 or len(set(keys)) != len(keys):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct blend joins")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = blend_inventory(before, set(), include_definitions=True)
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            known = {b["id"]: b for b in inventory["blends"]}
            service = Blends(self.studio)
            available = {t["name"]: t for t in (await service.join_types())["types"]}
            candidates, sources = {}, {}
            # Apply deeper nodes first, so changing an ancestor join to union cannot
            # invalidate another selected original path within the same batch.
            for change in sorted(changes, key=lambda c: len(tokens(c.join_path)), reverse=True):
                item = known.get(change.blend_id)
                if item is None or change.join_path not in {j["path"] for j in item["joins"]}:
                    raise StudioError("NOT_FOUND", "Use a join path from blends_list")
                option = available.get(change.join_type)
                if not option or option["code"] != RUNTIME_CODES[change.join_type]:
                    raise StudioError("BLEND_CONTRACT_CHANGED", "Runtime merge enum changed")
                if not option["available"]:
                    raise StudioError("BLEND_TYPE_UNAVAILABLE", "Merge type disabled in current native UI")
                candidates[change.blend_id] = patch_join(
                    candidates.get(change.blend_id, item["definition"]), change)
            for blend_id in candidates:
                for query in known[blend_id]["inputs"]:
                    source_id = query["datasource_id"]
                    if not isinstance(source_id, str) or not source_id:
                        raise StudioError("INVALID_BLEND", "Blend input datasource missing")
                    if source_id not in sources:
                        source = await self.studio.datasource(source_id)
                        if not json_equal(source["draft"], source["published"]):
                            raise StudioError("UNPUBLISHED_DATASOURCE", "Matching draft/published required")
                        sources[source_id] = fingerprint(source)
            validations = []
            for blend_id, candidate in candidates.items():
                validation = await service.validate_candidate(before, blend_id, candidate)
                if not validation["structurally_valid"]:
                    raise StudioError("INVALID_BLEND", "Native structural validation rejected join candidate")
                validations.append(validation)
            for source_id, token in sources.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError("VERSION_CONFLICT", "Blend datasource changed during preparation")
            target = report_content(before)
            for entry in target["resource"]["dataViewResource"]["entry"]:
                if entry["key"] in candidates:
                    entry["value"] = candidates[entry["key"]]
            plan = {"changes": [c.model_dump() for c in changes], "validation": validations,
                    "atomic_scope": "report_resources", "source_guard": "Preflight, not cross-entity CAS",
                    "scope": "Explicit saved join type/key changes; no native normalization copied. "
                             "Structural validation is not numerical query proof or source type compatibility."}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await ReportResources(self.studio).write(before, target), **plan}
