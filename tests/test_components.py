import asyncio
import copy

import pytest
from pydantic import ValidationError

from datastudio_mcp.browser import StudioError
from datastudio_mcp.components import (
    ComponentGeometry,
    Components,
    arrangement_geometry,
    layer_order,
)
from datastudio_mcp.document import fingerprint
from datastudio_mcp.service import Studio
from test_service import Session


class GeometrySession(Session):
    def __init__(self, root):
        super().__init__(root)
        self.layout_result = {"static": True}
        self.page["componentConfig"][0]["attributeConfig"] = {
            "componentAttribute": {
                "left": 10,
                "top": 20,
                "width": 100,
                "height": 80,
                "rotation": 17,
                "opaque": {"retained": True},
            }
        }
        second = copy.deepcopy(self.page["componentConfig"][0])
        second["componentId"] = "cd-second"
        self.page["componentConfig"].append(second)

    # Session.page is the fixture's saved document; expose a separate browser
    # boundary through an adapter below, without mocking native domain helpers.
    async def evaluate(self, script, arguments):
        assert arguments["version"] == self.version
        return self.layout_result


class BrowserAdapter:
    def __init__(self, session):
        self.saved = session
        self.config = session.config

    async def page(self):
        return self.saved

    async def post(self, endpoint, body):
        return await self.saved.post(endpoint, body)


def test_geometry_batch_preview_write_and_restore(tmp_path):
    async def scenario():
        session = GeometrySession(tmp_path)
        studio = Studio(BrowserAdapter(session))
        components = Components(studio)
        before = copy.deepcopy(session.page)
        token = fingerprint(before)
        changes = [
            ComponentGeometry(component_id="cd-test", left=-5, width=170),
            ComponentGeometry(component_id="cd-second", top=42, height=90),
        ]
        preview = await components.geometry("p_test", changes, token)
        assert preview["changes"][0]["after"] == {"left": -5, "width": 170}
        assert session.page == before and session.writes == 0
        result = await components.geometry("p_test", changes, token, dry_run=False)
        assert result["status"] == "verified" and session.writes == 1
        expected = copy.deepcopy(before)
        expected["componentConfig"][0]["attributeConfig"]["componentAttribute"].update(
            left=-5, width=170
        )
        expected["componentConfig"][1]["attributeConfig"]["componentAttribute"].update(
            top=42, height=90
        )
        assert session.page == expected
        unchanged = await components.geometry(
            "p_test", changes, fingerprint(session.page), dry_run=False
        )
        assert unchanged["status"] == "unchanged" and session.writes == 1
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified" and session.page == before

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["stale", "responsive", "group", "composite", "duplicate"])
def test_geometry_guards_before_write(tmp_path, case):
    async def scenario():
        session = GeometrySession(tmp_path)
        components = Components(Studio(BrowserAdapter(session)))
        changes = [ComponentGeometry(component_id="cd-test", left=25)]
        error = {
            "stale": "VERSION_CONFLICT",
            "responsive": "RESPONSIVE_LAYOUT",
            "group": "GROUP_OPERATION",
            "composite": "COMPOSITE_OPERATION",
            "duplicate": "INVALID_BATCH",
        }[case]
        if case == "responsive":
            session.layout_result = {"error": "RESPONSIVE_LAYOUT_REQUIRES_SEPARATE_OPERATION"}
        if case == "group":
            session.page["groupConfig"] = [{"componentId": ["cd-test"]}]
        if case == "composite":
            session.page["componentConfig"][0]["componentConfig"] = [{"componentId": "child"}]
        if case == "duplicate":
            changes *= 2
        token = "stale" if case == "stale" else fingerprint(session.page)
        with pytest.raises(StudioError) as caught:
            await components.geometry("p_test", changes, token, dry_run=False)
        assert error in caught.value.code
        assert session.writes == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"width": 0},
        {"height": -1},
        {"left": True},
        {"top": float("nan")},
        {"left": float("inf")},
        {"width": None},
        {"left": "10"},
    ],
)
def test_geometry_invalid_values(values):
    with pytest.raises(ValidationError):
        ComponentGeometry(component_id="cd-test", **values)


