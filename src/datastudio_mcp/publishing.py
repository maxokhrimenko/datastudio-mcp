"""Publishing and version history, as the Draft/Published menu and File > Version history.

With manual publishing, edits (UI or MCP) change the report draft; viewers see the
published revision until it is published. Revisions are the version history of a report
(entity type 1) or a datasource (entity type 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from .browser import StudioError
from .document import json_equal

if TYPE_CHECKING:
    from .service import Studio

ENTITY_TYPES = {"report": 1, "datasource": 2}
MODES = {"auto": 0, "manual": 1}


def content(config: dict[str, Any]) -> dict[str, Any]:
    """What viewers see: report-level content, navigation and every page."""
    return {
        "report": {k: v for k, v in (config.get("report") or {}).items()
                   if k != "lastModifierGaiaId"},
        "navigation": config.get("navigationInfo"),
        "conditions": config.get("chartCondition", []),
        "pages": {p["pageId"]: [p.get("page"), p.get("chartCondition", [])]
                  for p in config.get("page", [])},
    }


class Publishing:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def _state(self) -> dict[str, Any]:
        raw = await self.studio.session.post(
            "getReport",
            {"reportId": self.studio.config.report_id, "draftRequestMode": 2,
             "includePages": True, "updateViewedDate": False, "updateFirestore": False},
        )
        number = raw.get("publishedReportRevisionNumber")
        published = (raw.get("publishedReportRevision") or {}).get("reportConfig") or {}
        draft = raw.get("reportConfig") or {}
        # The server's hasChangesToPublish stays true after publishing; compare content.
        changed = number is not None and not json_equal(content(draft), content(published))
        return {
            "publishing": "manual" if number is not None else "auto",
            "published_revision": int(number) if number is not None else None,
            "has_changes_to_publish": changed,
            "published_pages": [p["pageId"] for p in published.get("page", [])],
        }

    async def status(self) -> dict[str, Any]:
        return {"report_id": self.studio.config.report_id, **await self._state()}

    async def publish(
        self, revision_number: int | None = None, *, dry_run: bool = True
    ) -> dict[str, Any]:
        """Publish the current draft (or republish an existing revision) to viewers."""
        async with self.studio.mutation_lock():
            before = await self._state()
            if before["publishing"] != "manual":
                raise StudioError("PUBLISHING_AUTO", "Report publishes automatically")
            if revision_number is None and not before["has_changes_to_publish"]:
                return {"status": "unchanged", **before}
            plan = {"publishes": revision_number or "current draft", "before": before}
            if dry_run:
                return {"status": "planned", **plan}
            body: dict[str, Any] = {"publishingMode": 1, "aiGenerated": False,
                                    "reportId": self.studio.config.report_id}
            if revision_number is not None:
                body["revisionNumber"] = str(revision_number)
            record = self.studio.journal_action("report_publish", before=before, request=body)
            response = await self.studio.session.post("publishReport", body)
            after = await self._state()
            ok = (after["published_revision"] or 0) > (before["published_revision"] or 0) or (
                revision_number is not None and after["published_revision"] == revision_number)
            if revision_number is None:
                ok = ok and not after["has_changes_to_publish"]
            record.update(state="verified" if ok else "verification_failed", after=after)
            self.studio._journal(record["operation_id"], record)
            return {"status": record["state"], "operation_id": record["operation_id"],
                    "revision": response.get("revisionNumber"), "after": after, **plan}

    async def set_mode(self, mode: Literal["auto", "manual"], *, dry_run: bool = True):
        """Switch publishing. auto publishes every saved change (current draft included)."""
        async with self.studio.mutation_lock():
            before = await self._state()
            if before["publishing"] == mode:
                return {"status": "unchanged", **before}
            if dry_run:
                return {"status": "planned", "mode": mode, "before": before,
                        "note": "auto publishes the current draft immediately" if mode == "auto"
                        else "later edits stay in the draft until report_publish"}
            record = self.studio.journal_action("publishing_mode", before=before, mode=mode)
            await self.studio.session.post(
                "publishReport",
                {"publishingMode": MODES[mode], "reportId": self.studio.config.report_id,
                 "aiGenerated": False},
            )
            after = await self._state()
            record.update(state="verified" if after["publishing"] == mode else
                          "verification_failed", after=after)
            self.studio._journal(record["operation_id"], record)
            return {"status": record["state"], "operation_id": record["operation_id"],
                    "before": before, "after": after}

    async def _target(
        self, entity: str, entity_id: str | None, allow_shared_source: bool = True
    ) -> str:
        """Only this report, or a datasource attached to it (shared ones on opt-in)."""
        if entity == "report":
            if entity_id not in (None, self.studio.config.report_id):
                raise StudioError("OUT_OF_SCOPE", "Use report_id to work on another report")
            return self.studio.config.report_id
        if not entity_id:
            raise StudioError("INVALID_REQUEST", "entity_id (datasource) is required")
        source = await self.studio.datasource(entity_id)  # Refuses unattached sources.
        if not source["source"].get("isEmbedded") and not allow_shared_source:
            raise StudioError(
                "SHARED_SOURCE", "Reusable datasource: pass allow_shared_source=true"
            )
        return entity_id

    async def revisions(
        self, entity: Literal["report", "datasource"] = "report", entity_id: str | None = None
    ) -> dict[str, Any]:
        target = await self._target(entity, entity_id)
        result = await self.studio.session.post(
            "getRevisions", {"entityId": target, "entityType": ENTITY_TYPES[entity]})
        editors = {e.get("key"): e.get("value") for e in result.get("editorInfo", [])}
        return {"entity": entity, "entity_id": target, "revisions": [
            {"revision_number": r.get("revisionNumber"),
             "name": (r.get("revisionAnnotation") or {}).get("name"),
             "created": r.get("createdTimestamp"), "published": r.get("isPublished"),
             "annotation": r.get("revisionAnnotation"),
             "editors": [(editors.get(e.get("userId")) or {}).get("displayName")
                         for e in (r.get("editorList") or {}).get("editor", [])]}
            for r in result.get("revisionInfo", [])]}

    async def name_current(self, name: str) -> dict[str, Any]:
        """File > Version history > Name current version."""
        if not 1 <= len(name) <= 200:
            raise StudioError("INVALID_NAME", "Name must be 1-200 characters")
        async with self.studio.mutation_lock():
            record = self.studio.journal_action("revision_name", name=name)
            await self.studio.session.post(
                "createNamedRevision",
                {"reportId": self.studio.config.report_id, "name": name, "aiGenerated": False})
            listed = await self.revisions()
            ok = any(r["name"] == name for r in listed["revisions"])
            record["state"] = "verified" if ok else "verification_failed"
            self.studio._journal(record["operation_id"], record)
            return {"status": record["state"], "operation_id": record["operation_id"],
                    "name": name}

    async def restore(
        self, revision_number: int, entity: Literal["report", "datasource"] = "report",
        entity_id: str | None = None, *, dry_run: bool = True,
        allow_shared_source: bool = False,
    ) -> dict[str, Any]:
        """Restore a revision as the new current draft (the history keeps the old one)."""
        target = await self._target(entity, entity_id, allow_shared_source)
        async with self.studio.mutation_lock():
            listed = await self.revisions(entity, target)
            numbers = {str(r["revision_number"]) for r in listed["revisions"]}
            if str(revision_number) not in numbers:
                raise StudioError("NOT_FOUND", "Revision is not in this entity's history")
            if dry_run:
                return {"status": "planned", "entity": entity, "entity_id": target,
                        "restores": revision_number}
            current = listed["revisions"][0]["revision_number"] if listed["revisions"] else None
            record = self.studio.journal_action(
                "revision_restore", entity=entity, entity_id=target,
                restores=revision_number, before_revision=current)
            await self.studio.session.post(
                "restoreRevision",
                {"entityId": target, "entityType": ENTITY_TYPES[entity],
                 "revisionNumber": str(revision_number)})
            after = await self.revisions(entity, target)
            newest = after["revisions"][0]["revision_number"] if after["revisions"] else None
            # Restoring appends a new head revision; its content is the restored one.
            record.update(state="verified" if newest != current else "verification_failed",
                          after_revision=newest)
            self.studio._journal(record["operation_id"], record)
        return {"status": record["state"], "operation_id": record["operation_id"],
                "entity": entity, "entity_id": target, "restored": revision_number,
                "new_head_revision": newest}

    async def links(self, page_id: str | None = None) -> dict[str, Any]:
        """Share > Get report link and File > Embed report: URLs and the embed snippet."""
        config = (await self.studio.report())["reportConfig"]
        attributes = config["report"].get("attributeConfig", {}).get("reportAttribute", {})
        page = next((p for p in config["page"] if p["pageId"] == page_id), None)
        if page_id is not None and page is None:
            raise StudioError("NOT_FOUND", "Page does not belong to this report")
        own = (page or {}).get("page", {}).get("attributeConfig", {}).get("pageAttribute", {})
        suffix = f"/page/{page_id}" if page_id else ""
        base = f"https://datastudio.google.com/reporting/{self.studio.config.report_id}{suffix}"
        embed = base.replace("/reporting/", "/embed/reporting/", 1)
        width = own.get("width") or attributes.get("width") or 1200
        height = own.get("height") or attributes.get("height") or 900
        return {"view": base, "edit": base + "/edit", "embed": embed,
                "embeddable": attributes.get("isEmbeddable", False),
                "iframe": f'<iframe width="{width}" height="{height}" src="{embed}" '
                          'frameborder="0" style="border:0" allowfullscreen '
                          'sandbox="allow-storage-access-by-user-activation allow-scripts '
                          'allow-same-origin allow-popups allow-popups-to-escape-sandbox">'
                          "</iframe>"}
