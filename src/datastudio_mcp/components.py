"""Component lifecycle inside a page, with page-sized atomic writes and recovery."""

from __future__ import annotations

import copy
import math
import uuid
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .browser import StudioError
from .document import find_page, fingerprint, json_equal, walk_components
from .native import Native

if TYPE_CHECKING:
    from .service import Studio



def _line_ends(component: dict[str, Any]) -> dict | None:
    """A line's two end points in page coordinates; its saved box can lag behind them."""
    return (component.get("propertyConfig", {}).get("componentProperty", {})
            .get("arrowProperty", {}).get("terminals"))


def vertical_extent(component: dict[str, Any]) -> tuple[float, float]:
    ends = _line_ends(component)
    if ends:
        tops = [ends["start"]["top"], ends["end"]["top"]]
        return min(tops), max(tops)
    a = component["attributeConfig"]["componentAttribute"]
    return (a.get("top") or 0), (a.get("top") or 0) + (a.get("height") or 0)


def shift_vertically(component: dict[str, Any], dy: float) -> None:
    component["attributeConfig"]["componentAttribute"]["top"] = (
        component["attributeConfig"]["componentAttribute"].get("top") or 0) + dy
    ends = _line_ends(component)
    if ends:
        ends["start"]["top"] += dy
        ends["end"]["top"] += dy

class NewGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    component_ids: list[str] = Field(min_length=2, max_length=100)


