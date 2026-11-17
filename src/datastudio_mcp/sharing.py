"""Datasource sharing: who can view or edit a reusable datasource.

Reads go through the Share dialog's own endpoint (getSharing). Writes drive the Share
dialog itself, as a person does: Google guards updateSharing with a reCAPTCHA token
that only the dialog obtains, so the MCP never builds that request.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.async_api import Page
from pydantic import BaseModel, ConfigDict, Field

from .browser import ORIGINS, StudioError

ROLES = {"viewer": "roles/datastudio.viewer", "editor": "roles/datastudio.editor"}
NAMES = {role: name for name, role in ROLES.items()} | {"roles/datastudio.owner": "owner"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATASOURCE = 1  # datastudioEntity.type of a datasource in the sharing API.
DIALOG = "[role=dialog]"


class SharingMember(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=3, max_length=320)
    role: Literal["viewer", "editor"]


class Sharing:
    def __init__(self, studio):
        self.studio = studio

    async def get(self, source_id: str) -> dict[str, Any]:
        payload = await self.studio.session.post(
            "getSharing", {"datastudioEntity": {"uuid": source_id, "type": DATASOURCE}})
        data = payload.get("sharingData")
        if not isinstance(data, dict) or not isinstance(data.get("sharingData"), list):
            raise StudioError("INVALID_RESPONSE", "getSharing returned an unexpected shape")
        config = data.get("sharingConfig") or {}
        return {
            "datasource_id": source_id,
            "members": [
                {"email": m["member"]["id"], "name": m["member"].get("displayName"),
                 "role": NAMES.get(m["role"], m["role"])}
                for m in data["sharingData"]
            ],
            "can_edit_sharing": bool(data.get("canEdit")),
            "is_owner": bool(data.get("isOwner")),
            "link_sharing": bool(config.get("allowPublicLinkSharing")),
            # The server's own ACL version: any change elsewhere makes it stale.
            "fingerprint": data.get("etag"),
        }

    async def grant(
        self,
        source_id: str,
        members: list[dict[str, str]],
        expected: str,
        *,
        notify: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Give people without access viewer or editor access through the Share dialog.

        Google checks each update with reCAPTCHA: a long series of automated shares can be
        refused (SHARING_REJECTED, RECAPTCHA_VERIFICATION_FAILED); a person then shares it.
        """
        wanted: dict[str, str] = {}
        for member in members:
            email, role = member.get("email", "").strip().lower(), member.get("role")
            if not EMAIL.fullmatch(email):
                raise StudioError("INVALID_MEMBER", f"Not an email address: {email!r}")
            if role not in ROLES:
                raise StudioError("INVALID_ROLE", "role is viewer or editor")
            if wanted.get(email, role) != role:
                raise StudioError("INVALID_MEMBER", f"Two roles for {email}")
            wanted[email] = role
        if not wanted:
            raise StudioError("INVALID_MEMBER", "Name at least one member")
        async with self.studio.mutation_lock():
            current = await self.get(source_id)
            if current["fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Sharing changed since inspection")
            if not current["can_edit_sharing"]:
                raise StudioError("SHARING_NOT_ALLOWED", "This account cannot share the source")
            before = {m["email"].lower(): m["role"] for m in current["members"]}
            if any(before.get(email) == "owner" for email in wanted):
                raise StudioError("INVALID_MEMBER", "The owner's role cannot change here")
            changes = {e: r for e, r in wanted.items() if before.get(e) != r}
            if any(e in before for e in changes):
                # The dialog's Send only adds people; changing a member's role or removing
                # one is done by a person in the dialog's access list.
                raise StudioError("ROLE_CHANGE_UNSUPPORTED",
                                  "Only people without access can be added; change existing "
                                  "roles in the Share dialog")
            plan = {"datasource_id": source_id, "before": current["members"],
                    "changes": [{"email": e, "from": before.get(e), "to": r}
                                for e, r in changes.items()],
                    "notify": notify}
            if not changes:
                return {"status": "unchanged", **plan}
            if dry_run:
                return {"status": "planned", **plan}
            record = self.studio.journal_action("datasource_sharing", **plan)
            await self._dialog(source_id, changes, notify)
            fresh = await self.get(source_id)
            after = {m["email"].lower(): m["role"] for m in fresh["members"]}
            record["state"] = ("verified" if all(after.get(e) == r for e, r in changes.items())
                               else "verification_failed")
            record["after"] = fresh["members"]
            self.studio._journal(record["operation_id"], record)
            return {"status": record["state"], "operation_id": record["operation_id"],
                    "members": fresh["members"], "fingerprint": fresh["fingerprint"], **plan}

    async def _dialog(self, source_id: str, changes: dict[str, str], notify: bool) -> None:
        for role in ROLES:
            emails = [e for e, r in changes.items() if r == role]
            if emails:
                await self._send(source_id, emails, role, notify)

    async def _send(self, source_id: str, emails: list[str], role: str, notify: bool) -> None:
        """One Share dialog pass: add the people, pick the role and notification, Send."""
        home = await self.studio.session.page()
        parsed = urlparse(home.url)
        prefix = re.match(r"^/u/\d+", parsed.path)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in ORIGINS:
            raise StudioError("OUT_OF_SCOPE", "No Data Studio tab to share from")
        timeout = self.studio.config.timeout_ms
        tab: Page = await home.context.new_page()
        try:
            await tab.goto(f"{origin}{prefix[0] if prefix else ''}/datasources/{source_id}",
                           wait_until="domcontentloaded", timeout=timeout)
            await tab.locator("button:has-text('Share')").first.click(timeout=timeout)
            field = tab.locator(f"{DIALOG} input").first
            for email in emails:
                await field.click(timeout=timeout)
                await field.fill(email)
                # The directory suggestion turns the text into a person chip.
                await tab.wait_for_timeout(1500)
                await field.press("Enter")
                await tab.wait_for_timeout(500)
            send = tab.locator(f"{DIALOG} button:has-text('Send')").first
            await send.wait_for(timeout=timeout)
            label = role.capitalize()
            picker = tab.locator(f"{DIALOG} button:has-text('Viewer'), "
                                 f"{DIALOG} button:has-text('Editor')").first
            if (await picker.inner_text()).strip() != label:
                await picker.click(timeout=timeout)
                await tab.locator(f"[role=menuitem]:has-text('{label}'), "
                                  f"[role=menuitemradio]:has-text('{label}')").first.click(
                    timeout=timeout)
            box = tab.locator(f"{DIALOG} input[type=checkbox]").first
            if await box.is_checked() != notify:
                await tab.locator(f"{DIALOG} :text('Notify people')").first.click(timeout=timeout)
            if await box.is_checked() != notify or (await picker.inner_text()).strip() != label:
                raise StudioError("SHARE_DIALOG_CHANGED", "Role or notification did not apply")
            async with tab.expect_response(lambda r: "/updateSharing" in r.url,
                                           timeout=timeout) as answer:
                await send.click(timeout=timeout)
            response = await answer.value
            if response.status != 200:
                reason = " ".join((await response.text()).split())[:300]
                raise StudioError("SHARING_REJECTED",
                                  f"updateSharing returned HTTP {response.status}: {reason}",
                                  outcome_unknown=True)
        except StudioError:
            raise
        except Exception as exc:  # Playwright: the dialog no longer looks as expected.
            raise StudioError("SHARE_DIALOG_CHANGED", "Share dialog did not respond as expected",
                              outcome_unknown=True) from exc
        finally:
            await tab.close()