def arrangement_objects():
    return [
        {
            "componentId": name,
            "attributeConfig": {
                "componentAttribute": {
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                    "rotation": 0,
                }
            },
        }
        for name, left, top, width, height in [
            ("a", 10, 20, 30, 40),
            ("b", 60, 100, 50, 20),
            ("c", 210, 220, 10, 80),
        ]
    ]


@pytest.mark.parametrize(
    "action,axis,expected",
    [
        ("align_left", "left", [10, 10, 10]),
        ("align_center", "left", [100, 90, 110]),
        ("align_right", "left", [190, 170, 210]),
        ("align_top", "top", [20, 20, 20]),
        ("align_middle", "top", [140, 150, 120]),
        ("align_bottom", "top", [260, 280, 220]),
        ("distribute_horizontal", "left", [10, 100, 210]),
        ("distribute_vertical", "top", [20, 130, 220]),
    ],
)
def test_arrangement_matches_selection_bounds_and_edge_gaps(action, axis, expected):
    selected = arrangement_objects()
    before = copy.deepcopy(selected)
    result = arrangement_geometry(selected, action)
    assert [change.model_dump(exclude_unset=True) for change in result] == [
        {"component_id": component["componentId"], axis: value}
        for component, value in zip(selected, expected, strict=True)
    ]
    assert selected == before


def test_arrangement_overlap_and_stable_ties():
    selected = arrangement_objects()
    for c in selected:
        c["attributeConfig"]["componentAttribute"].update(left=10, width=30)
    result = arrangement_geometry(selected[::-1], "distribute_horizontal")
    assert [c.component_id for c in result] == ["c", "b", "a"]
    assert [c.left for c in result] == [10, 10, 10]  # gap -30, not clamped to zero


def test_arrangement_rejects_rotation_and_small_distribution():
    selected = arrangement_objects()
    with pytest.raises(StudioError) as caught:
        arrangement_geometry(selected[:2], "distribute_horizontal")
    assert caught.value.code == "INVALID_BATCH"
    selected[0]["attributeConfig"]["componentAttribute"]["rotation"] = 90
    with pytest.raises(StudioError) as caught:
        arrangement_geometry(selected, "align_left")
    assert caught.value.code == "ROTATED_OPERATION_REQUIRED"


def test_arrangement_write_preserves_other_fields_and_undo(tmp_path):
    async def scenario():
        session = GeometrySession(tmp_path)
        session.page["componentConfig"] = arrangement_objects()
        session.conditions = [{"chartId": "b", "opaque": {"keep": True}}]
        before = copy.deepcopy(session.page)
        studio = Studio(BrowserAdapter(session))
        result = await Components(studio).arrange(
            "p_test", ["a", "b", "c"], "distribute_horizontal", fingerprint(before), dry_run=False
        )
        assert result["status"] == "verified" and session.writes == 1
        expected = copy.deepcopy(before)
        expected["componentConfig"][1]["attributeConfig"]["componentAttribute"]["left"] = 100
        assert session.page == expected
        assert session.conditions == [{"chartId": "b", "opaque": {"keep": True}}]
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))[
            "status"
        ] == "verified"
        assert session.page == before

    asyncio.run(scenario())


def test_arrangement_rechecks_after_calculation(tmp_path):
    class ConcurrentSession(GeometrySession):
        reads = 0

        async def post(self, endpoint, body):
            if endpoint == "getReport":
                self.reads += 1
                if self.reads == 2:
                    self.page["componentConfig"][0]["human_change"] = True
            return await super().post(endpoint, body)

    async def scenario():
        session = ConcurrentSession(tmp_path)
        session.page["componentConfig"] = arrangement_objects()
        with pytest.raises(StudioError) as caught:
            await Components(Studio(BrowserAdapter(session))).arrange(
                "p_test", ["a", "b", "c"], "align_left", fingerprint(session.page), dry_run=False
            )
        assert caught.value.code == "VERSION_CONFLICT"
        assert session.writes == 0
        assert session.page["componentConfig"][0]["human_change"] is True

    asyncio.run(scenario())


