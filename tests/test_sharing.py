import asyncio
import contextlib

import pytest

from datastudio_mcp.browser import Config, StudioError
from datastudio_mcp.sharing import Sharing

SOURCE = "7a7a7a7a-8b8b-4c9c-8dad-aeaeaeaeaeae"
OWNER = "owner@example.com"


class Session:
    def __init__(self, root, members):
        self.config = Config(None, root, root / "journal")
        self.members = dict(members)
        self.etag = 1
        self.posts = []

    async def post(self, endpoint, body):
        self.posts.append((endpoint, body))
        assert endpoint == "getSharing" and body["datastudioEntity"]["type"] == 1
        return {"sharingData": {
            "sharingData": [{"member": {"id": e, "displayName": e}, "role": f"roles/datastudio.{r}"}
                            for e, r in self.members.items()],
            "etag": f"e{self.etag}", "canEdit": True, "isOwner": True,
            "sharingConfig": {"allowPublicLinkSharing": False}}}


class Studio:
    def __init__(self, session):
        self.session = session
        self.config = session.config
        self.journal = {}

    @contextlib.asynccontextmanager
    async def mutation_lock(self):
        yield

    def journal_action(self, kind, **fields):
        record = {"kind": kind, "operation_id": "op", **fields}
        self.journal["op"] = dict(record)
        return record

    def _journal(self, operation_id, record):
        self.journal[operation_id] = dict(record)


def sharing(tmp_path, members, apply=True):
    session = Session(tmp_path, members)
    service = Sharing(Studio(session))
    sent = []

    async def dialog(source_id, changes, notify):
        sent.append((changes, notify))
        if apply:
            session.members.update(changes)
            session.etag += 1

    service._dialog = dialog
    return service, session, sent


def test_get_reads_roles_and_server_etag(tmp_path):
    service, _, _ = sharing(tmp_path, {OWNER: "owner", "a@example.com": "viewer"})
    state = asyncio.run(service.get(SOURCE))
    assert state["fingerprint"] == "e1" and state["can_edit_sharing"]
    assert {(m["email"], m["role"]) for m in state["members"]} == {
        (OWNER, "owner"), ("a@example.com", "viewer")}


def test_dry_run_plans_only_real_changes_and_sends_nothing(tmp_path):
    service, _, sent = sharing(tmp_path, {OWNER: "owner", "a@example.com": "editor"})
    plan = asyncio.run(service.grant(SOURCE, [
        {"email": "A@example.com", "role": "editor"},
        {"email": "b@example.com", "role": "viewer"}], "e1"))
    assert plan["status"] == "planned" and sent == []
    assert plan["changes"] == [{"email": "b@example.com", "from": None, "to": "viewer"}]


def test_grant_is_journalled_and_read_back(tmp_path):
    service, session, sent = sharing(tmp_path, {OWNER: "owner", "old@example.com": "viewer"})
    result = asyncio.run(service.grant(SOURCE, [
        {"email": "new@example.com", "role": "editor"},
        {"email": "old@example.com", "role": "viewer"}], "e1", dry_run=False))
    assert result["status"] == "verified" and result["fingerprint"] == "e2"
    assert sent == [({"new@example.com": "editor"}, False)]
    record = service.studio.journal["op"]
    assert record["state"] == "verified"
    assert {"email": "old@example.com", "name": "old@example.com", "role": "viewer"} in record["before"]
    assert session.members == {OWNER: "owner", "old@example.com": "viewer", "new@example.com": "editor"}


def test_a_change_the_server_did_not_keep_fails_verification(tmp_path):
    service, _, _ = sharing(tmp_path, {OWNER: "owner"}, apply=False)
    result = asyncio.run(service.grant(
        SOURCE, [{"email": "x@example.com", "role": "viewer"}], "e1", dry_run=False))
    assert result["status"] == "verification_failed"


def test_unchanged_when_everyone_already_has_the_role(tmp_path):
    service, _, sent = sharing(tmp_path, {OWNER: "owner", "a@example.com": "viewer"})
    result = asyncio.run(service.grant(
        SOURCE, [{"email": "a@example.com", "role": "viewer"}], "e1", dry_run=False))
    assert result["status"] == "unchanged" and sent == []


@pytest.mark.parametrize(("members", "expected", "code"), [
    ([{"email": "b@example.com", "role": "viewer"}], "stale", "VERSION_CONFLICT"),
    ([{"email": OWNER, "role": "viewer"}], "e1", "INVALID_MEMBER"),
    ([{"email": "a@example.com", "role": "editor"}], "e1", "ROLE_CHANGE_UNSUPPORTED"),
    ([{"email": "not-an-email", "role": "viewer"}], "e1", "INVALID_MEMBER"),
    ([{"email": "b@example.com", "role": "owner"}], "e1", "INVALID_ROLE"),
    ([{"email": "b@example.com", "role": "viewer"},
      {"email": "b@example.com", "role": "editor"}], "e1", "INVALID_MEMBER"),
])
def test_refusals(tmp_path, members, expected, code):
    service, _, sent = sharing(tmp_path, {OWNER: "owner", "a@example.com": "viewer"})
    with pytest.raises(StudioError) as error:
        asyncio.run(service.grant(SOURCE, members, expected, dry_run=False))
    assert error.value.code == code and sent == []
