import copy

import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.filters import filter_inventory


def fixture():
    return {"report": {"resource": {"filter": {"future": 1, "entry": [
        {"key": "f1", "value": {"name": "Same name", "datasourceId": "ds", "opaque": 7}},
        {"key": "f2", "value": {"name": "Same name", "datasourceId": "removed"}},
    ]}}}, "page": []}


def test_catalog_uses_ids_not_names_and_preserves_unknown_definition_fields():
    config = fixture()
    before = copy.deepcopy(config)
    result = filter_inventory(config, {"ds"})
    first, second = result["filters"]
    assert first["id"] == "f1" and second["id"] == "f2"
    assert first["definition"]["opaque"] == 7
    assert first["datasource_attached"] and not second["datasource_attached"]
    assert first["has_no_exact_references"] and second["has_no_exact_references"]
    first["definition"]["opaque"] = 9
    assert config == before


def test_reference_walk_includes_pages_nested_groups_report_and_blend_resources():
    config = fixture()
    config["page"] = [{"pageId": "p", "page": {
        "propertyConfig": {"pageProperty": {"filters": ["f1"]}},
        "groupConfig": [{"groupId": "g", "componentConfig": [
            {"componentId": "c", "propertyConfig": {"componentProperty": {"filters": ["f1"]}}}]}],
    }}]
    config["report"]["componentConfig"] = [{"componentId": "report-control", "filters": ["f1"]}]
    config["report"]["resource"]["dataViewResource"] = {"entry": [{"key": "blend", "value": {
        "tables": [{"filters": ["f1", "missing"]}]}}]}
    result = filter_inventory(config, {"ds"})
    refs = result["filters"][0]["references"]
    assert len(refs) == 4
    assert all(ref["kind"] == "filter_list" for ref in refs)
    nested = next(ref for ref in refs if "groupConfig" in ref["path"])
    assert nested["owner"] == {"pageId": "p", "groupId": "g", "componentId": "c"}
    assert len(result["dangling_filter_references"]) == 1
    assert result["dangling_filter_references"][0]["filter_id"] == "missing"


def test_native_blend_filter_config_ids_are_explicit_bindings_not_unknown_strings():
    config = fixture()
    config["report"]["resource"]["dataViewResource"] = {"entry": [{"key": "blend", "value": {
        "blockDatasource": {"blocks": [{"treeQueryBlockConfig": {"join": {"right": {
            "query": {"filterConfig": {"filterIds": ["f1", "missing"]}}}}}}]}}}]}
    result = filter_inventory(config, set())
    assert result["filters"][0]["references"][0]["kind"] == "blend_filter_list"
    assert result["dangling_filter_references"][0]["kind"] == "blend_filter_list"
    assert result["dangling_filter_references"][0]["filter_id"] == "missing"


def test_unknown_exact_references_are_reported_but_substrings_are_not_matches():
    config = fixture()
    config["report"]["future/~key"] = {"str": "f1", "substring": "prefix-f2-suffix"}
    first, second = filter_inventory(config, set())["filters"]
    assert first["references"] == [{"path": "/report/future~1~0key/str", "owner": {},
                                    "kind": "unclassified_exact_reference"}]
    assert not first["has_no_exact_references"] and second["has_no_exact_references"]


@pytest.mark.parametrize("bad", [None, [], {"entry": None}, {"entry": [None]},
                                 {"entry": [{"key": "", "value": {}}]},
                                 {"entry": [{"key": "f", "value": {}}, {"key": "f", "value": {}}]}])
def test_malformed_registry_is_not_silently_an_empty_inventory(bad):
    config = fixture()
    config["report"]["resource"]["filter"] = bad
    with pytest.raises(StudioError):
        filter_inventory(config, set())


def test_valid_report_with_absent_filter_resource_is_empty_but_missing_pages_is_invalid():
    assert filter_inventory({"report": {}, "page": []}, set())["filters"] == []
    with pytest.raises(StudioError):
        filter_inventory({"report": {}}, set())
