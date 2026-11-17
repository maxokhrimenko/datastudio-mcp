import asyncio
import copy

import pytest
from test_service import Session

from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.nativeops import NativeOps
from datastudio_mcp.service import Studio


class Saved(Session):
    async def post(self, endpoint, body):
        raw = await super().post(endpoint, body)
        if endpoint == "getReport":
            raw["reportConfig"]["version"] = "3"
            raw["reportConfig"]["report"] = {"theme": "a", "opaque": [1]}
        return raw


class OpsSession:
    """Saved-state fake plus the browser side of NativeOps: page().evaluate and acquire()."""

    def __init__(self, root, *results):
        self.saved = Saved(root)
        self.config = self.saved.config
        self.results = list(results)
        self.evaluations = []
        self.acquires = []

    page_doc = property(lambda self: self.saved.page)
    writes = property(lambda self: self.saved.writes)

    async def page(self):
        return self

    conditions_seen = ([], [])  # model's chartCondition before / after the operation

    async def evaluate(self, script, arguments):
        if ".chartCondition)" in script:  # NativeOps reads conditional formatting
            first = not getattr(self, "_conditions_read", False)
            self._conditions_read = first
            return {"conditions": copy.deepcopy(self.conditions_seen[0 if first else 1])}
        self.evaluations.append(arguments)
        return copy.deepcopy(self.results.pop(0))

    async def acquire(self, page_id=None, *, reload=False):
        self.acquires.append(reload)

    async def post(self, endpoint, body):
        return await self.saved.post(endpoint, body)


NEW = {"componentId": "cd-new", "type": "simple-description"}


def added(session):
    before = copy.deepcopy(session.page_doc)
    after = copy.deepcopy(before)
    after["componentConfig"].append(NEW)
    return {"before": before, "after": after, "created": ["cd-new"]}


def run(session, *, expected=None, dry_run=False, page_id="p_test", guard=None):
    token = expected or fingerprint(session.page_doc)
    return NativeOps(Studio(session)).run(
        "script", {"x": 1}, page_id=page_id, expected_fingerprint=token,
        dry_run=dry_run, plan={"plan": True}, guard=guard)


def test_dry_run_plans_the_page_delta_without_writing(tmp_path):
    session = OpsSession(tmp_path)
    session.results = [added(session)]
    result = asyncio.run(run(session, dry_run=True))
    assert result["status"] == "planned" and result["plan"] and result["created"] == ["cd-new"]
    assert result["after"]["componentConfig"][-1] == NEW
    assert session.writes == 0
    assert session.evaluations[0]["pageId"] == "p_test" and session.evaluations[0]["version"] == "1"


def test_write_keeps_unknown_saved_fields_and_reloads_the_viewer_model(tmp_path):
    session = OpsSession(tmp_path)
    original = copy.deepcopy(session.page_doc)
    ops = added(session)
    ops["after"]["componentConfig"][0]["text"] = "native"  # Only the delta may be applied.
    ops["before"]["componentConfig"][0]["opaque"] = "serializer lost it"
    ops["after"]["componentConfig"][0]["opaque"] = "serializer lost it"
    session.results = [ops]
    result = asyncio.run(run(session))
    assert result["status"] == "verified" and session.writes == 1
    expected = {**original["componentConfig"][0], "text": "native"}
    assert session.page_doc["componentConfig"][0] == expected
    assert session.page_doc["componentConfig"][1] == NEW
    assert session.acquires == [True]


def test_unchanged_operation_writes_nothing(tmp_path):
    session = OpsSession(tmp_path)
    page = copy.deepcopy(session.page_doc)
    session.results = [{"before": page, "after": copy.deepcopy(page)}]
    assert asyncio.run(run(session))["status"] == "unchanged"
    assert session.writes == 0 and session.acquires == [True]


def test_new_null_members_are_not_written(tmp_path):
    session = OpsSession(tmp_path)
    ops = added(session)
    ops["after"]["componentConfig"][-1] = {**NEW, "unsetField": None}
    session.results = [ops]
    asyncio.run(run(session))
    assert session.page_doc["componentConfig"][-1] == NEW


def test_stale_fingerprint_stops_before_the_native_operation(tmp_path):
    session = OpsSession(tmp_path)
    with pytest.raises(StudioError) as error:
        asyncio.run(run(session, expected="old"))
    assert error.value.code == "VERSION_CONFLICT"
    assert session.evaluations == [] and session.writes == 0


