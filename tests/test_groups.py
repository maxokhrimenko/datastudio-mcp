import asyncio
import copy

import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.components import Components, GroupMove, NewGroup, translate_groups
from datastudio_mcp.document import fingerprint
from datastudio_mcp.service import Studio
from test_components import BrowserAdapter, GeometrySession, arrangement_objects


class GroupSession(GeometrySession):
    async def evaluate(self, script, arguments):
        if "groups" not in arguments:
            return await super().evaluate(script, arguments)
        return [
            {
                "groupId": g["group_id"],
                "componentId": g["component_ids"],
                "childGroupId": [],
                "conceptDefs": [],
                "opaque_native_default": {"keep": 7},
                "propertyConfig": {
                    "groupProperty": {
                        "filters": [],
                        "inheritFilters": True,
                        "controlStatePassThrough": False,
                        "sectionType": 0,
                        "backgroundAndBorderProperty": None,
                        "groupAlignment": None,
                    }
                },
            }
            for g in arguments["groups"]
        ]


def test_groups_batch_create_ungroup_and_reverse_undo(tmp_path):
    async def scenario():
        session = GroupSession(tmp_path)
        session.page["componentConfig"] = arrangement_objects()
        extra = copy.deepcopy(session.page["componentConfig"][0])
        extra["componentId"] = "d"
        session.page["componentConfig"].append(extra)
        before = copy.deepcopy(session.page)
        studio = Studio(BrowserAdapter(session))
        components = Components(studio)
        specs = [NewGroup(component_ids=["a", "b"]), NewGroup(component_ids=["c", "d"])]
        preview = await components.groups_create("p_test", specs, fingerprint(before))
        assert preview["status"] == "planned" and preview["ids_provisional"]
        assert session.page == before and session.writes == 0
        created = await components.groups_create(
            "p_test", specs, fingerprint(before), dry_run=False
        )
        assert created["status"] == "verified" and session.writes == 1
        grouped = copy.deepcopy(session.page)
        assert grouped["componentConfig"] == before["componentConfig"]
        assert [g["componentId"] for g in grouped["groupConfig"]] == [["a", "b"], ["c", "d"]]
        assert all(g["opaque_native_default"] == {"keep": 7} for g in grouped["groupConfig"])
        assert all(
            "groupAlignment" not in g["propertyConfig"]["groupProperty"]
            for g in grouped["groupConfig"]
        )
        assert all(
            "backgroundAndBorderProperty" not in g["propertyConfig"]["groupProperty"]
            for g in grouped["groupConfig"]
        )
        ungrouped = await components.groups_ungroup(
            "p_test", created["created_group_ids"], fingerprint(grouped), dry_run=False
        )
        assert ungrouped["status"] == "verified" and session.page == before
        assert ungrouped["control_scope_changes"]
        assert (await studio.restore_operation(ungrouped["operation_id"], dry_run=False))[
            "status"
        ] == "verified"
        assert session.page == grouped
        assert (await studio.restore_operation(created["operation_id"], dry_run=False))[
            "status"
        ] == "verified"
        assert session.page == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case,code",
    [
        ("overlap", "INVALID_BATCH"),
        ("grouped", "GROUP_OPERATION_REQUIRED"),
        ("nested", "NESTED_GROUP_OPERATION_REQUIRED"),
        ("missing", "NOT_FOUND"),
    ],
)
def test_group_mutation_guards(tmp_path, case, code):
    async def scenario():
        session = GroupSession(tmp_path)
        components = Components(Studio(BrowserAdapter(session)))
        ids = ["cd-test", "cd-second"]
        if case in {"grouped", "nested"}:
            session.page["groupConfig"] = [
                {
                    "groupId": "g-existing",
                    "componentId": ids,
                    "childGroupId": ["g-child"] if case == "nested" else [],
                }
            ]
        with pytest.raises(StudioError) as caught:
            if case in {"overlap", "grouped"}:
                await components.groups_create(
                    "p_test",
                    [NewGroup(component_ids=ids)] * (2 if case == "overlap" else 1),
                    fingerprint(session.page),
                    dry_run=False,
                )
            else:
                await components.groups_ungroup(
                    "p_test", ["g-existing"], fingerprint(session.page), dry_run=False
                )
        assert caught.value.code == code and session.writes == 0

    asyncio.run(scenario())


