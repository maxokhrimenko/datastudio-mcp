import asyncio
import copy
from types import SimpleNamespace

import pytest

from datastudio_mcp import formulas
from datastudio_mcp.bigquery import BigQuerySource
from datastudio_mcp.browser import (
    Browser,
    BrowserConfig,
    Config,
    StudioError,
    parse_datasource_reference,
)
from datastudio_mcp.datasources import Datasources, FieldMetadataEdit, canonical_account
from datastudio_mcp.service import Studio
from datastudio_mcp.workspace import Workspace

SOURCE = "5e5e5e5e-6f6f-4a7a-8b8b-9c9c9c9c9c9c"
REPORT = "0f0f0f0f-1a1a-4b2b-8c3c-4d4d4d4d4d4d"
VIEWER = "datastudio-viewer@example-analytics.iam.gserviceaccount.com"


def physical(column, data_type="STRING", output=None):
    return {
        "field": {"ns": "t0", "name": f"_{column}_"},
        "outputName": output or column,
        "textFormula": f"t0.{column}",
        "immutableVersion": {"ns": "t0", "name": column},
        "underlyingConnectorDataType": data_type,
        "createdBy": 1,
    }


def connector(column, data_type="STRING"):
    return {
        "field": {"ns": "t0", "name": column},
        "mutableVersion": {"ns": "t0", "name": f"_{column}_"},
        "underlyingConnectorDataType": data_type,
    }


class StandaloneSession:
    """A datasource-scope session: no report, the account's datasources only."""

    def __init__(self, root):
        self.config = Config(None, root, root / "journal")
        self.draft = {
            "datasourceId": SOURCE,
            "blocks": [
                {
                    "connectorBlockConfig": {
                        "connectorConfig": {
                            "bigQueryConnectorConfig": {"projectId": "example-analytics"}
                        }
                    },
                    "fields": [connector("kept"), connector("gone")],
                }
            ],
            "datasourceBlock": {
                "fields": [
                    physical("kept", output="Kept label"),
                    physical("gone"),
                    {"field": {"ns": "t0", "name": "calc_x"}, "textFormula": "1", "createdBy": 2},
                ]
            },
        }
        self.published = copy.deepcopy(self.draft)
        self.credential = {"type": 1, "account": "Jane Doe"}
        self.columns_now = [
            connector("kept", "INTEGER"),
            {**physical("kept", "INTEGER")},
            connector("new"),
            physical("new"),
        ]
        self.posts = []

    async def page(self):
        class Page:
            async def evaluate(self, script, args):
                return {"dataRequest": args}

        return Page()

    async def columns(self, datasource_id, block):
        assert datasource_id == SOURCE and "connectorConfig" in block
        return copy.deepcopy(self.columns_now)

    async def post(self, endpoint, body):
        self.posts.append((endpoint, copy.deepcopy(body)))
        if endpoint == "getShareableList":
            return {"shareableConfig": [{"id": SOURCE, "name": "v2_audience_reach"}]}
        if endpoint == "getBlockDatasource":
            return {
                "draft": copy.deepcopy(self.draft),
                "published": copy.deepcopy(self.published),
                "isEditable": True,
                "credentialOwnerType": self.credential["type"],
                "storedCredentialOwnerDisplayName": self.credential["account"],
            }
        if endpoint == "publishDatasource":
            self.draft = copy.deepcopy(body["draft"])
            self.published = copy.deepcopy(self.draft)
            return {}
        if endpoint == "updateDataAccessControl":
            self.credential = {"type": 2, "account": body["serviceAccountEmail"]}
            return {"version": "1"}
        raise AssertionError(f"report endpoint {endpoint} called without a report")


def test_reads_any_datasource_without_a_report(tmp_path):
    async def run():
        session = StandaloneSession(tmp_path)
        studio = Studio(session)
        source = await studio.datasource(SOURCE)
        assert source["source"] == {"id": SOURCE, "isEmbedded": False}
        assert source["credentials"] == {"type": "owner", "account": "Jane Doe"}
        assert [s["name"] for s in await studio.sources()] == ["v2_audience_reach"]
        assert await studio.consumers() is None
        with pytest.raises(StudioError, match="needs a report"):
            await studio.report()
        assert [endpoint for endpoint, _ in session.posts] == [
            "getBlockDatasource",
            "getShareableList",
        ]

    asyncio.run(run())


