import asyncio
import copy
from types import SimpleNamespace

import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.service import Studio
from datastudio_mcp.switching import (
    Switching,
    TypeChange,
    conversion_requests,
    merge_type_switch,
    requires_loss_approval,
    switch_changes,
)
from test_service import REPORT, Session


def test_conversion_target_fields_preserve_omission_and_explicit_empty_roles():
    requests = conversion_requests(
        [
            TypeChange(component_id="c", type="simple-table"),
            TypeChange(
                component_id="d",
                type="simple-table",
                dimensions=[],
                metrics=[{"name": "revenue", "aggregation": 6}],
            ),
        ]
    )
    assert requests[0]["properties"] == []
    props = requests[1]["properties"]
    assert props[0] == {"key": "actionConceptList", "value": []}
    assert props[1]["key"] == "metrics"
    assert props[1]["value"][0]["queryTimeTransformation"]["dataTransformation"] == {
        "sourceFieldName": "revenue",
        "aggregation": 6,
    }
    with pytest.raises(StudioError) as caught:
        conversion_requests(
            [TypeChange(component_id="c", type="simple-table", metrics=[{"name": "x"}])]
        )
    assert caught.value.code == "MISSING_AGGREGATION"


def test_conversion_formula_requires_validation_and_preserves_formula_metadata():
    changes = [
        TypeChange(
            component_id="c",
            type="simple-table",
            metrics=[
                {
                    "expression": "SUM(t0.revenue)",
                    "aggregation": 7,
                    "display_name": "Revenue total",
                }
            ],
        )
    ]
    with pytest.raises(StudioError) as caught:
        conversion_requests(changes)
    assert caught.value.code == "UNVALIDATED_FORMULA"
    request = conversion_requests(changes, {("c", "SUM(t0.revenue)"): {"dataType": 2, "type": 1}})[
        0
    ]
    definition = request["properties"][0]["value"][0]
    transform = definition["queryTimeTransformation"]
    assert transform["dataTransformation"] == {
        "sourceFieldName": definition["name"],
        "sourceType": 1,
        "textFormula": "SUM(t0.revenue)",
        "frontendTextFormula": "SUM(t0.revenue)",
        "formulaOutputDataType": 2,
        "aggregation": 7,
    }
    assert transform["displayTransformation"]["displayName"] == "Revenue total"


def source():
    return {
        "componentId": "c",
        "type": "simple-combochart",
        "preset": "default",
        "datasourceId": "ds",
        "propertyConfig": {
            "componentProperty": {
                "comboChartProperty": {"fontSize": 12},
                "dimensions": {"primary": ["d"]},
                "metrics": {"primary": ["m"]},
                "sort": ["d"],
            }
        },
        "conceptDefs": [
            {"name": "d", "source": "date"},
            {"name": "m", "expression": "ABS(revenue)"},
        ],
    }


def test_type_switch_preserves_opaque_metadata_and_reports_removed_panel():
    before = source()
    saved = copy.deepcopy(before)
    saved["opaque"] = {"keep": [1, 2]}
    saved["conceptDefs"][1]["opaque"] = {"metric": True}
    saved["propertyConfig"]["componentProperty"]["comboChartProperty"]["unknown"] = "old panel"
    after = copy.deepcopy(before)
    after["type"] = "simple-table"
    props = after["propertyConfig"]["componentProperty"]
    del props["comboChartProperty"]
    props["tableProperty"] = {"fontSize": 12}
    after["conceptDefs"] = [{"name": "new", "source": "count"}, *after["conceptDefs"][::-1]]
    merged, preview = merge_type_switch(before, after, saved)
    assert merged["type"] == "simple-table" and merged["componentId"] == "c"
    assert merged["opaque"] == {"keep": [1, 2]}
    assert merged["conceptDefs"][1]["opaque"] == {"metric": True}
    assert merged["conceptDefs"][2]["name"] == "d" and "opaque" not in merged["conceptDefs"][2]
    assert preview["added_concept_names"] == ["new"] and preview["removed_concept_names"] == []
    assert preview["bindings_before"] == preview["bindings_after"]
    removed = next(c for c in preview["changes"] if c["kind"] == "removed")
    assert removed == {
        "kind": "removed",
        "path": "/propertyConfig/componentProperty/comboChartProperty",
        "before": {"fontSize": 12, "unknown": "old panel"},
    }
    assert source() == before


