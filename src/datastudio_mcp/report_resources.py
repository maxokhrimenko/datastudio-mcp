"""Guarded resource/report-filter writes with full before-state and read-back."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .browser import StudioError
from .document import fingerprint, json_equal

if TYPE_CHECKING:
    from .service import Studio


def report_content(config: dict) -> dict:
    # Native report requests do not write server-maintained modifier identity.
    return {key: copy.deepcopy(value) for key, value in config["report"].items()
            if key != "lastModifierGaiaId"}


def protected_content(config: dict) -> dict:
    return {"navigation": config.get("navigationInfo"),
            "conditions": config.get("chartCondition", []),
            "pages": [{"pageId": p["pageId"], "page": p["page"],
                       "chartCondition": p.get("chartCondition", [])} for p in config["page"]]}


class ReportResources:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def write(
        self, before: dict, target: dict, *, restoring: bool = False,
        filter_bindings_only: bool = False, settings: bool = False,
    ) -> dict:
        """Write report content. settings=True allows any report-level content (attributes,
        properties, resources, report components) to change; pages/navigation never do."""
        if settings or restoring:
            pass
        elif filter_bindings_only:
            expected = report_content(before)
            try:
                ids = target["propertyConfig"]["reportProperty"]["filters"]
                if not isinstance(ids, list) or any(not isinstance(f, str) for f in ids):
                    raise ValueError("Invalid filter list")
                expected["propertyConfig"]["reportProperty"]["filters"] = copy.deepcopy(ids)
            except (KeyError, TypeError, ValueError) as exc:
                raise StudioError("INVALID_REPORT", "Report filter binding is malformed") from exc
            if not json_equal(expected, target):
                raise StudioError("UNEXPECTED_SIDE_EFFECT", "Report filter edit changed other content")
        elif not json_equal(
            {k: v for k, v in report_content(before).items() if k != "resource"},
            {k: v for k, v in target.items() if k != "resource"},
        ):
            raise StudioError("UNEXPECTED_SIDE_EFFECT", "Resource edit changed other report content")
        if json_equal(report_content(before), target):
            return {"status": "unchanged"}
        operation_id = str(uuid.uuid4())
        record = {"kind": "report_resources", "operation_id": operation_id,
                  "report_id": self.studio.config.report_id,
                  "started_at": datetime.now(UTC).isoformat(), "state": "prepared",
                  "before": before, "intended_report": target,
                  "edit_scope": "report_filters" if filter_bindings_only
                  else "report_settings" if settings else "resources"}
        self.studio._journal(operation_id, record)
        fresh = (await self.studio.report())["reportConfig"]
        if (fresh["version"] != before["version"]
                or not json_equal(report_content(fresh), report_content(before))
                or not json_equal(protected_content(fresh), protected_content(before))):
            record["state"] = "conflict"
            self.studio._journal(operation_id, record)
            raise StudioError("VERSION_CONFLICT", "Report changed before resource dispatch")
        request = copy.deepcopy(target)
        request["chartCondition"] = copy.deepcopy(before.get("chartCondition", []))
        if "navigationInfo" in before:
            request["navigationInfo"] = copy.deepcopy(before["navigationInfo"])
        record["state"] = "dispatched"
        self.studio._journal(operation_id, record)
        try:
            await self.studio.session.post("updateReport", {
                "reportId": self.studio.config.report_id, "version": before["version"],
                "sessionId": operation_id, "reportRequest": request})
        except StudioError as exc:
            record["dispatch_error"] = exc.code
        try:
            actual = (await self.studio.report())["reportConfig"]
            record["after"] = actual
            mismatches = []
            if not json_equal(report_content(actual), target):
                mismatches.append("/report")
            if not json_equal(protected_content(actual), protected_content(before)):
                mismatches.append("/protected_content")
            record["mismatches"] = mismatches
            record["state"] = "verification_failed" if mismatches else "verified"
        except Exception:
            record["state"] = "outcome_unknown"
        self.studio._journal(operation_id, record)
        return {"status": record["state"], "operation_id": operation_id,
                "dispatch_error": record.get("dispatch_error"),
                "mismatches": record.get("mismatches", []),
                "report_content_fingerprint": fingerprint(record["after"]["report"])
                if "after" in record else None,
                "publication": "Not automatically published; publication state not inferred"}

    async def restore(self, record: dict, *, dry_run: bool = True) -> dict:
        current = (await self.studio.report())["reportConfig"]
        target = report_content(record["before"])
        if "after" in record and not json_equal(
            protected_content(record["before"]), protected_content(record["after"])
        ):
            raise StudioError(
                "UNRESOLVED_SIDE_EFFECT",
                "Pages/navigation/conditions changed during resource write; inspect their recovery separately",
            )
        if json_equal(report_content(current), target):
            return {"status": "unchanged", "restored_operation_id": record["operation_id"]}
        if "after" not in record:
            raise StudioError("UNRESOLVED_OPERATION", "Missing resource read-back; inspect before undo")
        if (not json_equal(report_content(current), report_content(record["after"]))
                or not json_equal(protected_content(current), protected_content(record["after"]))):
            raise StudioError("VERSION_CONFLICT", "Report or consumers changed after resource edit")
        if dry_run:
            return {"status": "planned", "restored_operation_id": record["operation_id"],
                    "before": report_content(current), "after": target}
        return {**await self.write(
                    current, target, restoring=True,
                    filter_bindings_only=record.get("edit_scope") == "report_filters",
                    settings=record.get("edit_scope") == "report_settings",
                ),
                "restored_operation_id": record["operation_id"]}
