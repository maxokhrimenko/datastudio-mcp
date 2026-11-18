"""Lossless JSON edits and entity lookup independent of browser and MCP."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .browser import StudioError


# Domain-separates fingerprint tokens from plain content hashes, so a token is never
# mistaken for (or compared with) a SHA-256 of the same JSON computed elsewhere.
_DOMAIN = "dsmcp-fp/2.bf5d439ef92ead2b"


def fingerprint(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{_DOMAIN}\n{canonical}".encode()).hexdigest()


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add", "replace", "remove", "test"]
    path: str
    value: Any = None

    @model_validator(mode="after")
    def check(self) -> Patch:
        if not self.path.startswith("/"):
            raise ValueError("A non-root JSON Pointer is required")
        if self.op != "remove" and "value" not in self.model_fields_set:
            raise ValueError("This operation requires value, including explicit null")
        return self


def tokens(pointer: str) -> list[str]:
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise StudioError("INVALID_POINTER", "Expected a valid non-root JSON Pointer")
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]


def array_index(token: str, size: int, *, append: bool = False) -> int:
    if append and token == "-":
        return size
    if not token.isascii() or not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise StudioError("INVALID_POINTER", "Array index must be a nonnegative canonical integer")
    index = int(token)
    if index >= size + int(append):
        raise StudioError("INVALID_POINTER", "Array index is out of bounds")
    return index


def apply_patches(document: dict[str, Any], patches: list[Patch]) -> dict[str, Any]:
    result = copy.deepcopy(document)
    for patch in patches:
        path = tokens(patch.path)
        parent: Any = result
        try:
            for token in path[:-1]:
                parent = (
                    parent[array_index(token, len(parent))]
                    if isinstance(parent, list)
                    else parent[token]
                )
            last = path[-1]
            if isinstance(parent, list):
                index = array_index(last, len(parent), append=patch.op == "add")
                if patch.op == "add":
                    parent.insert(index, copy.deepcopy(patch.value))
                elif patch.op == "remove":
                    parent.pop(index)
                elif patch.op == "replace":
                    parent[index] = copy.deepcopy(patch.value)
                elif fingerprint(parent[index]) != fingerprint(patch.value):
                    raise StudioError("PRECONDITION_FAILED", f"Test failed at {patch.path}")
            elif isinstance(parent, dict):
                if patch.op != "add" and last not in parent:
                    raise KeyError(last)
                if patch.op == "remove":
                    del parent[last]
                elif patch.op in ("add", "replace"):
                    parent[last] = copy.deepcopy(patch.value)
                elif fingerprint(parent[last]) != fingerprint(patch.value):
                    raise StudioError("PRECONDITION_FAILED", f"Test failed at {patch.path}")
            else:
                raise TypeError("Parent is not a container")
        except (KeyError, IndexError, TypeError) as exc:
            raise StudioError("INVALID_POINTER", f"No target at {patch.path}") from exc
    return result


def find_page(report: dict[str, Any], page_id: str) -> dict[str, Any]:
    for page in report["reportConfig"]["page"]:
        if page["pageId"] == page_id:
            return page
    raise StudioError("NOT_FOUND", "Page does not belong to this report")


def walk_components(components: list[dict[str, Any]]):
    for component in components:
        yield component
        yield from walk_components(component.get("componentConfig", []))


def find_component(page: dict[str, Any], component_id: str) -> dict[str, Any]:
    for component in walk_components(page.get("componentConfig", [])):
        if component["componentId"] == component_id:
            return component
    raise StudioError("NOT_FOUND", "Component does not belong to this page")


def assert_subset(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Server may add defaults, but must retain every explicit expected value."""
    # JSON has one number type. Google emits integral geometry as 470 even when
    # the client sent 470.0; bool is deliberately excluded despite Python's subtype.
    if type(expected) in (int, float) and type(actual) in (int, float):
        return [] if expected == actual else [path or "/"]
    if isinstance(expected, dict) and isinstance(actual, dict):
        failures = []
        for key, value in expected.items():
            child = f"{path}/{key}"
            if key in actual:
                failures.extend(assert_subset(value, actual[key], child))
            elif not (type(value) in (int, float, bool, str) and not value):
                # The server omits proto default scalars (0, false, ""): a theme color
                # reference {index: 0} is saved as {}. Absent equals the default.
                failures.append(child)
        return failures
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        return [
            p
            for i, (left, right) in enumerate(zip(expected, actual, strict=True))
            for p in assert_subset(left, right, f"{path}/{i}")
        ]
    return [] if fingerprint(expected) == fingerprint(actual) else [path or "/"]


def overlay_native_delta(before: Any, after: Any, saved: Any) -> Any:
    """Apply only native editor changes, retaining untouched opaque server fields.

    A serializer can add defaults, coerce scalar representations, or omit opaque
    fields even without an edit. Its complete output must not replace saved JSON.
    Arrays with stable cardinality merge by index; changed cardinality is an
    explicit replacement and requires the caller's corresponding operation scope.
    """
    if fingerprint(before) == fingerprint(after):
        return copy.deepcopy(saved)
    if isinstance(before, dict) and isinstance(after, dict) and isinstance(saved, dict):
        result = copy.deepcopy(saved)
        for key in before.keys() - after.keys():
            result.pop(key, None)
        for key in after:
            if key not in before:
                result[key] = copy.deepcopy(after[key])
            elif key in saved:
                result[key] = overlay_native_delta(before[key], after[key], saved[key])
            elif fingerprint(before[key]) != fingerprint(after[key]):
                result[key] = copy.deepcopy(after[key])
        return result
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        if not isinstance(saved, list) or len(saved) != len(before):
            raise StudioError(
                "CODEC_SHAPE_MISMATCH", "Changed native array cannot be aligned to saved elements"
            )
        return [
            overlay_native_delta(old, new, raw)
            for old, new, raw in zip(before, after, saved, strict=True)
        ]
    return copy.deepcopy(after)


