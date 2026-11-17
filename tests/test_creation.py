import asyncio
import copy

import pytest
from pydantic import ValidationError
from test_nativeops import OpsSession
from test_switching import SwitchAdapter, SwitchSession

from datastudio_mcp.browser import StudioError
from datastudio_mcp.creation import Creation, NewComponent, SourceField, creation_requests
from datastudio_mcp.document import fingerprint
from datastudio_mcp.service import Studio


def test_creation_formula_and_label_requests_require_validated_columns():
    spec = NewComponent(type="simple-table", datasource_id="ds", dimensions=[
        {"name": "month", "display_name": "Month label"}], metrics=[
        {"expression": "SUM(t0.revenue)", "aggregation": 7, "display_name": "Total"}])
    with pytest.raises(StudioError) as caught:
        creation_requests([spec])
    assert caught.value.code == "UNVALIDATED_FORMULA"
    request = creation_requests([spec], {("0", "SUM(t0.revenue)"): {"type": 1, "dataType": 2}})[0]
    definitions = request["field_definitions"]
    assert definitions["dimensions"][0]["queryTimeTransformation"]["displayTransformation"]["displayName"] == "Month label"
    metric = definitions["metrics"][0]
    data = metric["queryTimeTransformation"]["dataTransformation"]
    assert data["sourceFieldName"] == metric["name"]
    assert data["sourceType"] == 1 and data["aggregation"] == 7
    assert data["textFormula"] == data["frontendTextFormula"] == "SUM(t0.revenue)"
    assert data["formulaOutputDataType"] == 2


def test_creation_rejects_invalid_formula_before_native_draft(tmp_path):
    class Adapter(SwitchAdapter):
        async def evaluate(self, script, arguments):
            assert "expression" in arguments and "specs" not in arguments
            return {"dataRequest": arguments}

        async def post(self, endpoint, body):
            if endpoint == "validateFormula":
                return {"errorMessage": "Invalid expression"}
            return await super().post(endpoint, body)

    async def scenario():
        session = SwitchSession(tmp_path)
        with pytest.raises(StudioError) as caught:
            await Creation(Studio(Adapter(session, []))).create("p_test", [NewComponent(
                type="simple-table", datasource_id="ds", metrics=[
                    {"expression": "INVALID()", "aggregation": 7}])], fingerprint(session.page), dry_run=False)
        assert caught.value.code == "FORMULA_VALIDATION_FAILED"
        assert session.writes == 0
    asyncio.run(scenario())


def test_creation_refuses_a_chart_formula_when_field_editing_is_off(tmp_path):
    async def scenario():
        session = SwitchSession(tmp_path)
        session.published["isUnlocked"] = False
        with pytest.raises(StudioError) as caught:
            await Creation(Studio(SwitchAdapter(session, []))).create("p_test", [NewComponent(
                type="kpi-metric", datasource_id="ds", metrics=[
                    {"expression": "SUM(x)", "aggregation": 7}])], fingerprint(session.page),
                dry_run=True)
        assert caught.value.code == "FIELD_EDITING_DISABLED" and session.writes == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("mode,code", [
    ("draft", "UNPUBLISHED_DATASOURCE"), ("concurrent", "VERSION_CONFLICT")])
def test_creation_source_guards_at_browser_boundary(tmp_path, mode, code, dry_run):
    class Adapter(SwitchAdapter):
        evaluations = 0

        async def acquire(self, page_id=None, *, reload=False):
            return None

        async def evaluate(self, script, arguments):
            if ".chartCondition)" in script:  # conditional formatting read
                return {"conditions": []}
            self.evaluations += 1
            assert arguments["specs"][0]["datasource_id"] == "ds"
            self.saved.draft["revision"] += 1
            self.saved.published = copy.deepcopy(self.saved.draft)
            after = copy.deepcopy(self.saved.page)
            after["componentConfig"].append({"componentId": "new", "type": "simple-table"})
            return {"before": copy.deepcopy(self.saved.page), "after": after,
                    "created_component_ids": ["new"]}

    async def scenario():
        session = SwitchSession(tmp_path)
        before = copy.deepcopy(session.page)
        if mode == "draft":
            session.draft["unpublished"] = True
        adapter = Adapter(session, [])
        with pytest.raises(StudioError) as caught:
            await Creation(Studio(adapter)).create(
                "p_test", [NewComponent(type="simple-table", datasource_id="ds")],
                fingerprint(before), dry_run=dry_run)
        assert caught.value.code == code
        assert adapter.evaluations == (0 if mode == "draft" else 1)
        assert session.writes == 0 and session.page == before

    asyncio.run(scenario())


def test_spec_rejects_invalid_geometry_and_boolean_aggregation():
    for values in ({"width": -1}, {"height": float("nan")}, {"left": True}):
        with pytest.raises(ValidationError):
            NewComponent(type="simple-table", **values)
    with pytest.raises(ValidationError):
        SourceField(name="revenue", aggregation=True)


def test_create_dry_run_write_and_restore(tmp_path):
    async def scenario():
        session = OpsSession(tmp_path)
        studio = Studio(session)
        creation = Creation(studio)
        original = copy.deepcopy(session.page_doc)
        added = {"componentId": "cd-new", "type": "simple-description"}

        def native_result(component_id):
            after = copy.deepcopy(session.page_doc)
            after["componentConfig"].append({**added, "componentId": component_id})
            return {"before": copy.deepcopy(session.page_doc), "after": after,
                    "created_component_ids": [component_id]}

        specs = [NewComponent(type="simple-description")]
        token = fingerprint(original)
        session.results = [native_result("cd-new")]
        plan = await creation.create("p_test", specs, token)
        assert plan["created_component_ids"] == ["cd-new"] and session.writes == 0
        assert plan["added_components"] == [added]
        session.results = [native_result("cd-new")]
        result = await creation.create("p_test", specs, token, dry_run=False)
        assert result["status"] == "verified"
        assert session.page_doc["componentConfig"] == original["componentConfig"] + [added]
        with pytest.raises(StudioError, match="changed"):
            await creation.create("p_test", specs, token, dry_run=False)
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified" and session.page_doc == original
        assert session.writes == 2

    asyncio.run(scenario())


def test_preflight_requires_source_and_metric_aggregation(tmp_path):
    async def scenario():
        session = OpsSession(tmp_path)
        creation = Creation(Studio(session))
        with pytest.raises(StudioError, match="datasource"):
            await creation.create(
                "p_test",
                [NewComponent(type="simple-table", dimensions=[SourceField(name="x")])],
                "unused",
            )
        with pytest.raises(StudioError, match="aggregation"):
            await creation.create(
                "p_test",
                [
                    NewComponent(
                        type="simple-table", datasource_id="source", metrics=[SourceField(name="x")]
                    )
                ],
                "unused",
            )
        assert session.writes == 0

    asyncio.run(scenario())
