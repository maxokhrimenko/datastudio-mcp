import copy

import pytest

from datastudio_mcp.blends import blend_inventory
from datastudio_mcp.browser import StudioError


def query(source="ds", **extra):
    return {"query": {"datasourceId": source, "concepts": [], **extra}}


def fixture(tree=None):
    return {"report": {"resource": {"dataViewResource": {"entry": [
        {"key": "b1", "value": {"opaque": {"future": [7]}, "blockDatasource": {
            "datasourceBlock": {"name": "Same name", "fields": [{"name": "x"}]},
            "blocks": [{"treeQueryBlockConfig": tree or query()}],
        }}},
    ]}}}, "page": []}


def test_nested_join_inputs_paths_and_raw_fields_are_lossless():
    inner = {"join": {"type": 99, "condition": {"future": [1]},
                      "left": query(), "right": query("unattached")}}
    config = fixture({"join": {"type": 3, "left": inner,
                               "right": query(filterConfig={"filterIds": ["f"]})}})
    before = copy.deepcopy(config)
    item = blend_inventory(config, {"ds"}, include_definitions=True)["blends"][0]
    assert [join["type_code"] for join in item["joins"]] == [3, 99]
    assert [q["datasource_attached"] for q in item["inputs"]] == [True, False, True]
    assert item["inputs"][2]["filter_ids"] == ["f"]
    assert item["inputs"][0]["path"].endswith("/join/left/join/left/query")
    assert not item["issues"]
    item["definition"]["opaque"]["future"].append(8)
    item["joins"][1]["condition"]["future"].append(8)
    item["inputs"][2]["query"]["filterConfig"]["filterIds"].append("g")
    assert config == before


def test_single_query_is_not_misrepresented_as_join_and_summary_omits_definition():
    item = blend_inventory(fixture(), {"ds"})["blends"][0]
    assert len(item["inputs"]) == 1 and item["joins"] == []
    assert "definition" not in item and "query" not in item["inputs"][0]
    assert item["has_no_exact_references"] and item["output_field_count"] == 1


def test_exact_reference_walk_includes_nested_owners_unknown_fields_and_dangling_ids():
    config = fixture()
    config["page"] = [{"pageId": "p", "page": {"groupConfig": [{
        "groupId": "g", "componentConfig": [{"componentId": "c", "binding": {
            "datasetType": 2, "datasetId": "b1"}}]}]}}]
    config["report"]["future/~key"] = ["b1", "prefix-b1-suffix"]
    config["report"]["componentConfig"] = [{"componentId": "root", "binding": {
        "datasetType": 2, "datasetId": "missing"}}]
    result = blend_inventory(config, set())
    refs = result["blends"][0]["references"]
    assert len(refs) == 2
    nested = next(ref for ref in refs if ref["kind"] == "dataset_binding")
    assert nested["owner"] == {"pageId": "p", "groupId": "g", "componentId": "c"}
    assert any(ref["path"] == "/report/future~1~0key/0" for ref in refs)
    assert result["dangling_dataset_references"][0]["owner"] == {"componentId": "root"}
    assert result["dangling_dataset_references"][0]["blend_id"] == "missing"


@pytest.mark.parametrize("bad", [None, [], {"entry": None}, {"entry": [None]},
                                 {"entry": [{"key": "", "value": {}}]},
                                 {"entry": [{"key": "x", "value": {}},
                                            {"key": "x", "value": {}}]}])
def test_malformed_registry_is_not_an_empty_inventory(bad):
    config = fixture()
    config["report"]["resource"]["dataViewResource"] = bad
    with pytest.raises(StudioError):
        blend_inventory(config, set())


@pytest.mark.parametrize("bad", [None, [], "invalid"])
def test_malformed_report_resource_uses_domain_error(bad):
    with pytest.raises(StudioError):
        blend_inventory({"report": {"resource": bad}, "page": []}, set())


def test_missing_structure_and_unknown_tree_are_explicit_issues_not_validity_claims():
    config = fixture({"futureQuery": {"opaque": 1}})
    item = blend_inventory(config, set(), include_definitions=True)["blends"][0]
    assert item["issues"][0]["kind"] == "unrecognized_tree_node"
    assert item["definition"] == config["report"]["resource"]["dataViewResource"]["entry"][0]["value"]
    config["report"]["resource"]["dataViewResource"]["entry"][0]["value"] = {}
    item = blend_inventory(config, set())["blends"][0]
    assert {issue["kind"] for issue in item["issues"]} == {
        "unrecognized_block_datasource", "malformed_blocks", "malformed_output_block"}


@pytest.mark.parametrize("bad", [None, [], {"filterIds": None}, {"filterIds": "f"},
                                 {"filterIds": [1]}, {"filterIds": [""]}])
def test_malformed_input_filters_are_reported_without_losing_raw_value(bad):
    item = blend_inventory(fixture(query(filterConfig=bad)), set(),
                           include_definitions=True)["blends"][0]
    assert item["issues"][0]["kind"] == "malformed_filter_config"
    assert item["inputs"][0]["query"]["filterConfig"] == bad


def test_absent_registry_allowed_but_missing_pages_rejected():
    assert blend_inventory({"report": {}, "page": []}, set())["blends"] == []
    with pytest.raises(StudioError):
        blend_inventory({"report": {}}, set())


def test_union_is_a_distinct_node_with_lossless_keys_and_nested_input_paths():
    keys = [{"leftName": "a", "rightName": "b", "future": [1]}]
    config = fixture({"union": {"unionKeyPair": keys, "left": query(),
                                "right": {"join": {"type": 1, "left": query(), "right": query()}}}})
    before = copy.deepcopy(config)
    item = blend_inventory(config, set(), include_definitions=True)["blends"][0]
    assert len(item["inputs"]) == 3 and not item["issues"]
    union, join = item["joins"]
    assert union["node_kind"] == "union" and union["type_code"] == 5
    assert union["union_key_pairs"] == keys and join["node_kind"] == "join"
    assert item["inputs"][2]["path"].endswith("/union/right/join/right/query")
    union["union_key_pairs"][0]["future"].append(2)
    assert config == before


def test_ambiguous_union_and_join_is_not_traversed_as_valid():
    item = blend_inventory(fixture({"query": {}, "union": {}}), set())["blends"][0]
    assert item["issues"][0]["kind"] == "unrecognized_tree_node"
    assert not item["inputs"] and not item["joins"]