def json_equal(left: Any, right: Any) -> bool:
    """Exact JSON structure/value equality, numeric spellings aside; bool is distinct."""
    return not assert_subset(left, right) and not assert_subset(right, left)


def merge_concept_definitions(before: list, after: list, saved: list) -> list:
    """Merge chart concept definitions by stable name, not array position.

    Native field selection can reorder/remove/add definitions. Preserved concepts
    keep opaque saved properties even when the overall array cardinality changes.
    """

    def index(values):
        if not isinstance(values, list) or any(
            not isinstance(v, dict) or not isinstance(v.get("name"), str) for v in values
        ):
            raise StudioError("CODEC_CONTRACT_CHANGED", "Invalid concept definitions")
        result = {v["name"]: v for v in values}
        if len(result) != len(values):
            raise StudioError("CODEC_CONTRACT_CHANGED", "Duplicate concept names")
        return result

    old, new, raw = index(before), index(after), index(saved)
    if old.keys() != raw.keys():
        raise StudioError("EDITOR_STALE", "Native and saved concept inventories differ")
    return [
        overlay_native_delta(old[name], value, raw[name]) if name in old else copy.deepcopy(value)
        for name, value in new.items()
    ]


def drop_new_nulls(value: Any, saved: Any = None) -> Any:
    """Omit null members the saved JSON does not have: the server stores proto3 JSON, where
    null means unset, and never returns them. Saved nulls (if any) are kept verbatim."""
    if isinstance(value, list):
        return [
            drop_new_nulls(item, saved[i] if isinstance(saved, list) and i < len(saved) else None)
            for i, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    base = saved if isinstance(saved, dict) else {}
    # A null the saved JSON lacks (new member, or a cleared value) is dropped: the server
    # stores it as unset, and a dropped key is then verified as removed.
    return {
        key: drop_new_nulls(item, base.get(key))
        for key, item in value.items()
        if item is not None or (key in base and base[key] is None)
    }


IDENTITY_KEYS = ("componentId", "groupId", "pageId", "id", "name")


def _identity(items: list) -> str | None:
    """The key that identifies every element of a list of objects uniquely, if any."""
    if not items:
        return None
    return next((key for key in IDENTITY_KEYS if _identity_by(items, key)), None)


def _identity_by(items: list, key: str) -> bool:
    values = [item.get(key) if isinstance(item, dict) else None for item in items]
    return all(isinstance(v, str) and v for v in values) and len(set(values)) == len(values)


def overlay_delta(before: Any, after: Any, saved: Any) -> Any:
    """overlay_native_delta that also aligns lists of identified objects by identity.

    A native operation that adds, removes or reorders components, groups or resources
    changes list cardinality; elements are matched by componentId/groupId/id so every
    retained element keeps its saved opaque fields, new ones come from the native side.
    """
    if fingerprint(before) == fingerprint(after):
        return copy.deepcopy(saved)
    if isinstance(before, dict) and isinstance(after, dict) and isinstance(saved, dict):
        result = copy.deepcopy(saved)
        for key in before.keys() - after.keys():
            result.pop(key, None)
        for key in after:
            if key not in before:
                result[key] = copy.deepcopy(after[key])
            elif key in saved:
                result[key] = overlay_delta(before[key], after[key], saved[key])
            elif fingerprint(before[key]) != fingerprint(after[key]):
                result[key] = copy.deepcopy(after[key])
        return result
    if isinstance(before, list) and isinstance(after, list) and isinstance(saved, list):
        key = _identity(after) or _identity(before)
        unique = all(
            not items or _identity_by(items, key) for items in (before, after, saved)
        )
        if key and unique:
            old = {item[key]: item for item in before}
            raw = {item[key]: item for item in saved}
            if set(old) != set(raw):
                # The loaded model holds elements the saved content lacks (or vice
                # versa): its baseline is not the saved state, so no delta is trustworthy.
                raise StudioError(
                    "EDITOR_STALE", "Loaded model differs from saved content; reload it"
                )
            return [
                overlay_delta(old[item[key]], item, raw[item[key]])
                if item[key] in old
                else copy.deepcopy(item)
                for item in after
            ]
        if len(before) == len(after) == len(saved):
            return [overlay_delta(b, a, s) for b, a, s in zip(before, after, saved, strict=True)]
        if len(before) == len(after):
            raise StudioError(
                "CODEC_SHAPE_MISMATCH", "Changed native array cannot be aligned to saved elements"
            )
    return copy.deepcopy(after)


def normalize_colors(value: Any) -> Any:
    """Colors in the shape the server stores them, so read-back is exact: a transparent
    color without its opacity 0, and no palette-picker annotations (available, disabled)."""
    if isinstance(value, dict):
        out = {k: normalize_colors(v) for k, v in value.items()}
        if "color" in out:
            if out["color"] == "transparent" and out.get("opacity") == 0:
                out.pop("opacity")
            out.pop("available", None)
            out.pop("disabled", None)
        return out
    if isinstance(value, list):
        return [normalize_colors(v) for v in value]
    return value
