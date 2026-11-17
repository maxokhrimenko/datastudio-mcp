import asyncio
import copy
import json
import os
import stat

import pytest

from datastudio_mcp.browser import Config, StudioError
from datastudio_mcp.components import Components
from datastudio_mcp.document import Patch, fingerprint
from datastudio_mcp.service import ComponentChange, Studio

REPORT = "0f0f0f0f-1a1a-4b2b-8c3c-4d4d4d4d4d4d"


def test_journal_syncs_directory_after_atomic_replace(tmp_path, monkeypatch):
    studio = Studio(Session(tmp_path))
    synced = []
    real_fsync = os.fsync

    def observe(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(studio.config.state_dir.joinpath("probe.json").exists())
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", observe)
    path = studio._journal("probe", {"state": "prepared"})
    assert synced == [False, True]  # Parent creation, then committed file name.
    assert json.loads(path.read_text()) == {"state": "prepared"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_suffix(".pending").exists()


def test_journal_directory_sync_failure_prevents_dispatch(tmp_path, monkeypatch):
    async def scenario():
        session = Session(tmp_path)
        session.config.state_dir.mkdir()
        studio = Studio(session)
        real_fsync = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_directory)
        with pytest.raises(OSError, match="directory sync failed"):
            await studio.patch_components(
                "p_test", changes(), fingerprint(session.page), dry_run=False
            )
        assert session.writes == 0
        records = list(session.config.state_dir.glob("*.json"))
        assert len(records) == 1
        assert json.loads(records[0].read_text())["state"] == "prepared"

    asyncio.run(scenario())


class Session:
    def __init__(self, root):
        self.config = Config(REPORT, root, root / "journal")
        self.page = {
            "componentConfig": [
                {
                    "componentId": "cd-test",
                    "type": "simple-description",
                    "opaque": [1, 2],
                    "text": "before",
                }
            ],
            "groupConfig": [],
        }
        self.version = "1"
        self.writes = 0
        self.fail_after_write = False
        self.corrupt_write = False
        self.conditions = []
        self.ignore_conditions = False

    async def post(self, endpoint, body):
        if endpoint == "getReport":
            return {
                "reportConfig": {
                    "shareable": {"id": REPORT},
                    "report": {},
                    "page": [
                        {
                            "pageId": "p_test",
                            "page": copy.deepcopy(self.page),
                            "version": self.version,
                            "chartCondition": copy.deepcopy(self.conditions),
                        }
                    ],
                }
            }
        assert endpoint == "updatePage"
        assert body["reportId"] == REPORT
        assert body["version"] == self.version
        if getattr(self, "refuse_write", False):
            raise StudioError("HTTP_400", "updatePage returned HTTP 400")
        self.writes += 1
        self.page = {
            k: v
            for k, v in body["pageRequest"].items()
            if k not in ("pageId", "fieldMask", "chartCondition")
        }
        self.version = str(int(self.version) + 1)
        if 5 in body["pageRequest"]["fieldMask"] and not self.ignore_conditions:
            self.conditions = copy.deepcopy(body["pageRequest"]["chartCondition"])
        if self.corrupt_write:
            self.page["componentConfig"][0]["text"] = "server chose a different value"
        if self.fail_after_write:
            raise StudioError("TRANSPORT_FAILURE", "Connection lost", outcome_unknown=True)
        return {"version": self.version}


def changes(value="after"):
    return [
        ComponentChange(
            component_id="cd-test", patches=[Patch(op="replace", path="/text", value=value)]
        )
    ]


def test_plan_conflict_apply_and_restore(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        studio = Studio(session)
        original = copy.deepcopy(session.page)
        token = fingerprint(original)
        plan = await studio.patch_components("p_test", changes(), token)
        assert plan["status"] == "planned" and session.writes == 0
        result = await studio.patch_components("p_test", changes(), token, dry_run=False)
        assert result["status"] == "verified"
        assert session.page["componentConfig"][0]["opaque"] == [1, 2]
        record = studio.operation(result["operation_id"])
        assert record["before"]["page"] == original
        with pytest.raises(StudioError, match="differs"):
            await studio.patch_components("p_test", changes(), token, dry_run=False)
        assert session.writes == 1
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified" and session.page == original

    asyncio.run(scenario())


def test_ambiguous_response_is_read_back_without_replaying(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        session.fail_after_write = True
        result = await Studio(session).patch_components(
            "p_test", changes(), fingerprint(session.page), dry_run=False
        )
        assert result["status"] == "verified"
        assert result["dispatch_error"] == "TRANSPORT_FAILURE"
        assert session.writes == 1

    asyncio.run(scenario())


def test_a_write_the_server_refuses_is_rejected_not_a_failed_verification(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        session.refuse_write = True
        before = copy.deepcopy(session.page)
        result = await Studio(session).patch_components(
            "p_test", changes(), fingerprint(session.page), dry_run=False
        )
        assert result["status"] == "rejected" and result["dispatch_error"] == "HTTP_400"
        assert session.page == before

    asyncio.run(scenario())


def test_accepted_but_wrong_value_is_not_success(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        session.corrupt_write = True
        result = await Studio(session).patch_components(
            "p_test", changes(), fingerprint(session.page), dry_run=False
        )
        assert result["status"] == "verification_failed"
        assert result["mismatches"] == ["/componentConfig/0/text"]

    asyncio.run(scenario())


def test_recovery_refuses_later_work_and_foreign_journal(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        studio = Studio(session)
        result = await studio.patch_components(
            "p_test", changes(), fingerprint(session.page), dry_run=False
        )
        session.page["componentConfig"][0]["text"] = "someone else's edit"
        with pytest.raises(StudioError, match="changed after"):
            await studio.restore_operation(result["operation_id"], dry_run=False)
        path = session.config.state_dir / f"{result['operation_id']}.json"
        record = json.loads(path.read_text())
        record["report_id"] = "another-report"
        path.write_text(json.dumps(record))
        with pytest.raises(StudioError, match="another report"):
            studio.operation(result["operation_id"])
        assert session.writes == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("restore_removed_setting", [False, True])
def test_removal_inside_metric_style_array_is_verified(tmp_path, restore_removed_setting):
    class StyleSession(Session):
        async def post(self, endpoint, body):
            result = await super().post(endpoint, body)
            if endpoint == "updatePage" and restore_removed_setting:
                self.page["componentConfig"][0]["styles"][0]["compactNumbers"] = True
            return result

    async def scenario():
        session = StyleSession(tmp_path)
        session.page["componentConfig"][0]["styles"] = [
            {"compactNumbers": True, "opaque": "preserve"},
            {"compactNumbers": False},
        ]
        result = await Studio(session).patch_components(
            "p_test",
            [
                ComponentChange(
                    component_id="cd-test",
                    patches=[Patch(op="remove", path="/styles/0/compactNumbers")],
                )
            ],
            fingerprint(session.page),
            dry_run=False,
        )
        assert result["status"] == (
            "verification_failed" if restore_removed_setting else "verified"
        )
        if restore_removed_setting:
            assert result["mismatches"] == ["/componentConfig/0/styles/0/compactNumbers"]
        assert session.page["componentConfig"][0]["styles"][0]["opaque"] == "preserve"
        assert session.writes == 1

    asyncio.run(scenario())


def test_deleted_component_and_its_conditions_are_recreated(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        session.conditions = [{"chartId": "cd-test", "conditionalFormat": {"rule": [{"x": 1}]}}]
        page_before, conditions_before = (
            copy.deepcopy(session.page),
            copy.deepcopy(session.conditions),
        )
        studio = Studio(session)
        plan = await Components(studio).delete("p_test", ["cd-test"], fingerprint(session.page))
        assert plan["removed_condition_owner_ids"] == ["cd-test"] and session.writes == 0
        deleted = await Components(studio).delete(
            "p_test", ["cd-test"], fingerprint(session.page), dry_run=False
        )
        assert deleted["status"] == "verified"
        assert session.page["componentConfig"] == [] and session.conditions == []
        restored = await studio.restore_operation(deleted["operation_id"], dry_run=False)
        assert restored["status"] == "verified"
        assert session.page == page_before and session.conditions == conditions_before

    asyncio.run(scenario())


def test_condition_write_is_independently_verified(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        session.conditions = [{"chartId": "cd-test", "conditionalFormat": {"rule": []}}]
        session.ignore_conditions = True
        result = await Components(Studio(session)).delete(
            "p_test", ["cd-test"], fingerprint(session.page), dry_run=False
        )
        assert result["status"] == "verification_failed"
        assert "/chartCondition" in result["mismatches"]

    asyncio.run(scenario())


def test_grouped_delete_requires_group_aware_operation(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        session.page["groupConfig"] = [{"groupId": "g1", "componentId": ["cd-test"]}]
        with pytest.raises(StudioError, match="group-aware"):
            await Components(Studio(session)).delete(
                "p_test", ["cd-test"], fingerprint(session.page), dry_run=False
            )
        assert session.writes == 0

    asyncio.run(scenario())


def test_numeric_to_boolean_edit_and_restore_are_not_suppressed(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        session.page["componentConfig"][0]["toggle"] = 1
        studio = Studio(session)
        result = await studio.patch_components(
            "p_test",
            [
                ComponentChange(
                    component_id="cd-test",
                    patches=[Patch(op="replace", path="/toggle", value=True)],
                )
            ],
            fingerprint(session.page),
            dry_run=False,
        )
        assert result["status"] == "verified" and session.writes == 1
        assert session.page["componentConfig"][0]["toggle"] is True
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified" and session.writes == 2
        assert type(session.page["componentConfig"][0]["toggle"]) is int

    asyncio.run(scenario())


def test_conditions_only_restore_is_not_mistaken_for_unchanged_page(tmp_path):
    async def scenario():
        session = Session(tmp_path)
        studio = Studio(session)
        frame = (await studio.report())["reportConfig"]["page"][0]
        result = await studio._write_page(
            frame,
            copy.deepcopy(session.page),
            [{"chartId": "cd-test", "conditionalFormat": {"rule": []}}],
        )
        assert result["status"] == "verified"
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified" and session.conditions == []
        assert session.writes == 2

    asyncio.run(scenario())
