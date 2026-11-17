"""Offline guards of scope moves, variables, visibility, report links and the PDF request."""

import asyncio
import base64
import copy

import pytest

from datastudio_mcp.bigquery import BigQuerySource
from datastudio_mcp.browser import Config, StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.publishing import Publishing
from datastudio_mcp.scope import Scope
from datastudio_mcp.service import Studio
from datastudio_mcp.variables import ReportVariable, Variables
from datastudio_mcp.visual import Visual

REPORT = "11111111-1111-4111-8111-111111111111"


def chart(cid):
    return {"componentId": cid, "type": "simple-barchart", "opaque": {"keep": cid}}


class Session:
    """getReport-only fake: every test here stops before a write (plans and refusals)."""

    def __init__(self, root):
        self.config = Config(REPORT, root, root / "journal")
        self.raw = {
            "shareable": {"id": REPORT, "name": "My report"},
            "version": "1",
            "report": {"componentConfig": [chart("cd-header")],
                       "attributeConfig": {"reportAttribute": {"width": 1400, "height": 1000}},
                       "resource": {"unifiedParameterResource": [
                           {"reportParameterConfig": {"parameterKey": "show", "parameter": {
                               "parameterDisplayConfiguration": {"displayName": "Show"}}}}]}},
            "page": [{"pageId": "p_a", "version": "1", "chartCondition": [],
                      "page": {"componentConfig": [chart("cd-a"), chart("cd-b"),
                                                   chart("cd-c")],
                               "groupConfig": [{"componentId": ["cd-c"]}]}},
                     {"pageId": "p_b", "version": "1", "chartCondition": [],
                      "page": {"componentConfig": [], "groupConfig": [],
                               "attributeConfig": {"pageAttribute": {"width": 800}}}}],
            "navigationInfo": {"navItems": [{"page": {"pageId": "p_a", "displayName": "A"}},
                                            {"page": {"pageId": "p_b", "displayName": "B"}}]},
            "chartCondition": [],
        }
        self.evaluated = []

    async def post(self, endpoint, body):
        assert endpoint == "getReport", endpoint
        return {"reportConfig": copy.deepcopy(self.raw)}

    async def page(self):
        return self

    async def evaluate(self, script, arguments):
        self.evaluated.append(arguments)
        return {"status": 200, "data": base64.b64encode(b"%PDF-1.4 test").decode()}


def run(coro):
    return asyncio.run(coro)


def studio(tmp_path):
    session = Session(tmp_path)
    return Studio(session), session


def test_move_to_report_plans_and_refuses_grouped_missing_and_formatted(tmp_path):
    s, session = studio(tmp_path)
    plan = run(Scope(s).move("p_a", ["cd-a"], "report"))
    assert plan["status"] == "planned" and plan["to"] == "report"
    for ids, code in ((["cd-c"], "GROUPED_COMPONENT"), (["cd-x"], "NOT_FOUND"),
                      (["cd-a", "cd-a"], "INVALID_BATCH")):
        with pytest.raises(StudioError) as error:
            run(Scope(s).move("p_a", ids, "report"))
        assert error.value.code == code
    with pytest.raises(StudioError) as error:  # A report component is not on the page.
        run(Scope(s).move("p_a", ["cd-a"], "page"))
    assert error.value.code == "NOT_FOUND"
    session.raw["page"][0]["chartCondition"] = [{"chartId": "cd-a"}]
    with pytest.raises(StudioError) as error:
        run(Scope(s).move("p_a", ["cd-a"], "report"))
    assert error.value.code == "CONDITIONAL_FORMATTING_MOVE_UNSUPPORTED"


def test_copy_to_page_gives_new_ids_and_refuses_same_page_and_groups(tmp_path):
    s, _ = studio(tmp_path)
    plan = run(Scope(s).copy_to_page("p_a", ["cd-a", "cd-b"], "p_b"))
    mapping = plan["created_component_ids"]
    assert set(mapping) == {"cd-a", "cd-b"} and len(set(mapping.values())) == 2
    assert all(new.startswith("cd-") and new not in mapping for new in mapping.values())
    for args, code in ((("p_a", ["cd-a"], "p_a"), "SAME_PAGE"),
                       (("p_a", ["cd-c"], "p_b"), "GROUPED_COMPONENT")):
        with pytest.raises(StudioError) as error:
            run(Scope(s).copy_to_page(*args))
        assert error.value.code == code


def variable(key):
    return ReportVariable(parameter_key=key, parameter={"parameterDisplayConfiguration": {
        "displayName": key}})