def test_geometry_fractional_preview_matches_integer_server_contract(tmp_path):
    async def scenario():
        session = GeometrySession(tmp_path)
        components = Components(Studio(BrowserAdapter(session)))
        result = await components.geometry(
            "p_test",
            [
                ComponentGeometry(
                    component_id="cd-test", left=-7.75, top=11.75, width=289.75, height=268.75
                )
            ],
            fingerprint(session.page),
        )
        change = result["changes"][0]
        assert change["requested"] == {
            "left": -7.75,
            "top": 11.75,
            "width": 289.75,
            "height": 268.75,
        }
        assert change["after"] == {"left": -7, "top": 11, "width": 289, "height": 268}
        assert result["pixel_conversion"] == "truncate_toward_zero" and session.writes == 0
        with pytest.raises(StudioError) as caught:
            await components.geometry(
                "p_test",
                [ComponentGeometry(component_id="cd-test", width=0.5)],
                fingerprint(session.page),
                dry_run=False,
            )
        assert caught.value.code == "INVALID_GEOMETRY" and session.writes == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "action,selected,expected",
    [
        ("bring_to_front", ["d", "b"], ["a", "c", "e", "b", "d"]),
        ("send_to_back", ["d", "b"], ["b", "d", "a", "c", "e"]),
        ("bring_forward", ["d", "b"], ["a", "c", "b", "e", "d"]),
        ("send_backward", ["d", "b"], ["b", "a", "d", "c", "e"]),
        ("bring_forward", ["c", "b"], ["a", "d", "b", "c", "e"]),
        ("send_backward", ["c", "b"], ["b", "c", "a", "d", "e"]),
        ("bring_forward", ["e", "d", "b"], ["a", "c", "b", "d", "e"]),
        ("send_backward", ["a", "b", "d"], ["a", "b", "d", "c", "e"]),
        ("bring_forward", ["d", "e"], ["a", "b", "c", "d", "e"]),
        ("send_backward", ["b", "a"], ["a", "b", "c", "d", "e"]),
    ],
)
def test_layer_order_preserves_relative_stacking(action, selected, expected):
    original = ["a", "b", "c", "d", "e"]
    assert layer_order(original, selected, action) == expected
    assert original == ["a", "b", "c", "d", "e"]


@pytest.mark.parametrize(
    "selected,code",
    [([], "INVALID_BATCH"), (["a", "a"], "INVALID_BATCH"), (["missing"], "NOT_FOUND")],
)
def test_layer_selection_guards(selected, code):
    with pytest.raises(StudioError) as caught:
        layer_order(["a", "b"], selected, "bring_forward")
    assert caught.value.code == code


def test_layers_preserve_payloads_conditions_and_restore(tmp_path):
    async def scenario():
        session = GeometrySession(tmp_path)
        session.page["componentConfig"] = arrangement_objects()
        session.page["componentConfig"][0]["componentConfig"] = [
            {"componentId": "child", "opaque": 7}
        ]
        session.conditions = [{"chartId": "child", "opaque": "keep"}]
        before = copy.deepcopy(session.page)
        studio = Studio(BrowserAdapter(session))
        components = Components(studio)
        preview = await components.layers("p_test", ["a"], "bring_to_front", fingerprint(before))
        assert preview["after_order"] == ["b", "c", "a"] and session.writes == 0
        result = await components.layers(
            "p_test", ["a"], "bring_to_front", fingerprint(before), dry_run=False
        )
        assert result["status"] == "verified" and session.writes == 1
        assert session.page["componentConfig"] == [before["componentConfig"][i] for i in [1, 2, 0]]
        assert session.conditions == [{"chartId": "child", "opaque": "keep"}]
        assert (
            await components.layers(
                "p_test", ["a"], "bring_to_front", fingerprint(session.page), dry_run=False
            )
        )["status"] == "unchanged"
        assert session.writes == 1
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))[
            "status"
        ] == "verified"
        assert session.page == before

    asyncio.run(scenario())


def test_layers_group_guard_before_write(tmp_path):
    async def scenario():
        session = GeometrySession(tmp_path)
        session.page["groupConfig"] = [{"componentId": ["cd-second"]}]
        components = Components(Studio(BrowserAdapter(session)))
        for ids, action in ((["cd-test"], "bring_forward"), (["cd-second"], "bring_to_front")):
            with pytest.raises(StudioError) as caught:
                await components.layers(
                    "p_test", ids, action, fingerprint(session.page), dry_run=False
                )
            assert caught.value.code == "GROUP_OPERATION_REQUIRED" and session.writes == 0
        # An ungrouped component goes to the front of a grouped page, as in the editor.
        result = await components.layers(
            "p_test", ["cd-test"], "bring_to_front", fingerprint(session.page), dry_run=True
        )
        assert result["after_order"] == ["cd-second", "cd-test"]

    asyncio.run(scenario())