def test_type_switch_reports_removed_metric_and_changed_formula():
    before = source()
    after = copy.deepcopy(before)
    after["conceptDefs"] = [{"name": "d", "source": "other_date"}]
    after["propertyConfig"]["componentProperty"]["metrics"] = {"primary": []}
    _, preview = merge_type_switch(before, after, before)
    assert preview["removed_concept_names"] == ["m"]
    assert preview["changed_concept_names"] == ["d"]
    assert preview["bindings_before"] != preview["bindings_after"]


@pytest.mark.parametrize("key,value", [("componentId", "other"), ("datasourceId", "other")])
def test_type_switch_refuses_identity_and_source_replacement(key, value):
    before = source()
    after = copy.deepcopy(before)
    after[key] = value
    with pytest.raises(StudioError) as caught:
        merge_type_switch(before, after, before)
    assert caught.value.code == "UNEXPECTED_SIDE_EFFECT"


def test_type_switch_rejects_stale_concept_inventory():
    before = source()
    saved = copy.deepcopy(before)
    saved["conceptDefs"].append({"name": "human_added"})
    with pytest.raises(StudioError) as caught:
        merge_type_switch(before, before, saved)
    assert caught.value.code == "EDITOR_STALE"


def test_switch_change_paths_distinguish_null_missing_and_array_reorder():
    changes = switch_changes({"a/b": None, "~list": [1, 2]}, {"~list": [2, 1], "added": None})
    assert {c["path"]: c["kind"] for c in changes} == {
        "/a~1b": "removed",
        "/~0list": "changed",
        "/added": "added",
    }
    assert switch_changes({"number": 1}, {"number": 1.0}) == []


def test_switch_approval_distinguishes_added_defaults_from_changed_settings():
    before = source()
    after = copy.deepcopy(before)
    after["type"] = "simple-table"
    after["conceptDefs"].append({"name": "default_added"})
    _, preview = merge_type_switch(before, after, before)
    assert not requires_loss_approval(preview)
    after["propertyConfig"]["componentProperty"]["comboChartProperty"]["fontSize"] = 18
    _, preview = merge_type_switch(before, after, before)
    assert requires_loss_approval(preview)


def test_switch_approval_requires_permission_for_dropped_panel_or_binding():
    before = source()
    after = copy.deepcopy(before)
    del after["propertyConfig"]["componentProperty"]["comboChartProperty"]
    _, preview = merge_type_switch(before, after, before)
    assert requires_loss_approval(preview)
    after = copy.deepcopy(before)
    after["propertyConfig"]["componentProperty"]["metrics"] = {"primary": []}
    _, preview = merge_type_switch(before, after, before)
    assert requires_loss_approval(preview)


class ConversionBrowser:
    """CDP boundary fixture, including native layout inspection and cleanup."""

    def __init__(self, result, cleanup_failures=(), transport_failure=False):
        self.config = SimpleNamespace(report_id="report", timeout_ms=1000)
        self.context = self
        self.result = result
        self.cleanup_failures = cleanup_failures
        self.transport_failure = transport_failure
        self.cleanup_calls = []

    async def page(self):
        return self

    async def evaluate(self, script, arguments):
        assert arguments == {"reportId": "report", "pageId": "page", "version": "1"}
        return {"static": True}

    async def new_cdp_session(self, page):
        assert page is self
        return self

    async def send(self, method, parameters):
        if method == "Runtime.releaseObjectGroup":
            assert parameters["objectGroup"].startswith("datastudio-switch-")
            self.cleanup_calls.append("release")
            if "release" in self.cleanup_failures:
                raise RuntimeError("private transport detail")
            return {}
        if self.transport_failure:
            raise RuntimeError("private transport detail")
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "switch"}}
        if method == "Runtime.callFunctionOn":
            if parameters["objectId"] == "switch":
                return {"result": {"value": "function(a,b,c){return converter(this,a,b,c)}"}}
            assert parameters["objectId"] == "converter"
            return {"result": {"value": self.result}}
        assert method == "Runtime.getProperties"
        if parameters["objectId"] == "switch":
            return {"internalProperties": [{"name": "[[Scopes]]", "value": {"objectId": "scopes"}}]}
        if parameters["objectId"] == "scopes":
            return {"result": [{"name": "0", "value": {"objectId": "scope"}}]}
        assert parameters["objectId"] == "scope"
        return {
            "result": [
                {"name": "converter", "value": {"objectId": "converter", "type": "function"}}
            ]
        }

    async def detach(self):
        self.cleanup_calls.append("detach")
        if "detach" in self.cleanup_failures:
            raise RuntimeError("private transport detail")


