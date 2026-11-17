import asyncio
import copy

import pytest
from test_service import REPORT, Session

from datastudio_mcp.browser import StudioError
from datastudio_mcp.datasources import (
    CalculatedField,
    CalculatedFieldEdit,
    DatasourceParameter,
    Datasources,
    FieldMetadataEdit,
    FieldRename,
    field_reference_inventory,
    parameter_reference_inventory,
)
from datastudio_mcp.service import Studio


class SourceSession(Session):
    def __init__(self, root):
        super().__init__(root)
        del self.page  # Parent's report fixture must not shadow the CDP page method.
        self.draft = {
            "datasourceId": "source",
            "opaque": {"future": 7},
            "blocks": [{"keep": 1}],
            "datasourceBlock": {
                "fields": [
                    {
                        "field": {"ns": "t0", "name": "calc_x"},
                        "outputName": "Old",
                        "textFormula": "1",
                        "createdBy": 2,
                    }
                ]
            },
        }
        self.published = copy.deepcopy(self.draft)
        self.embedded = True
        self.invalid_formula = False
        self.change_on_validation = False
        self.report_components = []
        self.report_reads = 0
        self.change_report_on_read = None

    async def page(self):
        class Page:
            async def evaluate(self, script, args):
                return {"dataRequest": args}

        return Page()

    async def post(self, endpoint, body):
        if endpoint == "getReport":
            self.report_reads += 1
            if self.change_report_on_read == self.report_reads:
                self.report_components.append({"componentId": "concurrent"})
            return {
                "reportConfig": {
                    "shareable": {"id": REPORT},
                    "report": {
                        "componentConfig": copy.deepcopy(self.report_components),
                        "resource": {},
                    },
                    "page": [],
                }
            }
        if endpoint == "validateFormula":
            if self.change_on_validation:
                self.draft["concurrent_edit"] = True
                self.published = copy.deepcopy(self.draft)
            if self.invalid_formula:
                return {"errorMessage": "Invalid expression"}
            return {
                "column": {
                    "field": {"ns": "t0", "name": "validation_only"},
                    "type": 6,
                    "enabled": True,
                    "dataType": 2,
                    "opaque_validation_metadata": {"keep": True},
                }
            }
        if endpoint == "getClaimedDatasources":
            return {
                "claimedDatasources": [
                    {"basicEntityInfo": {"id": "source", "isEmbedded": self.embedded}}
                ]
            }
        if endpoint == "getBlockDatasource":
            return {
                "draft": copy.deepcopy(self.draft),
                "published": copy.deepcopy(self.published),
                "isEditable": True,
                "embeddedInReportId": REPORT,
            }
        assert endpoint == "publishDatasource"
        self.writes += 1
        self.draft = copy.deepcopy(body["draft"])
        self.published = copy.deepcopy(self.draft)
        if self.fail_after_write:
            raise StudioError("TRANSPORT_FAILURE", "Lost response", outcome_unknown=True)
        return {}


async def complete_dependencies(self, datasource_id, overrides=None):
    return {
        "status": "inspected",
        "dependents": {"t0.calc_x": []},
        "diagnostics": [],
    }


def test_rename_publish_and_restore_without_replay(tmp_path):
    async def scenario():
        session = SourceSession(tmp_path)
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource("source")
        changes = [FieldRename(field_name="calc_x", output_name="New")]
        plan = await service.rename("source", changes, before["fingerprint"])
        assert plan["status"] == "planned" and session.writes == 0
        session.fail_after_write = True
        result = await service.rename("source", changes, before["fingerprint"], dry_run=False)
        assert result["status"] == "verified" and session.writes == 1
        assert session.draft["opaque"] == before["draft"]["opaque"]
        assert session.draft["blocks"] == before["draft"]["blocks"]
        assert session.draft["datasourceBlock"]["fields"][0]["textFormula"] == "1"
        session.fail_after_write = False
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified"
        assert session.draft == before["draft"] and session.published == before["published"]

    asyncio.run(scenario())


def test_draft_and_shared_source_guards(tmp_path):
    async def scenario():
        session = SourceSession(tmp_path)
        studio = Studio(session)
        service = Datasources(studio)
        changes = [FieldRename(field_name="calc_x", output_name="New")]
        session.draft["unpublished"] = True
        with pytest.raises(StudioError, match="unrelated"):
            await service.rename("source", changes, "unused", dry_run=False)
        session.published = copy.deepcopy(session.draft)
        session.embedded = False
        with pytest.raises(StudioError, match="allow_shared_source"):
            await service.rename("source", changes, "unused", dry_run=False)
        assert session.writes == 0

    asyncio.run(scenario())