def no_dependents(monkeypatch, dependents=None):
    async def graph(self, datasource_id, overrides=None):
        return {"status": "inspected", "dependents": dependents or {}, "diagnostics": []}

    monkeypatch.setattr(formulas.Formulas, "dependencies", graph)


def test_metadata_edit_publishes_without_a_report_and_says_nothing_was_scanned(
    tmp_path, monkeypatch
):
    no_dependents(monkeypatch)

    async def run():
        session = StandaloneSession(tmp_path)
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource(SOURCE)
        edit = [FieldMetadataEdit(field_name="_kept_", output_name="Kept", default_aggregation=10)]
        with pytest.raises(StudioError, match="allow_shared_source"):
            await service.edit_field_metadata(SOURCE, edit, before["fingerprint"])
        result = await service.edit_field_metadata(
            SOURCE, edit, before["fingerprint"], dry_run=False, allow_shared_source=True
        )
        assert result["status"] == "verified"
        assert "No report in scope" in result["saved_reference_check"]["reference_scope"]
        publish = next(body for endpoint, body in session.posts if endpoint == "publishDatasource")
        assert "reportId" not in publish
        field = session.draft["datasourceBlock"]["fields"][0]
        assert (field["outputName"], field["defaultAggregationType"]) == ("Kept", 10)

    asyncio.run(run())


def test_refresh_adds_new_columns_drops_missing_and_keeps_edits(tmp_path, monkeypatch):
    no_dependents(monkeypatch)

    async def run():
        session = StandaloneSession(tmp_path)
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource(SOURCE)
        plan = await service.refresh_fields(SOURCE, before["fingerprint"], allow_shared_source=True)
        assert plan["status"] == "planned" and session.posts[-1][0] != "publishDatasource"
        assert (plan["added_columns"], plan["missing_columns"]) == (["new"], ["gone"])
        assert plan["retyped_columns"] == ["kept"]
        result = await service.refresh_fields(
            SOURCE, before["fingerprint"], dry_run=False, allow_shared_source=True
        )
        assert result["status"] == "verified"
        fields = session.draft["datasourceBlock"]["fields"]
        assert [f["field"]["name"] for f in fields] == ["_kept_", "calc_x", "_new_"]
        assert fields[0]["outputName"] == "Kept label"  # an edited field keeps its edit
        assert fields[0]["underlyingConnectorDataType"] == "STRING"  # retyped is not retyped
        assert [f["field"]["name"] for f in session.draft["blocks"][0]["fields"]] == [
            "kept",
            "new",
        ]

    asyncio.run(run())


def test_refresh_blocks_removing_a_column_a_formula_uses(tmp_path, monkeypatch):
    no_dependents(monkeypatch, {"t0._gone_": ["t0.calc_x"]})

    async def run():
        session = StandaloneSession(tmp_path)
        studio = Studio(session)
        before = await studio.datasource(SOURCE)
        result = await Datasources(studio).refresh_fields(
            SOURCE, before["fingerprint"], dry_run=False, allow_shared_source=True
        )
        assert result["status"] == "blocked"
        assert result["formula_dependents"] == {"_gone_": ["t0.calc_x"]}
        assert not any(endpoint == "publishDatasource" for endpoint, _ in session.posts)

    asyncio.run(run())


def test_reconnect_points_the_connector_at_a_custom_query_and_keeps_field_edits(
    tmp_path, monkeypatch
):
    no_dependents(monkeypatch)

    class FillingSession(StandaloneSession):
        async def columns(self, datasource_id, block):
            # The new columns are read through the new connection, not the saved one.
            assert block["connectorConfig"]["sql"] == "SELECT kept, new FROM t"
            return await super().columns(datasource_id, block)

        async def post(self, endpoint, body):
            result = await super().post(endpoint, body)
            if endpoint == "publishDatasource":  # The server derives the location.
                for state in (self.draft, self.published):
                    config = state["blocks"][0]["connectorBlockConfig"]["connectorConfig"]
                    config["bigQueryConnectorConfig"]["processingLocation"] = 301
            return result

    async def run():
        session = FillingSession(tmp_path)
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource(SOURCE)
        query = BigQuerySource(project_id="example-analytics", sql="SELECT kept, new FROM t")
        config = query.connector_config()["connectorConfig"]
        plan = await service.refresh_fields(
            SOURCE, before["fingerprint"], allow_shared_source=True, connector_config=config)
        assert plan["connection"] == {
            "from": {"table": "example-analytics.None.None"},
            "to": {"sql": "SELECT kept, new FROM t", "billing_project_id": "example-analytics",
                   "legacy_sql": False, "parameters": []}}
        assert (plan["added_columns"], plan["missing_columns"]) == (["new"], ["gone"])
        result = await service.refresh_fields(
            SOURCE, before["fingerprint"], dry_run=False, allow_shared_source=True,
            connector_config=config)
        assert result["status"] == "verified"  # server-filled keys are not a mismatch
        saved = session.published["blocks"][0]["connectorBlockConfig"]["connectorConfig"]
        assert saved["sql"] == "SELECT kept, new FROM t"
        assert session.published["datasourceBlock"]["fields"][0]["outputName"] == "Kept label"

    asyncio.run(run())


