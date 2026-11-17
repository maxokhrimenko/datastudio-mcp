"""Report operations, serialized mutation journal, and read-back verification."""

from __future__ import annotations

import asyncio
import copy
import fcntl
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .browser import ReportSession, StudioError
from .datasources import Datasources, source_state
from .document import (
    Patch,
    apply_patches,
    assert_subset,
    find_component,
    find_page,
    fingerprint,
    json_equal,
)
from .pages import Pages, structure


class ComponentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_id: str
    patches: list[Patch]


CREDENTIAL_OWNER_TYPES = {1: "owner", 2: "service_account"}


def credentials(raw: dict[str, Any]) -> dict[str, Any]:
    """How the datasource reaches its data. Viewer's credentials disable delegated access."""
    published = raw.get("published") or raw.get("draft") or {}
    if published.get("delegatedAccessEnabled") is False:
        kind = "viewer"
    else:
        kind = CREDENTIAL_OWNER_TYPES.get(raw.get("credentialOwnerType"), "unknown")
    return {
        "type": kind,
        "account": raw.get("storedCredentialOwnerDisplayName") if kind != "viewer" else None,
    }


# Journalled actions with no automatic undo, and how a person reverses each one.
ACTION_UNDO = {
    "report_publish": "Republish an earlier revision with report_publish(revision_number)",
    "publishing_mode": "Set the previous mode with report_publishing_mode",
    "revision_name": "Revision names are history annotations; nothing to undo",
    "revision_restore": "Restore the revision listed as before_revision with revision_restore",
    "report_rename": "Rename back with report_rename (before is in the journal)",
    "datasource_detach": "Re-attach with datasource_add_to_report",
    "datasource_create": "Remove it from the report with datasources_remove_from_report",
    "datasource_rename": "Rename back with datasource_rename (before is in the journal)",
    "datasource_sharing": "Remove the added people in the datasource Share dialog (by a person)",
}