def test_variables_upsert_delete_and_refuse_bound_or_unknown(tmp_path):
    s, session = studio(tmp_path)
    listed = run(Variables(s).list())
    assert [v["parameter_key"] for v in listed["variables"]] == ["show"]
    token = listed["fingerprint"]
    plan = run(Variables(s).write([variable("show"), variable("extra")], [], token))
    assert [e["reportParameterConfig"]["parameterKey"] for e in plan["after"]] == [
        "show", "extra"]
    assert run(Variables(s).write([], ["show"], token))["after"] == []
    with pytest.raises(StudioError) as error:
        run(Variables(s).write([], ["nope"], token))
    assert error.value.code == "NOT_FOUND"
    with pytest.raises(StudioError) as error:
        run(Variables(s).write([variable("x")], [], "stale"))
    assert error.value.code == "VERSION_CONFLICT"
    session.raw["page"][0]["page"]["componentConfig"][0]["attributeConfig"] = {
        "componentAttribute": {"visibilityParameterSourceKey": "REPORT_VARIABLE:show"}}
    with pytest.raises(StudioError) as error:
        run(Variables(s).write([], ["show"], token))
    assert error.value.code == "VARIABLE_IN_USE"


def test_visibility_binds_component_or_page_and_checks_the_key(tmp_path):
    s, session = studio(tmp_path)
    token = fingerprint(session.raw["page"][0]["page"])
    plan = run(Variables(s).visibility("p_a", "cd-a", "REPORT_VARIABLE:show", token))
    assert plan["attributes"] == {"visibilityParameterSourceKey": "REPORT_VARIABLE:show"}
    page = run(Variables(s).visibility("p_a", None, "DATASOURCE_VARIABLE:ds1:k", token))
    assert page["status"] == "planned"
    assert run(Variables(s).visibility("p_a", "cd-a", None, token))["status"] == "unchanged"
    for key, code in (("REPORT_VARIABLE:missing", "NOT_FOUND"), ("OTHER:x", "INVALID_SOURCE_KEY")):
        with pytest.raises(StudioError) as error:
            run(Variables(s).visibility("p_a", "cd-a", key, token))
        assert error.value.code == code


def test_links_use_page_size_over_report_size(tmp_path):
    s, _ = studio(tmp_path)
    links = run(Publishing(s).links("p_b"))
    assert links["view"].endswith(f"/reporting/{REPORT}/page/p_b")
    assert links["edit"] == links["view"] + "/edit" and "/embed/reporting/" in links["embed"]
    assert 'width="800" height="1000"' in links["iframe"] and links["embeddable"] is False
    with pytest.raises(StudioError):
        run(Publishing(s).links("p_missing"))


def test_report_pdf_requests_navigation_order_and_saves_the_file(tmp_path):
    s, session = studio(tmp_path)
    target = tmp_path / "out.pdf"
    result = run(Visual(s).report_pdf(str(target), page_numbers=True))
    body = session.evaluated[0]["body"]
    assert body["pageIds"] == ["p_a", "p_b"] and body["pageNames"] == ["A", "B"]
    assert body["reportName"] == "My_report" and body["width"] == 1400
    assert body["pageSettings"][1] == {"pageId": "p_b", "name": "B", "width": 800}
    assert body["pdfSettings"]["addPageNumbers"] is True and body["layoutType"] == 1
    assert target.read_bytes().startswith(b"%PDF") and result["bytes"] == target.stat().st_size
    for path, pages, code in (("x.png", None, "INVALID_PATH"),
                              (str(target), ["p_zz"], "NOT_FOUND")):
        with pytest.raises(StudioError) as error:
            run(Visual(s).report_pdf(path, pages))
        assert error.value.code == code


def test_custom_query_parameters_match_the_editor_checkboxes():
    connector = BigQuerySource(project_id="p", sql="SELECT 1", date_range_params=True,
                               viewer_email_param=True).connector_config()["connectorConfig"]
    params = connector["parameterConfiguration"]["parameters"]
    assert [p["key"] for p in params] == ["DS_START_DATE", "DS_END_DATE", "DS_USER_EMAIL"]
    start = params[0]["value"]
    assert [r["key"] for r in start["parametersModifiedByRoles"]] == [1, 2]
    assert start["parametersModifiedByRoles"][0]["value"]["dataType"] == 5
    assert start["parameterDisplayConfiguration"]["displayType"] == 6
    assert params[2]["value"]["parameterDisplayConfiguration"]["displayType"] == 1
    assert "parameterConfiguration" not in BigQuerySource(
        project_id="p", sql="SELECT 1").connector_config()["connectorConfig"]
    with pytest.raises(ValueError):
        BigQuerySource(project_id="p", dataset_id="d", table_id="t", date_range_params=True)