@pytest.mark.parametrize("cleanup_failures", [(), ("release",), ("detach",), ("release", "detach")])
@pytest.mark.parametrize(
    "conversion_error", [None, "INVALID_COMPONENT_CONFIGURATION", "UNEXPECTED_SIDE_EFFECT"]
)
def test_conversion_cleans_both_resources_and_preserves_primary_error(
    cleanup_failures, conversion_error
):
    async def scenario():
        edits = [{"before": source(), "after": source()}]
        value = (
            {"error": conversion_error}
            if conversion_error
            else {"edits": edits, "shared_state_unchanged": True}
        )
        browser = ConversionBrowser(value, cleanup_failures)
        switching = Switching(SimpleNamespace(session=browser))
        expected_error = conversion_error or (
            "NATIVE_CONTRACT_FAILED" if cleanup_failures else None
        )
        operation = switching._prepare(
            {"pageId": "page", "version": 1}, [TypeChange(component_id="c", type="simple-table")]
        )
        if expected_error:
            with pytest.raises(StudioError) as caught:
                await operation
            assert caught.value.code == expected_error
            assert "private transport detail" not in str(caught.value)
        else:
            assert await operation == edits
        assert browser.cleanup_calls == ["release", "detach"]

    asyncio.run(scenario())


def test_conversion_transport_failure_still_releases_and_detaches():
    async def scenario():
        browser = ConversionBrowser({}, ("release", "detach"), transport_failure=True)
        with pytest.raises(StudioError) as caught:
            await Switching(SimpleNamespace(session=browser))._prepare(
                {"pageId": "page", "version": 1},
                [TypeChange(component_id="c", type="simple-table")],
            )
        assert caught.value.code == "NATIVE_CONTRACT_FAILED"
        assert "transport failed" in str(caught.value)
        assert browser.cleanup_calls == ["release", "detach"]

    asyncio.run(scenario())


class SwitchSession(Session):
    def __init__(self, root):
        super().__init__(root)
        self.page["componentConfig"] = [source(), {**source(), "componentId": "second"}]
        self.draft = {"datasourceId": "ds", "revision": 1}
        self.published = copy.deepcopy(self.draft)

    async def post(self, endpoint, body):
        if endpoint == "getClaimedDatasources":
            return {"claimedDatasources": [{"basicEntityInfo": {"id": "ds", "isEmbedded": True}}]}
        if endpoint == "getBlockDatasource":
            assert body["datasourceId"] == "ds"
            return {
                "draft": copy.deepcopy(self.draft),
                "published": copy.deepcopy(self.published),
                "isEditable": True,
                "embeddedInReportId": REPORT,
            }
        return await super().post(endpoint, body)


class SwitchAdapter(ConversionBrowser):
    def __init__(self, saved, edits, concurrent=None):
        super().__init__({"edits": edits, "shared_state_unchanged": True})
        self.saved = saved
        self.config = saved.config
        self.concurrent = concurrent

    async def evaluate(self, script, arguments):
        assert arguments == {"reportId": REPORT, "pageId": "p_test", "version": self.saved.version}
        return {"static": True}

    async def post(self, endpoint, body):
        return await self.saved.post(endpoint, body)

    async def send(self, method, parameters):
        if method == "Runtime.callFunctionOn" and parameters["objectId"] == "converter":
            if self.concurrent == "source":
                self.saved.draft["revision"] += 1
                self.saved.published = copy.deepcopy(self.saved.draft)
            elif self.concurrent == "page":
                self.saved.page["human_edit"] = True
                self.saved.version = str(int(self.saved.version) + 1)
        return await super().send(method, parameters)


