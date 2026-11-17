import asyncio
import copy

import pytest

from datastudio_mcp.browser import Config, StudioError
from datastudio_mcp.document import Patch, fingerprint
from datastudio_mcp.pages import NewPage, Pages, structure
from datastudio_mcp.service import Studio

REPORT = "0f0f0f0f-1a1a-4b2b-8c3c-4d4d4d4d4d4d"


class Session:
    def __init__(self, root):
        self.config = Config(REPORT, root, root / "journal")
        self.raw = {
            "shareable": {"id": REPORT},
            "version": "1",
            "report": {"opaque": [1, 2]},
            "page": [
                {
                    "pageId": p,
                    "page": {"componentConfig": [], "groupConfig": [], "opaque": {"keep": True}},
                    "chartCondition": [],
                    "version": "1",
                }
                for p in ("p_original", "p_second")
            ],
            "navigationInfo": {
                "style": 1,
                "opaque": "keep",
                "navItems": [
                    {
                        "page": {
                            "pageId": "p_original",
                            "displayName": "Original",
                            "icon": {"matIcon": "storefront"},
                        }
                    },
                    {"page": {"pageId": "p_second", "displayName": "Second"}},
                ],
            },
        }
        self.calls = []
        self.fail_after = None
        self.drop_last_created = False

    async def post(self, endpoint, body):
        if endpoint == "getReport":
            return {"reportConfig": copy.deepcopy(self.raw)}
        assert body["reportId"] == REPORT
        self.calls.append(endpoint)
        if endpoint == "createPages":
            for offset, p in enumerate(body["pages"]):
                if self.drop_last_created and offset == len(body["pages"]) - 1:
                    continue
                assert p["pageId"] not in {x["pageId"] for x in self.raw["page"]}
                self.raw["page"].append(
                    {
                        "pageId": p["pageId"],
                        "page": copy.deepcopy(p["page"]),
                        "chartCondition": copy.deepcopy(p["chartCondition"]),
                        "version": str(int(self.raw["version"]) + 1),
                    }
                )
                metadata = {
                    k: copy.deepcopy(v) for k, v in p.items() if k not in ("page", "chartCondition")
                }
                self.raw["navigationInfo"]["navItems"].insert(
                    body["position"]["index"] + offset, {"page": metadata}
                )
        elif endpoint == "deletePages":
            self.raw["page"] = [p for p in self.raw["page"] if p["pageId"] not in body["pageIds"]]
            self.raw["navigationInfo"]["navItems"] = [
                p
                for p in self.raw["navigationInfo"]["navItems"]
                if p.get("page", {}).get("pageId") not in body["pageIds"]
            ]
        elif endpoint == "updateReportNavigationInfo":
            assert body["version"] == self.raw["version"]
            self.raw["navigationInfo"] = copy.deepcopy(body["navigationInfo"])
        else:
            raise AssertionError(endpoint)
        self.raw["version"] = str(int(self.raw["version"]) + 1)
        if self.fail_after == endpoint:
            raise StudioError("TRANSPORT_FAILURE", "Response lost", outcome_unknown=True)
        return {}


def test_batch_create_reorder_and_restore(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        studio, before = Studio(session), structure(session.raw)
        pages = Pages(studio)
        specs = [NewPage(name="A", hidden=True, icon={"matIcon": "home"}), NewPage(name="B")]
        plan = await pages.create(specs, fingerprint(before), index=0)
        assert plan["status"] == "planned" and session.calls == []
        result = await pages.create(specs, fingerprint(before), index=0, dry_run=False)
        assert result["status"] == "verified"
        assert session.calls == ["createPages", "updateReportNavigationInfo"]
        assert session.raw["navigationInfo"]["navItems"][0]["page"]["hiddenFromViewer"]
        assert len(session.raw["page"]) == 4
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified"
        assert structure(session.raw) == before
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))[
            "status"
        ] == "unchanged"

    asyncio.run(scenario())


def test_delete_and_recreate_preserves_ids_content_icon_and_order(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        studio, before = Studio(session), structure(session.raw)
        result = await Pages(studio).delete(["p_original"], fingerprint(before), dry_run=False)
        assert result["status"] == "verified"
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified"
        assert structure(session.raw) == before
        assert session.calls == ["deletePages", "createPages", "updateReportNavigationInfo"]

    asyncio.run(scenario())


def test_navigation_guard_and_restore_conflict(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        studio, before = Studio(session), structure(session.raw)
        pages = Pages(studio)
        with pytest.raises(StudioError, match="exactly once"):
            await pages.navigation(
                [Patch(op="remove", path="/navItems/0")], fingerprint(before), dry_run=False
            )
        with pytest.raises(StudioError, match="at least one"):
            await pages.delete(["p_original", "p_second"], fingerprint(before), dry_run=False)
        assert session.calls == []
        result = await pages.navigation(
            [Patch(op="replace", path="/navItems/0/page/displayName", value="Renamed")],
            fingerprint(before),
            dry_run=False,
        )
        assert result["status"] == "verified"
        with pytest.raises(StudioError, match="changed since"):
            await pages.delete(["p_second"], fingerprint(before), dry_run=False)
        session.raw["page"][0]["page"]["opaque"]["keep"] = "later human edit"
        with pytest.raises(StudioError, match="changed after"):
            await studio.restore_operation(result["operation_id"], dry_run=False)

    asyncio.run(scenario())


@pytest.mark.parametrize("lost_response", [True, False])
def test_partial_create_is_not_replayed_and_can_be_recovered(tmp_path, lost_response):
    async def scenario():
        session = Session(tmp_path)
        studio, before = Studio(session), structure(session.raw)
        if lost_response:
            session.fail_after = "createPages"
        else:
            session.drop_last_created = True
        result = await Pages(studio).create(
            [NewPage(name="A"), NewPage(name="B")], fingerprint(before), index=0, dry_run=False
        )
        assert result["status"] == "partially_applied"
        assert session.calls == ["createPages"]
        assert studio.operation(result["operation_id"])["after_structure"] == structure(session.raw)
        session.fail_after, session.drop_last_created = None, False
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified"
        assert structure(session.raw) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("concurrent_change", ["page", "navigation"])
def test_recovery_never_claims_concurrent_work_from_readback(tmp_path, concurrent_change):
    class ConcurrentSession(Session):
        async def post(self, endpoint, body):
            result = await super().post(endpoint, body)
            if endpoint == "createPages":
                if concurrent_change == "page":
                    other = copy.deepcopy(self.raw["page"][0])
                    other["pageId"] = "p_somebody_else"
                    self.raw["page"].append(other)
                    self.raw["navigationInfo"]["navItems"].append(
                        {"page": {"pageId": "p_somebody_else", "displayName": "Their work"}}
                    )
                else:
                    self.raw["navigationInfo"]["navItems"][0]["page"]["displayName"] = "Their title"
                raise StudioError("TRANSPORT_FAILURE", "Lost response", outcome_unknown=True)
            return result

    async def scenario():
        session = ConcurrentSession(tmp_path)
        studio = Studio(session)
        result = await Pages(studio).create(
            [NewPage(name="Ours")], fingerprint(structure(session.raw)), dry_run=False
        )
        assert result["status"] == "partially_applied"
        with pytest.raises(StudioError) as error:
            await studio.restore_operation(result["operation_id"], dry_run=False)
        assert error.value.code == "UNRESOLVED_SIDE_EFFECT"
        assert session.calls == ["createPages"]

    asyncio.run(scenario())