def test_shared_source_requires_opt_in_and_records_external_impact(tmp_path):
    async def scenario():
        session = SourceSession(tmp_path)
        session.embedded = False
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource("source")
        changes = [FieldRename(field_name="calc_x", output_name="Shared")]
        plan = await service.rename(
            "source",
            changes,
            before["fingerprint"],
            allow_shared_source=True,
        )
        assert plan["source_mode"] == "shared"
        assert "all consumers" in plan["impact_scope"] and session.writes == 0
        result = await service.rename(
            "source",
            changes,
            before["fingerprint"],
            dry_run=False,
            allow_shared_source=True,
        )
        assert result["status"] == "verified" and result["source_mode"] == "shared"
        record = studio.operation(result["operation_id"])
        assert record["source_mode"] == "shared"
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified"
        assert session.draft == before["draft"] and session.published == before["published"]

    asyncio.run(scenario())


def test_shared_field_delete_requires_separate_external_consumer_opt_in(tmp_path):
    async def scenario():
        session = SourceSession(tmp_path)
        session.embedded = False
        studio = Studio(session)
        before = await studio.datasource("source")
        with pytest.raises(StudioError) as error:
            await Datasources(studio).delete_calculated_fields(
                "source",
                ["calc_x"],
                before["fingerprint"],
                allow_shared_source=True,
            )
        assert error.value.code == "SHARED_CONSUMERS_UNSCANNED"
        assert session.writes == 0

    asyncio.run(scenario())


def test_calculated_create_preview_publish_restore(tmp_path):
    async def scenario():
        session = SourceSession(tmp_path)
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource("source")
        fields = [CalculatedField(output_name="Total", expression="SUM(t0.calc_x)", aggregation=7)]
        preview = await service.create_calculated_fields("source", fields, before["fingerprint"])
        assert preview["status"] == "planned" and session.writes == 0
        assert session.draft == before["draft"]
        session.fail_after_write = True
        result = await service.create_calculated_fields(
            "source", fields, before["fingerprint"], dry_run=False
        )
        assert result["status"] == "verified" and session.writes == 1
        actual = session.draft["datasourceBlock"]["fields"]
        assert actual[:-1] == before["draft"]["datasourceBlock"]["fields"]
        assert actual[-1] == result["created_fields"][0]
        assert actual[-1]["field"]["name"].startswith("calc_")
        assert actual[-1]["textFormula"] == "SUM(t0.calc_x)"
        assert actual[-1]["defaultAggregationType"] == 7
        assert actual[-1]["opaque_validation_metadata"] == {"keep": True}
        assert session.draft["blocks"] == before["draft"]["blocks"]
        assert session.draft["opaque"] == before["draft"]["opaque"]
        session.fail_after_write = False
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified" and session.writes == 2
        assert session.draft == before["draft"] and session.published == before["published"]

    asyncio.run(scenario())


def test_calculated_edit_preserves_identity_and_opaque_metadata(tmp_path):
    async def scenario():
        session = SourceSession(tmp_path)
        original_field = session.draft["datasourceBlock"]["fields"][0]
        original_field.update(
            {
                "opaque": {"keep": 1},
                "description": "Keep me",
                "outputSemanticInfo": {"obsolete": True},
                "semanticConfig": {"obsolete": True},
            }
        )
        session.published = copy.deepcopy(session.draft)
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource("source")
        fields = [
            CalculatedFieldEdit(
                field_name="calc_x",
                output_name="Changed",
                expression="SUM(t0._revenue_)",
                aggregation=7,
            )
        ]
        plan = await service.edit_calculated_fields("source", fields, before["fingerprint"])
        assert plan["ids_are_provisional"] is False and session.writes == 0
        result = await service.edit_calculated_fields(
            "source", fields, before["fingerprint"], dry_run=False
        )
        assert result["status"] == "verified" and session.writes == 1
        actual = session.draft["datasourceBlock"]["fields"]
        assert len(actual) == 1 and actual[0]["field"] == original_field["field"]
        assert actual[0]["textFormula"] == "SUM(t0._revenue_)"
        assert actual[0]["description"] == "Keep me" and actual[0]["opaque"] == {"keep": 1}
        assert "outputSemanticInfo" not in actual[0]
        assert "semanticConfig" not in actual[0]
        assert actual[0]["type"] == 6 and actual[0]["defaultAggregationType"] == 7
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified"
        assert session.draft == before["draft"] and session.published == before["published"]
        fields[0].expression = "SUM(t0.calc_x)"
        with pytest.raises(StudioError) as error:
            await service.edit_calculated_fields("source", fields, before["fingerprint"])
        assert error.value.code == "FORMULA_CYCLE"
        assert session.writes == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["invalid_formula", "change_on_validation", "aggregation"])