@pytest.mark.parametrize(
    "mode,code", [("invalid", "FORMULA_VALIDATION_FAILED"), ("source", "VERSION_CONFLICT")]
)
def test_conversion_formula_failure_stops_before_native_conversion(tmp_path, mode, code):
    class FormulaAdapter(SwitchAdapter):
        async def evaluate(self, script, arguments):
            assert arguments == {"datasourceId": "ds", "expression": "SUM(t0.revenue)"}
            return {"dataRequest": arguments}

        async def post(self, endpoint, body):
            if endpoint == "validateFormula":
                if mode == "invalid":
                    return {"errorMessage": "Invalid expression"}
                self.saved.draft["revision"] += 1
                self.saved.published = copy.deepcopy(self.saved.draft)
                return {"column": {"enabled": True, "dataType": 2, "type": 1}}
            return await super().post(endpoint, body)

    async def scenario():
        saved = SwitchSession(tmp_path)
        before = copy.deepcopy(saved.page)
        adapter = FormulaAdapter(saved, [])
        with pytest.raises(StudioError) as caught:
            await Switching(Studio(adapter)).patch(
                "p_test",
                [
                    TypeChange(
                        component_id="c",
                        type="simple-table",
                        metrics=[{"expression": "SUM(t0.revenue)", "aggregation": 7}],
                    )
                ],
                fingerprint(before),
                dry_run=False,
                allow_loss=True,
            )
        assert caught.value.code == code
        assert adapter.cleanup_calls == [] and saved.writes == 0 and saved.page == before

    asyncio.run(scenario())


def conversion_edits(saved, ids):
    originals = {c["componentId"]: c for c in saved.page["componentConfig"]}
    edits = []
    for component_id in ids:
        before = copy.deepcopy(originals[component_id])
        after = copy.deepcopy(before)
        after["type"] = "simple-table"
        del after["propertyConfig"]["componentProperty"]["comboChartProperty"]
        after["propertyConfig"]["componentProperty"]["tableProperty"] = {"fontSize": 12}
        edits.append({"before": before, "after": after})
    return edits


@pytest.mark.parametrize(
    "case,code",
    [
        ("no_approval", "LOSS_APPROVAL_REQUIRED"),
        ("source", "VERSION_CONFLICT"),
        ("page", "VERSION_CONFLICT"),
        ("unpublished", "UNPUBLISHED_DATASOURCE"),
        ("reordered", "EDITOR_STALE"),
        ("missing", "NATIVE_CONTRACT_FAILED"),
        ("wrong_type", "NATIVE_CONTRACT_FAILED"),
    ],
)
def test_switch_batch_guards_prevent_all_api_writes(tmp_path, case, code):
    async def scenario():
        saved = SwitchSession(tmp_path)
        before = copy.deepcopy(saved.page)
        edits = conversion_edits(saved, ["second", "c"])
        if case == "unpublished":
            saved.draft["unpublished"] = True
        elif case == "reordered":
            edits.reverse()
        elif case == "missing":
            edits.pop()
        elif case == "wrong_type":
            edits[0]["after"]["type"] = "unexpected"
        studio = Studio(SwitchAdapter(saved, edits, concurrent=case))
        with pytest.raises(StudioError) as caught:
            await Switching(studio).patch(
                "p_test",
                [TypeChange(component_id=i, type="simple-table") for i in ["second", "c"]],
                fingerprint(before),
                dry_run=False,
                allow_loss=case != "no_approval",
            )
        assert caught.value.code == code
        assert saved.writes == 0
        if case == "page":
            assert saved.page == {**before, "human_edit": True}
        else:
            assert saved.page == before

    asyncio.run(scenario())


def test_switch_batch_preview_single_page_write_and_exact_restore(tmp_path):
    async def scenario():
        saved = SwitchSession(tmp_path)
        before = copy.deepcopy(saved.page)
        edits = conversion_edits(saved, ["second", "c"])
        studio = Studio(SwitchAdapter(saved, edits))
        switching = Switching(studio)
        changes = [TypeChange(component_id=i, type="simple-table") for i in ["second", "c"]]
        preview = await switching.patch("p_test", changes, fingerprint(before))
        assert preview["status"] == "planned" and preview["requires_allow_loss"] is True
        assert [c["component_id"] for c in preview["conversions"]] == ["second", "c"]
        assert saved.page == before and saved.writes == 0
        result = await switching.patch(
            "p_test", changes, fingerprint(before), dry_run=False, allow_loss=True
        )
        assert result["status"] == "verified" and saved.writes == 1
        assert saved.page["componentConfig"] == [edits[1]["after"], edits[0]["after"]]
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified" and saved.writes == 2
        assert saved.page == before

    asyncio.run(scenario())