def test_report_scope_merges_over_saved_report_and_drops_chart_condition(tmp_path):
    session = OpsSession(tmp_path)
    ops = {"before": {"theme": "a"}, "after": {"theme": "b", "chartCondition": []}}
    session.results = [ops]
    saved = {"theme": "a", "opaque": [1]}
    result = asyncio.run(run(session, page_id=None, expected=fingerprint(saved), dry_run=True))
    assert result["after"] == {"theme": "b", "opaque": [1]}
    assert "pageId" in session.evaluations[0] and session.evaluations[0]["pageId"] is None
    assert session.evaluations[0]["version"] == "3"


def test_report_scope_reloads_the_operated_model_before_writing(tmp_path):
    # Reloaded after the write, an operated report model makes the editor save the
    # published revision's report-level content over the draft.
    session = OpsSession(tmp_path)
    session.results = [{"before": {"theme": "a"}, "after": {"theme": "b"}}]
    post = session.post

    async def recording(endpoint, body):
        if endpoint == "updateReport":
            session.acquires.append("write")
            return {}
        return await post(endpoint, body)

    session.post = recording
    asyncio.run(run(session, page_id=None, expected=fingerprint({"theme": "a", "opaque": [1]})))
    assert session.acquires == [True, "write", True]


def test_loaded_model_differing_from_saved_raises_editor_stale_without_writing(tmp_path):
    # server.call() owns the single reload-and-rerun; NativeOps only refuses and reloads.
    session = OpsSession(tmp_path)
    drifted = added(session)
    for side in ("before", "after"):
        drifted[side]["componentConfig"].append({"componentId": "human", "type": "x"})
    session.results = [drifted]
    with pytest.raises(StudioError) as error:
        asyncio.run(run(session))
    assert error.value.code == "EDITOR_STALE"
    assert len(session.evaluations) == 1 and session.writes == 0 and session.acquires == [True]


def test_guard_runs_before_and_after_and_failure_still_reloads(tmp_path):
    session = OpsSession(tmp_path)
    session.results = [added(session)]
    calls = []

    async def guard():
        calls.append(len(session.evaluations))

    asyncio.run(run(session, dry_run=True, guard=guard))
    assert calls == [0, 1]

    session = OpsSession(tmp_path)
    session.results = [added(session)]

    async def broken():
        raise StudioError("VERSION_CONFLICT", "source changed")

    with pytest.raises(StudioError):
        asyncio.run(run(session, guard=broken))
    assert session.evaluations == [] and session.writes == 0 and session.acquires == [True]


def test_native_error_result_is_raised_with_its_code(tmp_path):
    session = OpsSession(tmp_path, {"error": "UNKNOWN_TYPE_OR_PRESET", "details": "x/y"})
    with pytest.raises(StudioError) as error:
        asyncio.run(run(session))
    assert error.value.code == "UNKNOWN_TYPE_OR_PRESET"
    assert session.writes == 0 and session.acquires == [True]


def test_steps_are_one_write_named_on_failure_and_continue_on_the_changed_model(tmp_path):
    session = OpsSession(tmp_path)
    first, second = added(session), added(session)
    second["before"] = copy.deepcopy(first["after"])
    second["after"]["componentConfig"].append(
        {"componentId": "cd-two", "type": "simple-description"})
    session.results = [first, second]
    result = asyncio.run(NativeOps(Studio(session)).run(
        "script", [{"a": 1}, {"a": 2}], page_id="p_test",
        expected_fingerprint=fingerprint(session.page_doc), dry_run=True))
    ids = [c["componentId"] for c in result["after"]["componentConfig"]]
    assert ids[-2:] == ["cd-new", "cd-two"]
    assert [e["continuing"] for e in session.evaluations] == [False, True]
    assert session.writes == 0
    session = OpsSession(tmp_path)
    session.results = [added(session), {"error": "PROPERTY_DISABLED", "details": "k"}]

    async def failing(script, arguments):
        if ".chartCondition)" in script:
            return {"conditions": []}
        out = session.results.pop(0)
        if out.get("error"):
            raise StudioError(out["error"], out["details"])
        return out

    ops = NativeOps(Studio(session))
    ops.evaluate = failing
    with pytest.raises(StudioError) as error:
        asyncio.run(ops.run("script", [{}, {}], page_id="p_test",
                            expected_fingerprint=fingerprint(session.page_doc), dry_run=True))
    assert error.value.code == "PROPERTY_DISABLED" and "step 1" in str(error.value)


def test_conditional_formatting_change_alone_is_written(tmp_path):
    session = OpsSession(tmp_path)
    same = {"before": copy.deepcopy(session.page_doc), "after": copy.deepcopy(session.page_doc)}
    session.results = [same]
    rule = [{"chartId": "cd-test", "conditionalFormat": {"rule": [{"x": 1}]}}]
    session.conditions_seen = ([], rule)
    result = asyncio.run(run(session, dry_run=True))
    assert result["status"] == "planned" and result["chart_conditions"] == rule