class Studio:
    def __init__(self, session: ReportSession):
        self.session = session
        self.config = session.config
        self._lock = asyncio.Lock()

    @property
    def report_scoped(self) -> bool:
        return self.config.report_id is not None

    async def report(self) -> dict[str, Any]:
        if not self.report_scoped:
            raise StudioError(
                "NO_REPORT_SCOPE",
                "This operation needs a report; pass report_id or call report_open",
            )
        report = await self.session.post(
            "getReport",
            {
                "reportId": self.config.report_id,
                "draftRequestMode": 1,
                "includePages": True,
                "updateViewedDate": False,
                "updateFirestore": False,
            },
        )
        config = report.get("reportConfig", {})
        if config.get("shareable", {}).get("id") != self.config.report_id:
            raise StudioError(
                "INVALID_REPORT", "Server returned another report or an incomplete response"
            )
        if not isinstance(config.get("report"), dict) or not isinstance(config.get("page"), list):
            raise StudioError(
                "INVALID_REPORT", "Report content missing; refusing to treat it as empty"
            )
        return report

    async def consumers(self) -> dict[str, Any] | None:
        """The saved report whose references a datasource edit is checked against.

        None in the datasource scope: no report is in view, so no consumer is scanned.
        """
        return (await self.report())["reportConfig"] if self.report_scoped else None

    async def sources(self) -> list[dict[str, Any]]:
        if not self.report_scoped:
            result = await self.session.post("getShareableList", {"type": [1], "trashed": False})
            listed = result.get("shareableConfig")
            if not isinstance(listed, list):
                raise StudioError("INVALID_RESPONSE", "Datasource inventory is missing")
            return [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "owner": item.get("ownerDisplayName"),
                    "modified": item.get("modifiedDate"),
                }
                for item in listed
            ]
        result = await self.session.post(
            "getClaimedDatasources", {"reportId": self.config.report_id}
        )
        sources = result.get("claimedDatasources")
        if not isinstance(sources, list):
            raise StudioError("INVALID_RESPONSE", "Attached datasource inventory is missing")
        return [source["basicEntityInfo"] for source in sources]

    async def datasource(self, source_id: str) -> dict[str, Any]:
        if self.report_scoped:
            source = next((s for s in await self.sources() if s["id"] == source_id), None)
            if source is None:
                raise StudioError(
                    "OUT_OF_SCOPE", "Datasource is not attached to the configured report"
                )
        raw = await self.session.post("getBlockDatasource", {"datasourceId": source_id})
        if not self.report_scoped:
            source = {"id": source_id, "isEmbedded": bool(raw.get("embeddedInReportId"))}
        if raw.get("draft", {}).get("datasourceId") != source_id:
            raise StudioError(
                "INVALID_DATASOURCE", "Datasource draft is missing or belongs to another source"
            )
        result = {
            "source": source,
            "draft": raw["draft"],
            "published": raw.get("published"),
            "editable": raw.get("isEditable", False),
            "embedded_in_report_id": raw.get("embeddedInReportId"),
            "credentials": credentials(raw),
        }
        result["fingerprint"] = fingerprint(source_state(result))
        return result

    async def inventory(self) -> dict[str, Any]:
        raw = await self.report()
        config = raw["reportConfig"]
        return {
            "report_id": self.config.report_id,
            "name": config["shareable"]["name"],
            "version": config["version"],
            "navigation": config.get("navigationInfo"),
            "structure_fingerprint": fingerprint(structure(config)),
            "pages": [
                {
                    "page_id": p["pageId"],
                    "version": p["version"],
                    "fingerprint": fingerprint(p["page"]),
                    "component_count": len(p["page"].get("componentConfig", [])),
                }
                for p in config["page"]
            ],
            "report_component_count": len(config["report"].get("componentConfig", [])),
            "resources": list(config["report"].get("resource", {})),
        }

    async def refresh(self, page_id: str | None = None) -> dict[str, Any]:
        # Navigation must not interrupt a mutation or its read-back in another tool.
        async with self.mutation_lock():
            if page_id is not None:
                find_page(await self.report(), page_id)
            return await self.session.refresh(page_id)

    def operation(self, operation_id: str) -> dict[str, Any]:
        try:
            if str(uuid.UUID(operation_id)) != operation_id:
                raise ValueError("Noncanonical ID")
            record = json.loads((self.config.state_dir / f"{operation_id}.json").read_text())
        except (ValueError, OSError) as exc:
            raise StudioError("NOT_FOUND", "Operation journal not found") from exc
        if record.get("report_id") != self.config.report_id:
            raise StudioError("OUT_OF_SCOPE", "Operation journal belongs to another report")
        return record

    async def restore_operation(self, operation_id: str, *, dry_run: bool = True) -> dict[str, Any]:
        async with self.mutation_lock():
            record = self.operation(operation_id)
            if record.get("kind") == "report_resources":
                from .report_resources import ReportResources

                return await ReportResources(self).restore(record, dry_run=dry_run)
            if record.get("kind") == "checkbox_interaction":
                from .checkbox import Checkbox

                return await Checkbox(self).restore(record, dry_run=dry_run)
            if record.get("kind") == "control_filter_interaction":
                from .runtime_controls import RuntimeControls

                return await RuntimeControls(self).restore(record, dry_run=dry_run)
            if record.get("kind") == "datasource_edit":
                return await Datasources(self).restore(record, dry_run=dry_run)
            if record.get("kind") in ACTION_UNDO:
                raise StudioError("NOT_RESTORABLE", ACTION_UNDO[record["kind"]])
            if record.get("kind") == "datasource_credentials":
                raise StudioError(
                    "NOT_RESTORABLE",
                    "Credential changes are not replayed; set the credential explicitly",
                )
            if record.get("kind") == "page_lifecycle":
                return await Pages(self).restore(record, dry_run=dry_run)
            if "after" not in record:
                raise StudioError(
                    "UNRESOLVED_OPERATION",
                    "Read-back is missing; inspect current state before recovery",
                )
            current_report = await self.report()
            report_guard = record.get("expected_report_fingerprint")
            if (
                report_guard is not None
                and fingerprint(current_report["reportConfig"]["report"]) != report_guard
            ):
                raise StudioError(
                    "VERSION_CONFLICT", "Report/filter resources changed after page operation"
                )
            current = find_page(current_report, record["page_id"])
            target = record["before"]["page"]
            target_conditions = record["before"].get("chartCondition", [])
            if json_equal(current["page"], target) and json_equal(
                current.get("chartCondition", []), target_conditions
            ):
                return {"status": "unchanged", "restored_operation_id": operation_id}
            if not json_equal(current["page"], record["after"]["page"]) or not json_equal(
                current.get("chartCondition", []), record["after"].get("chartCondition", [])
            ):
                raise StudioError(
                    "VERSION_CONFLICT",
                    "Page changed after this operation; restoring would overwrite later work",
                )
            if dry_run:
                return {
                    "status": "planned",
                    "page_id": record["page_id"],
                    "restored_operation_id": operation_id,
                    "before": current["page"],
                    "after": target,
                    "chart_conditions_after": target_conditions,
                }
            result = await self._write_page(
                current, target, target_conditions, expected_report_fingerprint=report_guard
            )
            result["restored_operation_id"] = operation_id
            return result

    @asynccontextmanager
    async def mutation_lock(self):
        async with self._lock:
            # OS releases the advisory lock after process death. The lock belongs
            # to the browser profile, so two MCP clients cannot race its editor.
            path = self.config.profile_dir / ".datastudio-mcp.lock"
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise StudioError(
                        "BROWSER_BUSY", "Another MCP process is editing this browser profile"
                    ) from exc
                yield
            finally:
                os.close(fd)

    def journal_action(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Durable record of a write whose undo is another tool call (see ACTION_UNDO)."""
        record = {"kind": kind, "operation_id": str(uuid.uuid4()),
                  "report_id": self.config.report_id,
                  "started_at": datetime.now(UTC).isoformat(), "state": "dispatched", **fields}
        self._journal(record["operation_id"], record)
        return record

    def _journal(self, operation_id: str, record: dict[str, Any]) -> Path:
        # Persist newly created directory entries too: fsync(file) alone does
        # not make either its parent directory or an atomic rename durable.
        missing = []
        directory = self.config.state_dir
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            self._sync_directory(directory.parent)
        path = self.config.state_dir / f"{operation_id}.json"
        temporary = path.with_suffix(".pending")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            self._sync_directory(self.config.state_dir)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    async def patch_components(
        self,
        page_id: str,
        changes: list[ComponentChange],
        expected_fingerprint: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not changes or len({c.component_id for c in changes}) != len(changes):
            raise StudioError("INVALID_BATCH", "Provide nonempty changes with unique component IDs")
        async with self.mutation_lock():
            raw = await self.report()
            frame = find_page(raw, page_id)
            before = frame["page"]
            if fingerprint(before) != expected_fingerprint:
                raise StudioError(
                    "VERSION_CONFLICT", "Page differs from the state used to prepare these changes"
                )
            after = copy.deepcopy(before)
            diffs = []
            for change in changes:
                target = find_component(after, change.component_id)
                updated = apply_patches(target, change.patches)
                if updated.get("componentId") != target["componentId"]:
                    raise StudioError(
                        "IMMUTABLE_ID",
                        "Use component creation/deletion instead of changing its identity",
                    )
                if updated.get("datasourceId") != target.get("datasourceId"):
                    raise StudioError(
                        "INVALID_BINDING",
                        "Changing datasource requires coordinated field remapping",
                    )
                diffs.append(
                    {
                        "component_id": change.component_id,
                        "before": copy.deepcopy(target),
                        "after": updated,
                    }
                )
                target.clear()
                target.update(updated)
            if json_equal(before, after):
                return {"status": "unchanged", "fingerprint": expected_fingerprint}
            if dry_run:
                return {
                    "status": "planned",
                    "page_id": page_id,
                    "changes": diffs,
                    "before_fingerprint": expected_fingerprint,
                    "atomic_scope": "page",
                }
            return await self._write_page(frame, after)

    async def _write_page(
        self,
        frame: dict[str, Any],
        after: dict[str, Any],
        after_conditions: list[dict[str, Any]] | None = None,
        *,
        expected_report_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        conditions = (
            copy.deepcopy(frame.get("chartCondition", []))
            if after_conditions is None
            else copy.deepcopy(after_conditions)
        )
        operation_id = str(uuid.uuid4())
        record = {
            "operation_id": operation_id,
            "report_id": self.config.report_id,
            "page_id": frame["pageId"],
            "started_at": datetime.now(UTC).isoformat(),
            "state": "prepared",
            "before": frame,
            "intended_page": after,
            "intended_chart_conditions": conditions,
        }
        if expected_report_fingerprint is not None:
            record["expected_report_fingerprint"] = expected_report_fingerprint
        self._journal(operation_id, record)
        # Re-read after disk persistence: never knowingly overwrite a changed page.
        fresh_report = await self.report()
        fresh = find_page(fresh_report, frame["pageId"])
        if (
            (
                expected_report_fingerprint is not None
                and fingerprint(fresh_report["reportConfig"]["report"])
                != expected_report_fingerprint
            )
            or fresh["version"] != frame["version"]
            or not json_equal(fresh["page"], frame["page"])
            or not json_equal(fresh.get("chartCondition", []), frame.get("chartCondition", []))
        ):
            record["state"] = "conflict"
            self._journal(operation_id, record)
            raise StudioError("VERSION_CONFLICT", "Page changed before dispatch")
        request = copy.deepcopy(after)
        request.update(
            {
                "fieldMask": [4, 3, 5]
                if not json_equal(conditions, frame.get("chartCondition", []))
                else [4, 3],
                "chartCondition": conditions,
                "pageId": frame["pageId"],
            }
        )
        record["state"] = "dispatched"
        self._journal(operation_id, record)
        failure = None
        try:
            await self.session.post(
                "updatePage",
                {
                    "reportId": self.config.report_id,
                    "pageId": frame["pageId"],
                    "pageRequest": request,
                    "version": frame["version"],
                    "sessionId": operation_id,
                },
            )
        except StudioError as exc:
            failure = exc
        try:
            actual_report = await self.report()
            actual = find_page(actual_report, frame["pageId"])
            mismatches = assert_subset(after, actual["page"])
            if (
                expected_report_fingerprint is not None
                and fingerprint(actual_report["reportConfig"]["report"])
                != expected_report_fingerprint
            ):
                mismatches.append("/report")
            if not json_equal(conditions, actual.get("chartCondition", [])):
                mismatches.append("/chartCondition")
            # Verify explicit removals too; subset comparison alone cannot do so.
            for path in removed_paths(frame["page"], after):
                if path_exists(actual["page"], path):
                    mismatches.append(
                        "/"
                        + "/".join(str(key).replace("~", "~0").replace("/", "~1") for key in path)
                    )
            record["after"] = actual
            record["state"] = "verified" if not mismatches else "verification_failed"
            if failure and mismatches and json_equal(actual["page"], frame["page"]):
                # The server refused the write and the saved page is the one read before.
                record["state"] = "rejected"
            record["mismatches"] = mismatches
        except Exception:
            record["state"] = "outcome_unknown"
        if failure:
            record["dispatch_error"] = failure.code
            record["dispatch_message"] = str(failure)
        self._journal(operation_id, record)
        return {
            "dispatch_message": record.get("dispatch_message"),
            "status": record["state"],
            "operation_id": operation_id,
            "page_id": frame["pageId"],
            "mismatches": record.get("mismatches", []),
            "dispatch_error": record.get("dispatch_error"),
            "fingerprint": fingerprint(record["after"]["page"]) if "after" in record else None,
            "publication": "not_yet_characterized",
        }


def removed_paths(
    before: Any, after: Any, prefix: tuple[str | int, ...] = ()
) -> list[tuple[str | int, ...]]:
    if isinstance(before, list) and isinstance(after, list):
        return [
            path
            for index, (old, new) in enumerate(zip(before, after, strict=False))
            for path in removed_paths(old, new, (*prefix, index))
        ]
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    paths = []
    for key, value in before.items():
        path = (*prefix, key)
        if key not in after:
            paths.append(path)
        else:
            paths.extend(removed_paths(value, after[key], path))
    return paths


def path_exists(value: Any, path: tuple[str | int, ...]) -> bool:
    for key in path:
        if isinstance(key, int):
            if not isinstance(value, list) or not 0 <= key < len(value):
                return False
        elif not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return True