def test_calculated_create_rejects_unsafe_publication(tmp_path, failure):
    async def scenario():
        session = SourceSession(tmp_path)
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource("source")
        fields = [
            CalculatedField(
                output_name="Total",
                expression="SUM(t0.calc_x)",
                aggregation=6 if failure == "aggregation" else 7,
            )
        ]
        if failure == "invalid_formula":
            session.invalid_formula = True
            result = await service.create_calculated_fields(
                "source", fields, before["fingerprint"], dry_run=False
            )
            assert result["status"] == "validation_failed"
        else:
            session.change_on_validation = failure == "change_on_validation"
            with pytest.raises(StudioError) as error:
                await service.create_calculated_fields(
                    "source", fields, before["fingerprint"], dry_run=False
                )
            assert error.value.code == (
                "VERSION_CONFLICT" if failure == "change_on_validation" else "INVALID_AGGREGATION"
            )
        assert session.writes == 0

    asyncio.run(scenario())


def test_calculated_delete_preview_publish_and_restore(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            "datastudio_mcp.datasources.Formulas.dependencies", complete_dependencies
        )
        session = SourceSession(tmp_path)
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource("source")
        preview = await service.delete_calculated_fields(
            "source", ["calc_x"], before["fingerprint"]
        )
        assert preview["status"] == "planned" and session.writes == 0
        assert session.draft == before["draft"]
        result = await service.delete_calculated_fields(
            "source", ["calc_x"], before["fingerprint"], dry_run=False
        )
        assert result["status"] == "verified" and session.writes == 1
        assert session.draft["datasourceBlock"]["fields"] == []
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified" and session.writes == 2
        assert session.draft == before["draft"] and session.published == before["published"]

    asyncio.run(scenario())


@pytest.mark.parametrize("blocker", ["formula", "report"])
def test_calculated_delete_blocks_consumers(tmp_path, monkeypatch, blocker):
    async def dependencies(self, datasource_id, overrides=None):
        return {
            "status": "inspected",
            "dependents": {"t0.calc_x": ["t0.calc_consumer"] if blocker == "formula" else []},
            "diagnostics": [],
        }

    async def scenario():
        monkeypatch.setattr("datastudio_mcp.datasources.Formulas.dependencies", dependencies)
        session = SourceSession(tmp_path)
        if blocker == "report":
            session.report_components = [
                {
                    "componentId": "chart",
                    "datasourceId": "source",
                    "conceptDefs": [
                        {
                            "queryTimeTransformation": {
                                "dataTransformation": {"sourceFieldName": "calc_x"}
                            }
                        }
                    ],
                }
            ]
        studio = Studio(session)
        before = await studio.datasource("source")
        result = await Datasources(studio).delete_calculated_fields(
            "source", ["calc_x"], before["fingerprint"], dry_run=False
        )
        assert result["status"] == "blocked"
        assert result["reason"] == "FIELD_IN_USE" and result["writes_dispatched"] == 0
        assert session.writes == 0 and session.draft == before["draft"]

    asyncio.run(scenario())


def test_calculated_delete_guards_report_consumer_race(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            "datastudio_mcp.datasources.Formulas.dependencies", complete_dependencies
        )
        session = SourceSession(tmp_path)
        session.change_report_on_read = 2
        studio = Studio(session)
        before = await studio.datasource("source")
        with pytest.raises(StudioError) as error:
            await Datasources(studio).delete_calculated_fields(
                "source", ["calc_x"], before["fingerprint"], dry_run=False
            )
        assert error.value.code == "VERSION_CONFLICT" and session.writes == 0

    asyncio.run(scenario())


