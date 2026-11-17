import asyncio
import copy

import pytest

from test_component_filters import BrowserAdapter, FilterSession
from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.group_filters import GroupFilterChange, GroupFilters
from datastudio_mcp.service import Studio


class GroupSession(FilterSession):
    def __init__(self, root, mode="valid"):
        super().__init__(root, mode)
        self.validations = 0
        self.page["groupConfig"] = [
            {"groupId": "g", "childGroupId": ["h"], "componentId": ["cd-test"], "opaque": 42},
            {"groupId": "h", "childGroupId": [], "componentId": ["child"]},
        ]
        for group in self.page["groupConfig"]:
            group["propertyConfig"] = {
                "groupProperty": {
                    "filters": ["f1"],
                    "inheritFilters": True,
                    "controlStatePassThrough": False,
                    "sectionType": 0,
                    "opaque": {"keep": 1},
                }
            }

    async def evaluate(self, script, args):
        self.validations += 1
        assert args["groupId"] in {"g", "h"}
        assert args["beforeIds"] == ["f1"] and args["beforeInherit"] is True
        if self.mode == "second-invalid" and self.validations == 2:
            return {"error": "INVALID_FILTER"}
        if self.mode == "source-change":
            self.source["revision"] += 1
        return {"owner_scope": "group", "consumers": []}


def test_batch_preserves_nested_structure_control_scope_and_exact_undo(tmp_path):
    async def scenario():
        session = GroupSession(tmp_path)
        studio = Studio(BrowserAdapter(session))
        before = copy.deepcopy(session.page)
        changes = [
            GroupFilterChange(group_id="g", filter_ids=[]),
            GroupFilterChange(group_id="h", inherit_filters=False),
        ]
        args = ("p_test", changes, fingerprint(before), fingerprint(session.report_content))
        preview = await GroupFilters(studio).patch(*args)
        assert preview["status"] == "planned" and session.page == before
        assert session.writes == 0 and not session.config.state_dir.exists()
        result = await GroupFilters(studio).patch(*args, dry_run=False)
        assert result["status"] == "verified" and session.writes == 1
        intended = copy.deepcopy(before)
        intended["groupConfig"][0]["propertyConfig"]["groupProperty"]["filters"] = []
        intended["groupConfig"][1]["propertyConfig"]["groupProperty"]["inheritFilters"] = False
        assert session.page == intended
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))[
            "status"
        ] == "verified"
        assert session.page == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,code",
    [
        ("second-invalid", "INVALID_FILTER"),
        ("source-change", "VERSION_CONFLICT"),
        ("resource-race", "VERSION_CONFLICT"),
    ],
)
def test_batch_failure_never_saves_first_validated_group(tmp_path, mode, code):
    async def scenario():
        session = GroupSession(tmp_path, mode)
        before = copy.deepcopy(session.page)
        with pytest.raises(StudioError) as caught:
            await GroupFilters(Studio(BrowserAdapter(session))).patch(
                "p_test",
                [
                    GroupFilterChange(group_id="g", filter_ids=["f2"]),
                    GroupFilterChange(group_id="h", filter_ids=["f2"]),
                ],
                fingerprint(before),
                fingerprint(session.report_content),
                dry_run=False,
            )
        assert caught.value.code == code and session.writes == 0
        assert session.page == before
        if mode == "second-invalid":
            assert session.validations == 2 and not session.config.state_dir.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["empty", "duplicate", "missing-group", "missing-filter"])
def test_bad_group_or_filter_selection_has_no_journal(tmp_path, case):
    async def scenario():
        session = GroupSession(tmp_path)
        changes = [GroupFilterChange(group_id="g", filter_ids=["f2"])]
        if case == "empty":
            changes = []
        if case == "duplicate":
            changes *= 2
        if case == "missing-group":
            changes[0].group_id = "missing"
        if case == "missing-filter":
            changes[0].filter_ids = ["missing"]
        with pytest.raises(StudioError):
            await GroupFilters(Studio(BrowserAdapter(session))).patch(
                "p_test",
                changes,
                fingerprint(session.page),
                fingerprint(session.report_content),
                dry_run=False,
            )
        assert session.writes == 0 and not session.config.state_dir.exists()

    asyncio.run(scenario())