def test_insert_space_moves_everything_below_with_groups_whole_and_grows_the_page(tmp_path):
    async def scenario():
        session = GeometrySession(tmp_path)
        page = session.page
        third = copy.deepcopy(page["componentConfig"][0])
        third["componentId"] = "cd-third"
        page["componentConfig"].append(third)
        tops = {"cd-test": 10, "cd-second": 300, "cd-third": 50}
        for c in page["componentConfig"]:
            c["attributeConfig"]["componentAttribute"]["top"] = tops[c["componentId"]]
        # cd-third starts above the line but is grouped with cd-second (below): the group
        # starts at 50, above the line, so neither moves; cd-test does not move either.
        page["groupConfig"] = [{"groupId": "g", "componentId": ["cd-second", "cd-third"]}]
        page["attributeConfig"] = {"pageAttribute": {"height": 1000}}
        components = Components(Studio(BrowserAdapter(session)))
        with pytest.raises(StudioError) as caught:
            await components.insert_space("p_test", 100, 40, fingerprint(page), dry_run=True)
        assert caught.value.code == "NOT_FOUND"
        page["groupConfig"] = []
        result = await components.insert_space("p_test", 100, 40, fingerprint(page), dry_run=False)
        assert result["status"] == "verified" and result["moved_component_ids"] == ["cd-second"]
        after = {c["componentId"]: c["attributeConfig"]["componentAttribute"]["top"]
                 for c in session.page["componentConfig"]}
        assert after == {"cd-test": 10, "cd-second": 340, "cd-third": 50}
        assert session.page["attributeConfig"]["pageAttribute"]["height"] == 1040

    asyncio.run(scenario())


def test_insert_space_with_negative_height_removes_space_but_never_overlaps(tmp_path):
    async def scenario():
        session = GeometrySession(tmp_path)
        page = session.page
        tops = {"cd-test": 10, "cd-second": 300}
        for c in page["componentConfig"]:
            a = c["attributeConfig"]["componentAttribute"]
            a.update(top=tops[c["componentId"]], height=80)
        page["attributeConfig"] = {"pageAttribute": {"height": 1000}}
        components = Components(Studio(BrowserAdapter(session)))
        with pytest.raises(StudioError) as caught:  # cd-test ends at 90: up to 80 overlaps
            await components.insert_space("p_test", 300, -220, fingerprint(page), dry_run=True)
        assert caught.value.code == "OVERLAP"
        result = await components.insert_space("p_test", 300, -200, fingerprint(page), dry_run=False)
        assert result["status"] == "verified"
        assert session.page["componentConfig"][1]["attributeConfig"]["componentAttribute"]["top"] == 100
        assert session.page["attributeConfig"]["pageAttribute"]["height"] == 800

    asyncio.run(scenario())


def test_insert_space_moves_a_line_by_its_end_points(tmp_path):
    async def scenario():
        session = GeometrySession(tmp_path)
        page = session.page
        line = page["componentConfig"][1]
        # The saved box of a line lags behind; its end points are where it is drawn.
        line["attributeConfig"]["componentAttribute"].update(top=0, height=20)
        line["propertyConfig"] = {"componentProperty": {"arrowProperty": {"terminals": {
            "start": {"top": 500, "left": 48}, "end": {"top": 500, "left": 648}}}}}
        page["componentConfig"][0]["attributeConfig"]["componentAttribute"]["top"] = 10
        page["attributeConfig"] = {"pageAttribute": {"height": 1000}}
        components = Components(Studio(BrowserAdapter(session)))
        result = await components.insert_space("p_test", 100, 40, fingerprint(page), dry_run=False)
        assert result["moved_component_ids"] == [line["componentId"]]
        moved = next(c for c in session.page["componentConfig"] if c["componentId"] == line["componentId"])
        ends = moved["propertyConfig"]["componentProperty"]["arrowProperty"]["terminals"]
        assert ends["start"]["top"] == ends["end"]["top"] == 540

    asyncio.run(scenario())