def test_field_reference_inventory_scopes_and_blocks_ownerless_matches():
    config = {
        "report": {
            "componentConfig": [
                {"datasourceId": "source", "formula": "SUM(t0.calc_x)"},
                {"datasourceId": "other", "sourceFieldName": "calc_x"},
            ],
            "resource": {"opaque": {"strValue": "calc_x"}},
        },
        "page": [],
    }
    result = field_reference_inventory(config, "source", {"calc_x"})
    assert [item["classification"] for item in result["references"]["calc_x"]] == [
        "scoped",
        "unscoped",
    ]
    assert result["other_source_matches"]["calc_x"][0]["classification"] == "other_source"


def test_field_metadata_edit_physical_field_preserves_opaque_state(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            "datastudio_mcp.datasources.Formulas.dependencies", complete_dependencies
        )
        session = SourceSession(tmp_path)
        physical = {
            "field": {"ns": "t0", "name": "_revenue_"},
            "immutableVersion": {"ns": "t0", "name": "revenue"},
            "outputName": "Revenue",
            "description": "Old",
            "dataType": 2,
            "outputSemanticInfo": {
                "semanticConfig": [{"semanticType": 33}],
                "opaqueSemantic": {"keep": True},
            },
            "defaultAggregationType": 6,
            "enabled": True,
            "opaque": {"keep": 1},
        }
        session.draft["datasourceBlock"]["fields"].append(physical)
        session.published = copy.deepcopy(session.draft)
        studio = Studio(session)
        before = await studio.datasource("source")
        changes = [
            FieldMetadataEdit(
                field_name="_revenue_",
                output_name="Revenue changed",
                description=None,
                data_type=3,
                semantic_config=[{"semanticType": 33, "opaqueOption": True}],
                default_aggregation=4,
                enabled=False,
            )
        ]
        preview = await Datasources(studio).edit_field_metadata(
            "source", changes, before["fingerprint"]
        )
        assert preview["status"] == "planned" and session.writes == 0
        result = await Datasources(studio).edit_field_metadata(
            "source", changes, before["fingerprint"], dry_run=False
        )
        assert result["status"] == "verified" and session.writes == 1
        actual = session.draft["datasourceBlock"]["fields"][1]
        assert actual["field"] == physical["field"]
        assert actual["immutableVersion"] == physical["immutableVersion"]
        assert actual["opaque"] == {"keep": 1} and "description" not in actual
        assert actual["outputName"] == "Revenue changed"
        assert actual["dataType"] == 3
        assert actual["outputSemanticInfo"] == {
            "semanticConfig": [{"semanticType": 33, "opaqueOption": True}],
            "opaqueSemantic": {"keep": True},
        }
        assert actual["defaultAggregationType"] == 4 and actual["enabled"] is False
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified"
        assert session.draft == before["draft"] and session.published == before["published"]

    asyncio.run(scenario())


def test_field_metadata_semantic_change_blocks_used_field_without_opt_in(
    tmp_path, monkeypatch
):
    async def dependencies(self, datasource_id, overrides=None):
        return {
            "status": "inspected",
            "dependents": {"t0.calc_x": ["t0.calc_consumer"]},
            "diagnostics": [],
        }

    async def scenario():
        monkeypatch.setattr("datastudio_mcp.datasources.Formulas.dependencies", dependencies)
        session = SourceSession(tmp_path)
        session.report_components = [
            {"datasourceId": "source", "sourceFieldName": "calc_x"}
        ]
        studio = Studio(session)
        before = await studio.datasource("source")
        result = await Datasources(studio).edit_field_metadata(
            "source",
            [FieldMetadataEdit(field_name="calc_x", default_aggregation=6)],
            before["fingerprint"],
            dry_run=False,
        )
        assert result["status"] == "blocked"
        assert result["reason"] == "FIELD_IN_USE_APPROVAL_REQUIRED"
        assert result["formula_dependents"] == {"calc_x": ["t0.calc_consumer"]}
        assert session.writes == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"data_type": 3},
        {"semantic_config": [{"semanticType": 33}]},
        {"output_name": None},
    ],
)
def test_field_metadata_requires_coherent_explicit_changes(kwargs):
    with pytest.raises(ValueError):
        FieldMetadataEdit(field_name="calc_x", **kwargs)