def test_only_a_bigquery_source_is_reconnected(tmp_path, monkeypatch):
    no_dependents(monkeypatch)

    async def run():
        session = StandaloneSession(tmp_path)
        block = session.draft["blocks"][0]["connectorBlockConfig"]
        block["connectorConfig"] = {"sheetsConnectorConfig": {}}
        session.published = copy.deepcopy(session.draft)
        studio = Studio(session)
        before = await studio.datasource(SOURCE)
        config = BigQuerySource(project_id="p", sql="SELECT 1").connector_config()
        with pytest.raises(StudioError) as error:
            await Datasources(studio).refresh_fields(
                SOURCE, before["fingerprint"], allow_shared_source=True,
                connector_config=config["connectorConfig"])
        assert error.value.code == "UNSUPPORTED_SOURCE_SHAPE"

    asyncio.run(run())


def test_credentials_default_to_the_projects_canonical_reader(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE", "datastudio-viewer@{project}.iam.gserviceaccount.com")

    async def run():
        session = StandaloneSession(tmp_path)
        service = Datasources(Studio(session))
        plan = await service.set_credentials(SOURCE, allow_shared_source=True)
        assert plan["status"] == "planned" and plan["after"]["account"] == VIEWER
        assert plan["account_source"] == "template"
        result = await service.set_credentials(SOURCE, dry_run=False, allow_shared_source=True)
        assert result["status"] == "verified"
        body = next(b for endpoint, b in session.posts if endpoint == "updateDataAccessControl")
        assert body == {
            "datasourceId": SOURCE,
            "reportId": "",
            "delegatedAccessEnabled": True,
            "serviceAccountEmail": VIEWER,
            "oauthType": 1,
        }
        again = await service.set_credentials(SOURCE, dry_run=False, allow_shared_source=True)
        assert again["status"] == "unchanged"
        with pytest.raises(StudioError, match="INVALID_ACCOUNT|service account email"):
            await service.set_credentials(SOURCE, "someone@gmail.com", allow_shared_source=True)

    asyncio.run(run())


def test_canonical_account_needs_a_template_and_one_bigquery_project(monkeypatch):
    block = {"connectorBlockConfig": {"connectorConfig": {"bigQueryConnectorConfig": {}}}}
    monkeypatch.delenv("DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE", raising=False)
    with pytest.raises(StudioError, match="DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE"):
        canonical_account({"blocks": [block]})
    monkeypatch.setenv(
        "DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE", "datastudio-viewer@{project}.iam.gserviceaccount.com")
    with pytest.raises(StudioError, match="pass it"):
        canonical_account({"blocks": [block]})
    billed = {"projectId": "data-project", "billingProjectId": "example-billing"}
    block["connectorBlockConfig"]["connectorConfig"]["bigQueryConnectorConfig"] = billed
    assert canonical_account({"blocks": [block]}).startswith(
        "datastudio-viewer@example-billing"
    )


def test_datasource_references_and_scope_bundle(tmp_path):
    url = f"https://datastudio.google.com/u/0/datasources/{SOURCE}"
    assert parse_datasource_reference(url) == SOURCE
    assert parse_datasource_reference(SOURCE) == SOURCE
    with pytest.raises(StudioError, match="datasource UUID"):
        parse_datasource_reference(f"https://datastudio.google.com/reporting/{REPORT}")

    browser = Browser(BrowserConfig(tmp_path, tmp_path / "journal"))
    ws = Workspace(browser, factory=lambda session: SimpleNamespace(session=session))
    scope = ws.datasources()
    assert scope.session.config.report_id is None and ws.datasources() is scope
    with pytest.raises(StudioError, match="NO_ACTIVE_REPORT|report_open"):
        ws.resolve(None)
