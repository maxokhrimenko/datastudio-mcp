"""Page lifecycle and navigation; multi-request operations expose partial outcomes."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .browser import StudioError
from .document import Patch, apply_patches, assert_subset, fingerprint, walk_components
from .native import Native

if TYPE_CHECKING:
    from .service import Studio


class NewPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1)
    hidden: bool = False
    icon: dict[str, Any] | None = None


def structure(config: dict[str, Any]) -> dict[str, Any]:
    """Exclude server-owned revision/position fields; navigation owns actual order."""
    if not isinstance(config.get("navigationInfo", {}).get("navItems"), list):
        raise StudioError("INVALID_NAVIGATION", "Report navigation is unavailable")
    return {
        "pages": {
            p["pageId"]: {
                "page": copy.deepcopy(p["page"]),
                "chartCondition": copy.deepcopy(p.get("chartCondition", [])),
            }
            for p in config["page"]
        },
        "navigation": copy.deepcopy(config["navigationInfo"]),
    }


def page_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [p for item in value for p in page_items(item)]
    if isinstance(value, dict):
        if isinstance(value.get("page"), dict) and "pageId" in value["page"]:
            return [value["page"]]
        return [p for item in value.values() for p in page_items(item)]
    return []


def validate_navigation(snapshot: dict[str, Any]) -> None:
    nav = snapshot["navigation"]
    if not isinstance(nav, dict) or not isinstance(nav.get("navItems"), list):
        raise StudioError("INVALID_NAVIGATION", "navigation.navItems must be an array")
    ids = [p["pageId"] for p in page_items(nav)]
    if any(not isinstance(page_id, str) or not page_id for page_id in ids):
        raise StudioError("INVALID_NAVIGATION", "Page IDs must be nonempty strings")
    if len(ids) != len(set(ids)) or set(ids) != set(snapshot["pages"]):
        raise StudioError(
            "INVALID_NAVIGATION", "Every existing page must occur exactly once in navigation"
        )


def remove_navigation_pages(value: Any, ids: set[str]) -> Any:
    if isinstance(value, list):
        return [
            remove_navigation_pages(item, ids)
            for item in value
            if not (
                isinstance(item, dict)
                and isinstance(item.get("page"), dict)
                and item["page"].get("pageId") in ids
            )
        ]
    if isinstance(value, dict):
        return {k: remove_navigation_pages(v, ids) for k, v in value.items()}
    return value


class Pages:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def _read(self) -> dict[str, Any]:
        return (await self.studio.report())["reportConfig"]

    async def _checked(self, expected: str) -> dict[str, Any]:
        config = await self._read()
        if fingerprint(structure(config)) != expected:
            raise StudioError("VERSION_CONFLICT", "Report pages/navigation changed since planning")
        return config

    async def create(
        self, pages: list[NewPage], expected: str, index: int | None = None, *, dry_run: bool = True
    ) -> dict[str, Any]:
        if not pages:
            raise StudioError("INVALID_BATCH", "Provide at least one new page")
        async with self.studio.mutation_lock():
            config = await self._checked(expected)
            target = structure(config)
            items = target["navigation"]["navItems"]
            position = len(items) if index is None else index
            if type(position) is not int or not 0 <= position <= len(items):
                raise StudioError(
                    "INVALID_POSITION", "Index must be a top-level insertion position"
                )
            ids = []
            for offset, spec in enumerate(pages):
                page_id = "p_" + uuid.uuid4().hex[:10]
                while page_id in target["pages"]:
                    page_id = "p_" + uuid.uuid4().hex[:10]
                ids.append(page_id)
                target["pages"][page_id] = {
                    "page": {
                        "componentConfig": [],
                        "groupConfig": [],
                        "conceptDefs": [],
                        "propertyConfig": {"pageProperty": {"filters": [], "inheritFilters": True}},
                        "attributeConfig": {
                            "pageAttribute": {
                                "displayConfigVersion": 0,
                                "datasourceConfigVersion": 2,
                                "name": spec.name,
                            }
                        },
                    },
                    "chartCondition": [],
                }
                item = {"pageId": page_id, "displayName": spec.name}
                if spec.hidden:
                    item["hiddenFromViewer"] = True
                if spec.icon is not None:
                    item["icon"] = copy.deepcopy(spec.icon)
                items.insert(position + offset, {"page": item})
            return await self._reconcile(config, target, dry_run=dry_run, created_ids=ids)

    async def delete(
        self, page_ids: list[str], expected: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        if not page_ids or len(set(page_ids)) != len(page_ids):
            raise StudioError("INVALID_BATCH", "Provide nonempty unique page IDs")
        async with self.studio.mutation_lock():
            config = await self._checked(expected)
            target = structure(config)
            if not set(page_ids) <= set(target["pages"]):
                raise StudioError("NOT_FOUND", "A page does not belong to this report")
            if len(page_ids) == len(target["pages"]):
                raise StudioError("LAST_PAGE", "A report must retain at least one page")
            for page_id in page_ids:
                del target["pages"][page_id]
            target["navigation"] = remove_navigation_pages(target["navigation"], set(page_ids))
            return await self._reconcile(config, target, dry_run=dry_run)

    async def duplicate(
        self,
        page_id: str,
        name: str,
        expected: str,
        index: int | None = None,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not name:
            raise StudioError("INVALID_NAME", "A new page name is required")
        async with self.studio.mutation_lock():
            config = await self._checked(expected)
            source = next((p for p in config["page"] if p["pageId"] == page_id), None)
            if source is None:
                raise StudioError("NOT_FOUND", "Source page does not belong to this report")
            target = structure(config)
            metadata = copy.deepcopy(
                next(p for p in page_items(target["navigation"]) if p["pageId"] == page_id)
            )
            del metadata["pageId"]
            metadata["displayName"] = name
            draft, omitted = await Native(self.studio.session).duplicate_page(source, metadata)
            new_id = draft["pageId"]
            existing_ids = {
                c["componentId"]
                for p in config["page"]
                for c in walk_components(p["page"].get("componentConfig", []))
            }
            existing_ids.update(
                c["componentId"]
                for c in walk_components(config["report"].get("componentConfig", []))
            )
            new_ids = [c["componentId"] for c in walk_components(draft["page"]["componentConfig"])]
            if (
                new_id in target["pages"]
                or len(new_ids) != len(set(new_ids))
                or existing_ids.intersection(new_ids)
            ):
                raise StudioError(
                    "DUPLICATE_ID", "Native duplication did not isolate component IDs"
                )
            items = target["navigation"]["navItems"]
            position = len(items) if index is None else index
            if type(position) is not int or not 0 <= position <= len(items):
                raise StudioError(
                    "INVALID_POSITION", "Index must be a top-level insertion position"
                )
            target["pages"][new_id] = {
                "page": draft["page"],
                "chartCondition": draft["chartCondition"],
            }
            items.insert(position, {"page": {"pageId": new_id, **metadata}})
            result = await self._reconcile(config, target, dry_run=dry_run, created_ids=[new_id])
            result["source_page_id"] = page_id
            result["omitted_condition_owner_ids"] = omitted
            result["component_id_map"] = dict(
                zip(
                    [c["componentId"] for c in walk_components(source["page"]["componentConfig"])],
                    new_ids,
                    strict=True,
                )
            )
            return result

    async def navigation(
        self, patches: list[Patch], expected: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        async with self.studio.mutation_lock():
            config = await self._checked(expected)
            target = structure(config)
            target["navigation"] = apply_patches(target["navigation"], patches)
            validate_navigation(target)
            return await self._reconcile(config, target, dry_run=dry_run)

    async def restore(self, record: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        """Caller already holds the profile mutation lock."""
        if "after_structure" not in record:
            raise StudioError("UNRESOLVED_OPERATION", "Missing read-back; inspect before recovery")
        if record.get("after_report") != record.get("before_report"):
            raise StudioError(
                "UNRESOLVED_SIDE_EFFECT",
                "Report resources changed; inspect before lifecycle recovery",
            )
        config = await self._read()
        current = structure(config)
        if current == record["before_structure"]:
            return {"status": "unchanged", "restored_operation_id": record["operation_id"]}
        if current != record["after_structure"]:
            raise StudioError("VERSION_CONFLICT", "Pages/navigation changed after this operation")
        before = record["before_structure"]
        created, deleted = set(record["create_page_ids"]), set(record["delete_page_ids"])
        if (
            not (set(current["pages"]) - set(before["pages"])) <= created
            or not (set(before["pages"]) - set(current["pages"])) <= deleted
        ):
            raise StudioError(
                "UNRESOLVED_SIDE_EFFECT",
                "Observed page additions/removals are not owned by this operation",
            )
        # A read-back snapshot can include somebody else's concurrent navigation
        # edit. Matching that snapshot later is not authority to undo their edit.
        affected = created | deleted
        prior_nav = remove_navigation_pages(before["navigation"], affected)
        intended_nav = remove_navigation_pages(record["intended_structure"]["navigation"], affected)
        observed_nav = remove_navigation_pages(current["navigation"], affected)
        if observed_nav not in (prior_nav, intended_nav):
            raise StudioError(
                "UNRESOLVED_SIDE_EFFECT",
                "Navigation changes cannot be attributed to this operation",
            )
        result = await self._reconcile(config, record["before_structure"], dry_run=dry_run)
        result["restored_operation_id"] = record["operation_id"]
        return result

    async def _reconcile(
        self,
        config: dict[str, Any],
        target: dict[str, Any],
        *,
        dry_run: bool,
        created_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        before = structure(config)
        validate_navigation(target)
        if before == target:
            return {"status": "unchanged", "structure_fingerprint": fingerprint(before)}
        # Lifecycle restoration must not silently become a page-content overwrite.
        for page_id in set(before["pages"]) & set(target["pages"]):
            if before["pages"][page_id] != target["pages"][page_id]:
                raise StudioError(
                    "INVALID_RESTORE", "Common page content differs; restore its edit first"
                )
        create_ids = [
            p["pageId"]
            for p in page_items(target["navigation"])
            if p["pageId"] not in before["pages"]
        ]
        delete_ids = [p for p in before["pages"] if p not in target["pages"]]
        plan = {
            "create_page_ids": create_ids,
            "delete_page_ids": delete_ids,
            "navigation_before": before["navigation"],
            "navigation_after": target["navigation"],
            "atomic_scope": "individual_request",
            "created_page_ids": created_ids or [],
        }
        if dry_run:
            return {"status": "planned", **plan, "before_fingerprint": fingerprint(before)}
        operation_id = str(uuid.uuid4())
        record = {
            "operation_id": operation_id,
            "report_id": self.studio.config.report_id,
            "kind": "page_lifecycle",
            "started_at": datetime.now(UTC).isoformat(),
            "state": "prepared",
            "before_structure": before,
            "before_report": copy.deepcopy(config["report"]),
            "intended_structure": target,
            "steps": [],
            **plan,
        }
        self.studio._journal(operation_id, record)
        expected = before
        failure = None
        try:
            for phase in ("create", "delete", "navigation"):
                current = await self._read()
                if structure(current) != expected:
                    raise StudioError(
                        "VERSION_CONFLICT", "Pages/navigation changed before next request"
                    )
                predicted = copy.deepcopy(expected)
                body: dict[str, Any] = {
                    "reportId": self.studio.config.report_id,
                    "sessionId": operation_id,
                }
                if phase == "create" and create_ids:
                    metadata = {p["pageId"]: p for p in page_items(target["navigation"])}
                    body["pages"] = [
                        {**copy.deepcopy(metadata[p]), **copy.deepcopy(target["pages"][p])}
                        for p in create_ids
                    ]
                    body["position"] = {"index": len(expected["navigation"]["navItems"])}
                    for p in create_ids:
                        predicted["pages"][p] = copy.deepcopy(target["pages"][p])
                        predicted["navigation"]["navItems"].append({"page": metadata[p]})
                    endpoint = "createPages"
                elif phase == "delete" and delete_ids:
                    body["pageIds"] = delete_ids
                    predicted["pages"] = {
                        p: v for p, v in predicted["pages"].items() if p not in delete_ids
                    }
                    predicted["navigation"] = remove_navigation_pages(
                        predicted["navigation"], set(delete_ids)
                    )
                    endpoint = "deletePages"
                elif phase == "navigation" and expected["navigation"] != target["navigation"]:
                    body.update(
                        navigationInfo=target["navigation"], version=str(current["version"])
                    )
                    predicted["navigation"] = target["navigation"]
                    endpoint = "updateReportNavigationInfo"
                else:
                    continue
                step = {"endpoint": endpoint, "state": "dispatched"}
                record["steps"].append(step)
                record["state"] = "dispatched"
                self.studio._journal(operation_id, record)
                await self.studio.session.post(endpoint, body)
                actual = await self._read()
                observed = structure(actual)
                mismatches = assert_subset(predicted, observed)
                if set(predicted["pages"]) != set(observed["pages"]):
                    mismatches.append("/pages")
                # Existing pages and report resources must not change as a side effect.
                if any(
                    expected["pages"][p] != observed["pages"].get(p)
                    for p in expected["pages"]
                    if p not in delete_ids
                ):
                    mismatches.append("/unchanged_pages")
                if actual["report"] != config["report"]:
                    mismatches.append("/report")
                if mismatches:
                    record["mismatches"] = mismatches
                    raise StudioError(
                        "VERIFICATION_FAILED", "Request did not preserve intended structure"
                    )
                expected = observed
                step["state"] = "verified"
                record["state"] = "partially_applied"
                record["after_structure"] = observed
                self.studio._journal(operation_id, record)
        except StudioError as exc:
            failure = exc
        try:
            final_config = await self._read()
            actual = structure(final_config)
            record["after_structure"] = actual
            record["after_report"] = final_config["report"]
            matches = (
                set(actual["pages"]) == set(target["pages"])
                and actual["navigation"] == target["navigation"]
                and not assert_subset(target["pages"], actual["pages"])
                and final_config["report"] == config["report"]
                and all(
                    before["pages"][p] == actual["pages"].get(p)
                    for p in before["pages"]
                    if p in target["pages"]
                )
            )
            record["state"] = (
                "verified"
                if matches and not record.get("mismatches")
                else "rejected"
                if actual == before and final_config["report"] == config["report"]
                else "partially_applied"
            )
        except Exception:
            record["state"] = "outcome_unknown"
        if failure:
            record["dispatch_error"] = failure.code
        self.studio._journal(operation_id, record)
        return {
            "status": record["state"],
            "operation_id": operation_id,
            "structure_fingerprint": fingerprint(record["after_structure"])
            if "after_structure" in record
            else None,
            "dispatch_error": record.get("dispatch_error"),
            "mismatches": record.get("mismatches", []),
            **plan,
        }