def movable_page():
    return {
        "componentConfig": arrangement_objects(),
        "groupConfig": [
            {
                "groupId": "g1",
                "componentId": ["a", "b"],
                "opaque": {"keep": 7},
                "propertyConfig": {"groupProperty": {"filters": [{"resourceId": "filter-1"}]}},
            },
            {"groupId": "g2", "componentId": ["c"]},
        ],
    }


def test_group_translation_preserves_everything_except_positions():
    page = movable_page()
    expected = copy.deepcopy(page)
    for i, dx, dy in [(0, -17, 12), (1, -17, 12), (2, 5, -9)]:
        a = expected["componentConfig"][i]["attributeConfig"]["componentAttribute"]
        a["left"] += dx
        a["top"] += dy
    after, changes = translate_groups(
        page,
        [
            GroupMove(group_id="g1", offset_x=-17, offset_y=12),
            GroupMove(group_id="g2", offset_x=5, offset_y=-9),
        ],
    )
    assert after == expected
    assert page == movable_page()
    assert [c["component_id"] for c in changes] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "case,code",
    [
        ("overlap", "AMBIGUOUS_GROUP_MEMBERSHIP"),
        ("nested", "NESTED_GROUP_OPERATION_REQUIRED"),
        ("missing", "INVALID_GROUP"),
        ("composite", "COMPOSITE_OPERATION_REQUIRED"),
        ("fractional", "INVALID_GEOMETRY"),
        ("overflow", "INVALID_GEOMETRY"),
    ],
)
def test_group_move_invalid_layout(case, code):
    page = movable_page()
    attrs = page["componentConfig"][0]["attributeConfig"]["componentAttribute"]
    if case == "overlap":
        page["groupConfig"][1]["componentId"].append("a")
    if case == "nested":
        page["groupConfig"][1]["childGroupId"] = ["g1"]
    if case == "missing":
        page["groupConfig"][0]["componentId"].append("missing")
    if case == "composite":
        page["componentConfig"][0]["componentConfig"] = [{"componentId": "child"}]
    if case == "fractional":
        attrs["left"] = 0.5
    if case == "overflow":
        attrs["left"] = 9007199254740991
    before = copy.deepcopy(page)
    with pytest.raises(StudioError) as caught:
        translate_groups(page, [GroupMove(group_id="g1", offset_x=10)])
    assert caught.value.code == code and page == before


def test_groups_move_preview_write_noop_and_undo(tmp_path):
    async def scenario():
        session = GroupSession(tmp_path)
        session.page = movable_page()
        session.conditions = [{"chartId": "a", "opaque": [1, 2]}]
        before = copy.deepcopy(session.page)
        studio = Studio(BrowserAdapter(session))
        components = Components(studio)
        moves = [GroupMove(group_id="g1", offset_x=-10, offset_y=20)]
        preview = await components.groups_move("p_test", moves, fingerprint(before))
        assert preview["status"] == "planned" and not preview["control_scope_changes"]
        assert session.writes == 0 and session.page == before
        moved = await components.groups_move("p_test", moves, fingerprint(before), dry_run=False)
        assert moved["status"] == "verified" and session.writes == 1
        assert session.page["groupConfig"] == before["groupConfig"]
        assert session.conditions == [{"chartId": "a", "opaque": [1, 2]}]
        noop = await components.groups_move(
            "p_test", [GroupMove(group_id="g1")], fingerprint(session.page), dry_run=False
        )
        assert noop["status"] == "unchanged" and session.writes == 1
        assert (await studio.restore_operation(moved["operation_id"], dry_run=False))[
            "status"
        ] == "verified"
        assert session.page == before

    asyncio.run(scenario())