class GroupMove(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    group_id: str = Field(min_length=1)
    offset_x: int = Field(default=0, ge=-9007199254740991, le=9007199254740991)
    offset_y: int = Field(default=0, ge=-9007199254740991, le=9007199254740991)


def translate_groups(
    page: dict[str, Any], moves: list[GroupMove]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not 1 <= len(moves) <= 100 or len({m.group_id for m in moves}) != len(moves):
        raise StudioError("INVALID_BATCH", "Move 1–100 distinct groups")
    groups = page.get("groupConfig", [])
    by_id = {g["groupId"]: g for g in groups}
    if len(by_id) != len(groups):
        raise StudioError("DUPLICATE_ID", "Group IDs are not unique")
    selected = {m.group_id for m in moves}
    if not selected <= by_id.keys():
        raise StudioError("NOT_FOUND", "Group does not belong to this page")
    if any(by_id[g].get("childGroupId") for g in selected) or any(
        selected.intersection(g.get("childGroupId", [])) for g in groups
    ):
        raise StudioError(
            "NESTED_GROUP_OPERATION_REQUIRED", "Nested groups need hierarchy-aware translation"
        )
    after = copy.deepcopy(page)
    components = {c["componentId"]: c for c in after.get("componentConfig", [])}
    if len(components) != len(after.get("componentConfig", [])):
        raise StudioError("DUPLICATE_ID", "Component IDs are not unique")
    owners: dict[str, list[str]] = {}
    for group in groups:
        for component_id in group.get("componentId", []):
            owners.setdefault(component_id, []).append(group["groupId"])
    diffs = []
    for move in moves:
        members = by_id[move.group_id].get("componentId", [])
        if not members:
            raise StudioError("INVALID_GROUP", "Group has no direct members")
        for component_id in members:
            if owners.get(component_id) != [move.group_id]:
                raise StudioError(
                    "AMBIGUOUS_GROUP_MEMBERSHIP", "Component belongs to overlapping groups"
                )
            if component_id not in components:
                raise StudioError("INVALID_GROUP", "Group references a missing top-level component")
            component = components[component_id]
            if component.get("componentConfig"):
                raise StudioError(
                    "COMPOSITE_OPERATION_REQUIRED",
                    "Composite child translation is not yet characterized",
                )
            attrs = component.get("attributeConfig", {}).get("componentAttribute", {})
            before = {key: attrs.get(key) for key in ("left", "top")}
            if not all(
                type(v) in (int, float)
                and math.isfinite(v)
                and int(v) == v
                and abs(v) <= 9007199254740991
                for v in before.values()
            ):
                raise StudioError(
                    "INVALID_GEOMETRY", "Group members require exact integer pixel positions"
                )
            target = {
                "left": int(before["left"]) + move.offset_x,
                "top": int(before["top"]) + move.offset_y,
            }
            if any(abs(v) > 9007199254740991 for v in target.values()):
                raise StudioError(
                    "INVALID_GEOMETRY", "Translated position exceeds exact browser integer range"
                )
            attrs.update(target)
            diffs.append(
                {
                    "group_id": move.group_id,
                    "component_id": component_id,
                    "before": before,
                    "after": target,
                }
            )
    return after, diffs


class ComponentGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    component_id: str = Field(min_length=1)
    left: float | None = None
    top: float | None = None
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_geometry(self) -> ComponentGeometry:
        values = self.model_dump(exclude_unset=True, exclude={"component_id"})
        if not values or any(value is None for value in values.values()):
            raise ValueError("Provide at least one geometry value; explicit null is not allowed")
        return self


Arrangement = Literal[
    "align_left",
    "align_center",
    "align_right",
    "align_top",
    "align_middle",
    "align_bottom",
    "distribute_horizontal",
    "distribute_vertical",
]

LayerAction = Literal["bring_to_front", "send_to_back", "bring_forward", "send_backward"]


def layer_order(order: list[str], selected: list[str], action: LayerAction) -> list[str]:
    if len(order) != len(set(order)):
        raise StudioError("DUPLICATE_ID", "Page component IDs are not unique")
    if not 1 <= len(selected) <= 100 or len(selected) != len(set(selected)):
        raise StudioError("INVALID_BATCH", "Select 1–100 unique component IDs")
    chosen = set(selected)
    if not chosen <= set(order):
        raise StudioError("NOT_FOUND", "Select components belonging to this page")
    # Native components[] is back-to-front. Selection order never changes
    # relative stacking within the selected or unselected subsets.
    if action == "bring_to_front":
        return [x for x in order if x not in chosen] + [x for x in order if x in chosen]
    if action == "send_to_back":
        return [x for x in order if x in chosen] + [x for x in order if x not in chosen]
    result = list(order)
    if action == "bring_forward":
        for i in range(len(result) - 2, -1, -1):
            if result[i] in chosen and result[i + 1] not in chosen:
                result[i], result[i + 1] = result[i + 1], result[i]
    elif action == "send_backward":
        for i in range(1, len(result)):
            if result[i] in chosen and result[i - 1] not in chosen:
                result[i], result[i - 1] = result[i - 1], result[i]
    else:
        raise StudioError("INVALID_LAYER_ACTION", "Unknown layer action")
    return result


def arrangement_geometry(
    selected: list[dict[str, Any]], action: Arrangement
) -> list[ComponentGeometry]:
    """Native ungrouped selection-box alignment and equal-edge-gap distribution."""
    horizontal = action in {"align_left", "align_center", "align_right", "distribute_horizontal"}
    distribution = action in {"distribute_horizontal", "distribute_vertical"}
    if action not in {
        "align_left",
        "align_center",
        "align_right",
        "align_top",
        "align_middle",
        "align_bottom",
        "distribute_horizontal",
        "distribute_vertical",
    }:
        raise StudioError("INVALID_ARRANGEMENT", "Unknown arrangement action")
    if not (3 if distribution else 2) <= len(selected) <= 100:
        raise StudioError(
            "INVALID_BATCH", "Alignment needs 2–100 objects; distribution needs 3–100"
        )
    position, size = ("left", "width") if horizontal else ("top", "height")
    objects = []
    for component in selected:
        attrs = component.get("attributeConfig", {}).get("componentAttribute", {})
        if component.get("componentConfig"):
            raise StudioError("COMPOSITE_OPERATION_REQUIRED", "Arrange leaf components only")
        if (
            not all(
                type(attrs.get(k)) in (int, float) and math.isfinite(attrs[k])
                for k in ("left", "top", "width", "height")
            )
            or min(attrs["width"], attrs["height"]) <= 0
        ):
            raise StudioError("INVALID_GEOMETRY", "Component has no positive static bounds")
        rotation = attrs.get("rotation", 0)
        if type(rotation) not in (int, float) or not math.isfinite(rotation) or rotation % 360 != 0:
            raise StudioError(
                "ROTATED_OPERATION_REQUIRED", "Rotated arrangement is not yet characterized"
            )
        objects.append((component["componentId"], attrs[position], attrs[size]))
    if distribution:
        objects.sort(key=lambda item: item[1])  # Stable ties retain caller selection order.
        gap = sum(
            objects[i][1] - objects[i - 1][1] - objects[i - 1][2] for i in range(1, len(objects))
        ) / (len(objects) - 1)
        values = [objects[0][1]]
        for i in range(1, len(objects) - 1):
            values.append(values[-1] + objects[i - 1][2] + gap)
        values.append(objects[-1][1])
    else:
        low = min(item[1] for item in objects)
        high = max(item[1] + item[2] for item in objects)
        if action in {"align_left", "align_top"}:
            values = [low for _ in objects]
        elif action in {"align_right", "align_bottom"}:
            values = [high - item[2] for item in objects]
        else:
            values = [low / 2 + high / 2 - item[2] / 2 for item in objects]
    if not all(math.isfinite(value) for value in values):
        raise StudioError("INVALID_GEOMETRY", "Arrangement overflowed finite coordinates")
    return [
        ComponentGeometry(component_id=item[0], **{position: value})
        for item, value in zip(objects, values, strict=True)
    ]


def select_top_level(page: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    if not ids or len(set(ids)) != len(ids):
        raise StudioError("INVALID_BATCH", "Provide nonempty unique component IDs")
    components = {c["componentId"]: c for c in page.get("componentConfig", [])}
    if not set(ids) <= set(components):
        raise StudioError("NOT_FOUND", "Select top-level components belonging to this page")
    members = {
        component_id
        for group in page.get("groupConfig", [])
        for component_id in group.get("componentId", [])
    }
    selected = [components[component_id] for component_id in ids]
    if members.intersection(c["componentId"] for c in walk_components(selected)):
        raise StudioError(
            "GROUP_OPERATION_REQUIRED", "Grouped components require a group-aware operation"
        )
    return selected


class Components:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def _checked(self, page_id: str, expected: str) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = await self.studio.report()
        frame = find_page(raw, page_id)
        if fingerprint(frame["page"]) != expected:
            raise StudioError(
                "VERSION_CONFLICT", "Page changed since these components were selected"
            )
        return raw, frame

    async def groups_move(
        self, page_id: str, moves: list[GroupMove], expected: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        async with self.studio.mutation_lock():
            _, frame = await self._checked(page_id, expected)
            after, diffs = translate_groups(frame["page"], moves)
            await Native(self.studio.session).require_static_page(frame)
            if json_equal(after, frame["page"]):
                return {"status": "unchanged", "fingerprint": expected}
            plan = {
                "page_id": page_id,
                "changes": diffs,
                "atomic_scope": "page",
                "control_scope_changes": False,
            }
            if dry_run:
                return {"status": "planned", "before_fingerprint": expected, **plan}
            return {**await self.studio._write_page(frame, after), **plan}

    async def groups_create(
        self, page_id: str, groups: list[NewGroup], expected: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        if not 1 <= len(groups) <= 100:
            raise StudioError("INVALID_BATCH", "Create 1–100 groups")
        ids = [component_id for group in groups for component_id in group.component_ids]
        if len(ids) != len(set(ids)):
            raise StudioError("INVALID_BATCH", "Each component may belong to only one new group")
        async with self.studio.mutation_lock():
            raw, frame = await self._checked(page_id, expected)
            select_top_level(frame["page"], ids)
            existing = {
                g["groupId"]
                for p in raw["reportConfig"]["page"]
                for g in p["page"].get("groupConfig", [])
            }
            specs = [
                {"group_id": "g-" + uuid.uuid4().hex, "component_ids": group.component_ids}
                for group in groups
            ]
            if existing.intersection(spec["group_id"] for spec in specs):
                raise StudioError("DUPLICATE_ID", "New group ID already exists")
            drafts = await Native(self.studio.session).group_drafts(frame, specs)
            after = copy.deepcopy(frame["page"])
            after.setdefault("groupConfig", []).extend(drafts)
            plan = {
                "page_id": page_id,
                "groups": drafts,
                "created_group_ids": [spec["group_id"] for spec in specs],
                "atomic_scope": "page",
                "control_scope_changes": True,
            }
            if dry_run:
                return {"status": "planned", "ids_provisional": True, **plan}
            return {**await self.studio._write_page(frame, after), **plan}

    async def groups_ungroup(
        self, page_id: str, group_ids: list[str], expected: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        if not 1 <= len(group_ids) <= 100 or len(group_ids) != len(set(group_ids)):
            raise StudioError("INVALID_BATCH", "Select 1–100 unique group IDs")
        async with self.studio.mutation_lock():
            _, frame = await self._checked(page_id, expected)
            groups = frame["page"].get("groupConfig", [])
            by_id = {g["groupId"]: g for g in groups}
            if len(by_id) != len(groups):
                raise StudioError("DUPLICATE_ID", "Existing group IDs are not unique")
            selected = set(group_ids)
            if not selected <= by_id.keys():
                raise StudioError("NOT_FOUND", "Group does not belong to this page")
            if any(g.get("childGroupId") for g in groups if g["groupId"] in selected) or any(
                selected.intersection(g.get("childGroupId", [])) for g in groups
            ):
                raise StudioError(
                    "NESTED_GROUP_OPERATION_REQUIRED", "Nested groups need parent-aware ungrouping"
                )
            await Native(self.studio.session).require_static_page(frame)
            after = copy.deepcopy(frame["page"])
            after["groupConfig"] = [g for g in after["groupConfig"] if g["groupId"] not in selected]
            plan = {
                "page_id": page_id,
                "removed_groups": [by_id[g] for g in group_ids],
                "atomic_scope": "page",
                "control_scope_changes": True,
                "effect": "Removes group filters and returns members to page scope; components retained",
            }
            if dry_run:
                return {"status": "planned", **plan}
            return {**await self.studio._write_page(frame, after), **plan}

    async def layers(
        self,
        page_id: str,
        component_ids: list[str],
        action: LayerAction,
        expected: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        async with self.studio.mutation_lock():
            _, frame = await self._checked(page_id, expected)
            page = frame["page"]
            if page.get("groupConfig") and action not in ("bring_to_front", "send_to_back"):
                # One step may land inside a group's run of members; to the front or back
                # of the whole page never reorders a group's members among themselves.
                raise StudioError(
                    "GROUP_OPERATION_REQUIRED",
                    "On a grouped page only bring_to_front / send_to_back move a layer",
                )
            select_top_level(page, component_ids)
            before_ids = [c["componentId"] for c in page["componentConfig"]]
            after_ids = layer_order(before_ids, component_ids, action)
            await Native(self.studio.session).require_static_page(frame)
            if before_ids == after_ids:
                return {"status": "unchanged", "fingerprint": expected}
            after = copy.deepcopy(page)
            indexed = {c["componentId"]: c for c in after["componentConfig"]}
            after["componentConfig"] = [indexed[component_id] for component_id in after_ids]
            plan = {
                "page_id": page_id,
                "action": action,
                "before_order": before_ids,
                "after_order": after_ids,
                "order_direction": "back_to_front",
                "atomic_scope": "page",
            }
            if dry_run:
                return {"status": "planned", "before_fingerprint": expected, **plan}
            return {**await self.studio._write_page(frame, after), **plan}

    async def arrange(
        self,
        page_id: str,
        component_ids: list[str],
        action: Arrangement,
        expected: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        _, frame = await self._checked(page_id, expected)
        selected = select_top_level(frame["page"], component_ids)
        changes = arrangement_geometry(selected, action)
        # geometry rechecks the same fingerprint under its mutation lock. A
        # concurrent edit between calculation and dispatch cannot reuse this plan.
        result = await self.geometry(page_id, changes, expected, dry_run=dry_run)
        return {**result, "arrangement": action, "reference": "selection_bounds"}

    async def geometry(
        self,
        page_id: str,
        changes: list[ComponentGeometry],
        expected: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not 1 <= len(changes) <= 100:
            raise StudioError("INVALID_BATCH", "Provide 1–100 geometry changes")
        async with self.studio.mutation_lock():
            _, frame = await self._checked(page_id, expected)
            selected = select_top_level(frame["page"], [c.component_id for c in changes])
            # Responsive layout has a different ownership/placement contract.
            # Consult the loaded native model instead of inferring from saved sizes.
            await Native(self.studio.session).require_static_page(frame)
            after = copy.deepcopy(frame["page"])
            targets = {c["componentId"]: c for c in after["componentConfig"]}
            diffs = []
            for change, original in zip(changes, selected, strict=True):
                if original.get("componentConfig"):
                    raise StudioError(
                        "COMPOSITE_OPERATION_REQUIRED",
                        "Composite geometry needs coordinated child layout",
                    )
                attrs = original.get("attributeConfig", {}).get("componentAttribute", {})
                if not all(
                    type(attrs.get(k)) in (int, float) and math.isfinite(attrs[k])
                    for k in ("left", "top", "width", "height")
                ):
                    raise StudioError("INVALID_GEOMETRY", "Component has no static geometry")
                requested = change.model_dump(exclude_unset=True, exclude={"component_id"})
                # Live updatePage stores these four attributes as integer pixels,
                # truncating toward zero (including negative positions). Make
                # this conversion visible in preview, not a read-back tolerance.
                updates = {key: math.trunc(value) for key, value in requested.items()}
                if any(updates.get(key, 1) < 1 for key in ("width", "height")):
                    raise StudioError("INVALID_GEOMETRY", "Size must be at least one whole pixel")
                target = targets[change.component_id]["attributeConfig"]["componentAttribute"]
                target.update(updates)
                # A line is drawn from its end points: they span the new box corner to corner.
                ends = _line_ends(targets[change.component_id])
                if ends:
                    box = {**{k: attrs.get(k, 0) for k in ("left", "top", "width", "height")},
                           **updates}
                    down = ends["end"]["top"] >= ends["start"]["top"]
                    right = ends["end"]["left"] >= ends["start"]["left"]
                    y0, y1 = box["top"], box["top"] + (box["height"] if box["height"] > 1 else 0)
                    x0, x1 = box["left"], box["left"] + box["width"]
                    ends["start"].update(top=y0 if down else y1, left=x0 if right else x1)
                    ends["end"].update(top=y1 if down else y0, left=x1 if right else x0)
                diffs.append(
                    {
                        "component_id": change.component_id,
                        "before": {key: attrs[key] for key in updates},
                        "requested": requested,
                        "after": updates,
                    }
                )
            if json_equal(frame["page"], after):
                return {"status": "unchanged", "fingerprint": expected}
            plan = {
                "page_id": page_id,
                "changes": diffs,
                "atomic_scope": "page",
                "pixel_conversion": "truncate_toward_zero",
            }
            if dry_run:
                return {"status": "planned", "before_fingerprint": expected, **plan}
            return {**await self.studio._write_page(frame, after), **plan}

    async def insert_space(
        self, page_id: str, at_top: int, height: int, expected: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        """Make room on the canvas: everything starting at or below at_top moves down by
        height, a group as a whole (by its top member), and the page grows by height. A
        negative height removes empty space: what is below moves up and the page shrinks."""
        if height == 0 or at_top < 0:
            raise StudioError("INVALID_GEOMETRY", "height must be nonzero and at_top >= 0")
        async with self.studio.mutation_lock():
            _, frame = await self._checked(page_id, expected)
            await Native(self.studio.session).require_static_page(frame)
            after = copy.deepcopy(frame["page"])
            top = {c["componentId"]: vertical_extent(c)[0] for c in after.get("componentConfig", [])}
            unit = {cid: [cid] for cid in top}  # what moves together with a component
            for group in after.get("groupConfig", []):
                members = [m for m in group.get("componentId", []) if m in top]
                for m in members:
                    unit[m] = members
            moved = sorted({m for cid, members in unit.items()
                            if min(top[x] or 0 for x in members) >= at_top for m in members})
            if height < 0:
                # Removing space must not slide anything onto what stays above the line.
                for component in after.get("componentConfig", []):
                    if (component["componentId"] not in moved
                            and vertical_extent(component)[1] > at_top + height):
                        raise StudioError("OVERLAP", f"{component['componentId']} reaches below "
                                          f"{at_top + height}; remove less space")
            for component in after.get("componentConfig", []):
                if component["componentId"] in moved:
                    shift_vertically(component, height)
            attrs = after.setdefault("attributeConfig", {}).setdefault("pageAttribute", {})
            if "height" in attrs:
                attrs["height"] += height
            else:
                report = (await self.studio.report())["reportConfig"]["report"]
                base = report.get("attributeConfig", {}).get("reportAttribute", {}).get("height")
                if base is None:
                    raise StudioError("INVALID_GEOMETRY", "Page has no canvas height to grow")
                attrs["height"] = base + height
            if not moved:
                raise StudioError("NOT_FOUND", f"Nothing starts at or below {at_top}")
            plan = {"page_id": page_id, "moved_component_ids": moved, "offset_y": height,
                    "page_height": attrs["height"], "atomic_scope": "page"}
            if dry_run:
                return {"status": "planned", "before_fingerprint": expected, **plan}
            return {**await self.studio._write_page(frame, after), **plan}

    async def delete(
        self, page_id: str, component_ids: list[str], expected: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        async with self.studio.mutation_lock():
            _, frame = await self._checked(page_id, expected)
            selected = select_top_level(frame["page"], component_ids)
            removed_ids = {c["componentId"] for c in walk_components(selected)}
            after = copy.deepcopy(frame["page"])
            after["componentConfig"] = [
                c for c in after["componentConfig"] if c["componentId"] not in removed_ids
            ]
            conditions = [
                c for c in frame.get("chartCondition", []) if c.get("chartId") not in removed_ids
            ]
            if dry_run:
                return {
                    "status": "planned",
                    "page_id": page_id,
                    "removed_component_ids": sorted(removed_ids),
                    "removed_condition_owner_ids": [
                        c["chartId"]
                        for c in frame.get("chartCondition", [])
                        if c.get("chartId") in removed_ids
                    ],
                    "atomic_scope": "page",
                }
            result = await self.studio._write_page(frame, after, conditions)
            result["removed_component_ids"] = sorted(removed_ids)
            return result

    async def duplicate(
        self,
        page_id: str,
        component_ids: list[str],
        expected: str,
        offset_x: float = 20,
        offset_y: float = 20,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in (offset_x, offset_y)
        ):
            raise StudioError("INVALID_GEOMETRY", "Offsets must be finite numbers")
        async with self.studio.mutation_lock():
            raw, frame = await self._checked(page_id, expected)
            selected = select_top_level(frame["page"], component_ids)
            draft, _ = await Native(self.studio.session).duplicate_page(
                frame,
                {"displayName": "Copy"},
                require_static=True,
            )
            originals = list(walk_components(frame["page"]["componentConfig"]))
            copies = list(walk_components(draft["page"]["componentConfig"]))
            pairs = {old["componentId"]: new for old, new in zip(originals, copies, strict=True)}
            added = [copy.deepcopy(pairs[c["componentId"]]) for c in selected]
            existing_ids = {
                c["componentId"]
                for p in raw["reportConfig"]["page"]
                for c in walk_components(p["page"].get("componentConfig", []))
            }
            existing_ids.update(
                c["componentId"]
                for c in walk_components(raw["reportConfig"]["report"].get("componentConfig", []))
            )
            added_ids = [c["componentId"] for c in walk_components(added)]
            if len(added_ids) != len(set(added_ids)) or existing_ids.intersection(added_ids):
                raise StudioError("DUPLICATE_ID", "Copy contains conflicting component IDs")
            for component in added:
                attrs = component.get("attributeConfig", {}).get("componentAttribute", {})
                if not all(
                    type(attrs.get(k)) in (int, float) and math.isfinite(attrs[k])
                    for k in ("left", "top")
                ):
                    raise StudioError("INVALID_GEOMETRY", "Source has no static canvas position")
                attrs["left"] += offset_x
                attrs["top"] += offset_y
                if not all(math.isfinite(attrs[k]) for k in ("left", "top")):
                    raise StudioError("INVALID_GEOMETRY", "Resulting position is not finite")
            after = copy.deepcopy(frame["page"])
            after["componentConfig"].extend(added)
            conditions = copy.deepcopy(frame.get("chartCondition", []))
            conditions.extend(c for c in draft["chartCondition"] if c["chartId"] in added_ids)
            id_map = {
                c["componentId"]: pairs[c["componentId"]]["componentId"]
                for c in walk_components(selected)
            }
            plan = {
                "page_id": page_id,
                "component_id_map": id_map,
                "created_component_ids": added_ids,
                "atomic_scope": "page",
            }
            if dry_run:
                return {"status": "planned", "added_components": added, **plan}
            result = await self.studio._write_page(frame, after, conditions)
            return {**result, **plan}