def parameter(key="country_param"):
    return DatasourceParameter(
        key=key,
        definition={
            "parametersModifiedByRoles": [
                {
                    "key": 2,
                    "value": {
                        "name": key,
                        "dataType": 1,
                        "options": [{"label": "Ukraine", "stringValue": "UA"}],
                        "defaultValue": [{"label": "Ukraine", "stringValue": "UA"}],
                        "lock": 2,
                        "validation": {},
                    },
                }
            ],
            "parameterDisplayConfiguration": {
                "name": key,
                "displayName": "Country",
                "displayType": 2,
                "opaque": {"keep": True},
            },
        },
    )


def test_parameter_create_replace_list_delete_and_restore(tmp_path):
    async def scenario():
        session = SourceSession(tmp_path)
        studio = Studio(session)
        service = Datasources(studio)
        before = await studio.datasource("source")
        preview = await service.upsert_parameters(
            "source", [parameter()], before["fingerprint"]
        )
        assert preview["status"] == "planned" and session.writes == 0
        created = await service.upsert_parameters(
            "source", [parameter()], before["fingerprint"], dry_run=False
        )
        assert created["status"] == "verified"
        listed = await service.parameters("source")
        assert listed["parameters"][0]["value"] == parameter().definition
        restored_create = await studio.restore_operation(created["operation_id"], dry_run=False)
        assert restored_create["status"] == "verified" and session.draft == before["draft"]

        session.draft.setdefault("resource", {}).setdefault("parameterConfiguration", {})[
            "parameters"
        ] = [{"key": "country_param", "value": parameter().definition}]
        session.published = copy.deepcopy(session.draft)
        with_parameter = await studio.datasource("source")
        deleted = await service.delete_parameters(
            "source", ["country_param"], with_parameter["fingerprint"], dry_run=False
        )
        assert deleted["status"] == "verified" and service._parameter_entries(session.draft) == []
        restored_delete = await studio.restore_operation(deleted["operation_id"], dry_run=False)
        assert restored_delete["status"] == "verified"
        assert service._parameter_entries(session.draft)[0]["value"] == parameter().definition

    asyncio.run(scenario())


def test_parameter_delete_and_replace_block_references(tmp_path):
    async def scenario():
        session = SourceSession(tmp_path)
        entry = {"key": "country_param", "value": parameter().definition}
        session.draft["resource"] = {
            "parameterConfiguration": {"parameters": [copy.deepcopy(entry)]}
        }
        session.draft["datasourceBlock"]["fields"][0]["textFormula"] = "\ue000country_param"
        session.published = copy.deepcopy(session.draft)
        studio = Studio(session)
        before = await studio.datasource("source")
        deleted = await Datasources(studio).delete_parameters(
            "source", ["country_param"], before["fingerprint"], dry_run=False
        )
        assert deleted["status"] == "blocked" and deleted["reason"] == "PARAMETER_IN_USE"
        changed = parameter()
        changed.definition["parameterDisplayConfiguration"]["displayName"] = "Changed"
        replaced = await Datasources(studio).upsert_parameters(
            "source", [changed], before["fingerprint"], dry_run=False
        )
        assert replaced["status"] == "blocked"
        assert replaced["reason"] == "PARAMETER_IN_USE_APPROVAL_REQUIRED"
        assert session.writes == 0

    asyncio.run(scenario())


def test_parameter_reference_inventory_detects_sentinel_and_report_key():
    result = parameter_reference_inventory(
        {"report": {"parameterKey": "country_param"}, "page": []},
        {"datasourceBlock": {"fields": [{"textFormula": "\ue000country_param"}]}},
        {"country_param"},
    )
    assert [item["scope"] for item in result["country_param"]] == ["field", "report"]


def test_proposed_cycle_follows_calculated_references():
    from datastudio_mcp.datasources import proposed_cycle

    fields = [
        {"field": {"name": "_a_"}, "textFormula": "t0.a"},
        {"field": {"name": "calc_kind"}, "textFormula": 'CASE WHEN t0._a_ = "x" THEN "y" END'},
        {"field": {"name": "calc_rate"}, "textFormula": "SUM(t0._a_)"},
    ]
    proposed_cycle(fields, {"calc_rate": "SUM(IF(t0.calc_kind IS NULL, 0, 1))"})
    with pytest.raises(StudioError) as error:
        proposed_cycle(fields, {"calc_rate": "SUM(IF(t0.calc_kind IS NULL, 0, 1))",
                                "calc_kind": "IF(t0.calc_rate > 0, 'a', 'b')"})
    assert error.value.code == "FORMULA_CYCLE"
