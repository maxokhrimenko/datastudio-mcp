import pytest
from pydantic import ValidationError

from datastudio_mcp.bigquery import BigQuerySource, usages


@pytest.mark.parametrize("values", [
    {"project_id": "p"},
    {"project_id": "p", "dataset_id": "d"},
    {"project_id": "p", "dataset_id": "d", "table_id": "t", "sql": "SELECT 1"},
    {"project_id": "p", "dataset_id": "d;drop", "table_id": "t"},
    {"project_id": "p", "sql": "SELECT 1", "billing_project_id": "bad id"},
])
def test_source_needs_exactly_a_table_or_a_query_with_safe_identifiers(values):
    with pytest.raises(ValidationError):
        BigQuerySource(**values)


def test_table_connector_config():
    block = BigQuerySource(project_id="p", dataset_id="d", table_id="t").connector_config("Proj")
    config = block["connectorConfig"]["bigQueryConnectorConfig"]
    assert block["alias"] == "t0"
    assert config["sqlType"] == 2 and config["datasetId"] == "d" and config["tableId"] == "t"
    assert config["meta"]["projectName"] == "Proj"
    assert "sql" not in block["connectorConfig"] and "billingProjectId" not in config


def test_query_connector_config_with_legacy_sql_and_billing_project():
    source = BigQuerySource(project_id="p", sql="SELECT 1", legacy_sql=True, billing_project_id="b")
    connector = source.connector_config()["connectorConfig"]
    config = connector["bigQueryConnectorConfig"]
    assert connector["sql"] == "SELECT 1" and connector["useDatetimeType"] is False
    assert config["sqlType"] == 1 and config["billingProjectId"] == "b"
    assert "tableId" not in config
    assert source.default_name() == "BigQuery custom query"


def test_usages_lists_components_pages_and_resources_but_not_other_sources():
    config = {
        "report": {
            "componentConfig": [{"componentId": "r1", "datasourceId": "ds"}],
            "resource": {"filter": {"entry": [{"datasourceId": "ds"}]},
                         "parameterResource": {"x": "other"}},
        },
        "page": [{"pageId": "p1", "page": {"componentConfig": [
            {"componentId": "a", "datasourceId": "other",
             "componentConfig": [{"componentId": "child", "datasourceId": "ds"}]},
            {"componentId": "b", "propertyConfig": {"componentProperty": {"dataset": ["ds"]}}},
        ]}}],
    }
    assert sorted(usages(config, "ds")) == [
        "p1/b", "p1/child", "report/r1", "resource:filter"]
    assert usages(config, "unused") == []
