import asyncio
import copy

import pytest
from pydantic import ValidationError

from test_service import Session
from datastudio_mcp.browser import StudioError
from datastudio_mcp.component_filters import ComponentFilterChange, ComponentFilters
from datastudio_mcp.document import fingerprint
from datastudio_mcp.service import Studio


class FilterSession(Session):
    def __init__(self, root, mode="valid"):
        super().__init__(root)
        self.mode, self.report_reads = mode, 0
        self.report_content = {
            "resource": {
                "filter": {
                    "entry": [
                        {
                            "key": "f1",
                            "value": {"datasourceId": "ds", "filterDefinition": {"keep": 1}},
                        },
                        {
                            "key": "f2",
                            "value": {"datasourceId": "ds", "filterDefinition": {"keep": 2}},
                        },
                    ]
                }
            }
        }
        props = {
            "dataset": {"datasetId": "ds", "datasetType": 1},
            "filters": ["f1"],
            "inheritFilters": True,
            "opaque": {"keep": 3},
        }
        self.page["componentConfig"][0]["propertyConfig"] = {"componentProperty": props}
        self.page["componentConfig"].append(
            {
                "componentId": "cd-nested",
                "componentConfig": [
                    {
                        "componentId": "child",
                        "propertyConfig": {"componentProperty": copy.deepcopy(props)},
                    }
                ],
            }
        )
        self.page["groupConfig"] = [{"groupId": "g", "componentId": ["cd-test"], "opaque": 4}]
        self.source = {"datasourceId": "ds", "revision": 1}

    async def evaluate(self, script, args):
        if self.mode == "source-change":
            self.source["revision"] += 1
        if self.mode == "invalid":
            return {"error": "FILTER_INCOMPATIBLE"}
        return {"components": [{"component_id": c["componentId"]} for c in args["changes"]]}

    async def post(self, endpoint, body):
        if endpoint == "getClaimedDatasources":
            return {"claimedDatasources": [{"basicEntityInfo": {"id": "ds"}}]}
        if endpoint == "getBlockDatasource":
            source = copy.deepcopy(self.source)
            return {
                "draft": source,
                "published": {} if self.mode == "draft" else copy.deepcopy(source),
            }
        result = await super().post(endpoint, body)
        if endpoint == "getReport":
            self.report_reads += 1
            if self.mode == "resource-race" and self.report_reads == 3:
                self.report_content["other"] = True
            result["reportConfig"]["report"] = copy.deepcopy(self.report_content)
        if endpoint == "updatePage" and self.mode == "resource-side-effect":
            self.report_content["other"] = True
        return result


# Session.page is saved content; the adapter separates it from browser page().
class BrowserAdapter:
    def __init__(self, session):
        self.session, self.config = session, session.config

    async def page(self):
        return self.session

    async def post(self, endpoint, body):
        return await self.session.post(endpoint, body)


def test_component_batch_preserves_group_and_nested_payload_and_undo(tmp_path):
    async def scenario():
        session = FilterSession(tmp_path)
        studio = Studio(BrowserAdapter(session))
        before = copy.deepcopy(session.page)
        expected = fingerprint(session.report_content)
        changes = [
            ComponentFilterChange(component_id="cd-test", filter_ids=[]),
            ComponentFilterChange(component_id="child", inherit_filters=False),
        ]
        preview = await ComponentFilters(studio).patch(
            "p_test", changes, fingerprint(before), expected
        )
        assert preview["status"] == "planned" and session.writes == 0
        assert session.page == before and not session.config.state_dir.exists()
        result = await ComponentFilters(studio).patch(
            "p_test", changes, fingerprint(before), expected, dry_run=False
        )
        assert result["status"] == "verified" and session.writes == 1
        intended = copy.deepcopy(before)
        intended["componentConfig"][0]["propertyConfig"]["componentProperty"]["filters"] = []
        intended["componentConfig"][1]["componentConfig"][0]["propertyConfig"]["componentProperty"][
            "inheritFilters"
        ] = False
        assert session.page == intended
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))[
            "status"
        ] == "verified"
        assert session.page == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,code",
    [
        ("invalid", "FILTER_INCOMPATIBLE"),
        ("source-change", "VERSION_CONFLICT"),
        ("draft", "UNPUBLISHED_DATASOURCE"),
        ("resource-race", "VERSION_CONFLICT"),
    ],
)
def test_invalid_or_concurrent_dependency_prevents_write(tmp_path, mode, code):
    async def scenario():
        session = FilterSession(tmp_path, mode)
        before = copy.deepcopy(session.page)
        with pytest.raises(StudioError) as caught:
            await ComponentFilters(Studio(BrowserAdapter(session))).patch(
                "p_test",
                [ComponentFilterChange(component_id="cd-test", filter_ids=["f2"])],
                fingerprint(before),
                fingerprint(session.report_content),
                dry_run=False,
            )
        assert caught.value.code == code and session.writes == 0 and session.page == before

    asyncio.run(scenario())


def test_resource_change_after_dispatch_fails_readback_and_blocks_undo(tmp_path):
    async def scenario():
        session = FilterSession(tmp_path, "resource-side-effect")
        studio = Studio(BrowserAdapter(session))
        result = await ComponentFilters(studio).patch(
            "p_test",
            [ComponentFilterChange(component_id="cd-test", filter_ids=["f2"])],
            fingerprint(session.page),
            fingerprint(session.report_content),
            dry_run=False,
        )
        assert result["status"] == "verification_failed" and result["mismatches"] == ["/report"]
        with pytest.raises(StudioError, match="Report/filter resources changed"):
            await studio.restore_operation(result["operation_id"], dry_run=False)
        assert session.writes == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    ["empty", "duplicate", "unknown-filter", "unknown-component", "stale-page", "stale-report"],
)
def test_invalid_batch_does_not_journal(tmp_path, case):
    async def scenario():
        session = FilterSession(tmp_path)
        changes = [ComponentFilterChange(component_id="cd-test", filter_ids=["f2"])]
        if case == "empty":
            changes = []
        if case == "duplicate":
            changes *= 2
        if case == "unknown-filter":
            changes[0].filter_ids = ["missing"]
        if case == "unknown-component":
            changes[0].component_id = "missing"
        with pytest.raises(StudioError):
            await ComponentFilters(Studio(BrowserAdapter(session))).patch(
                "p_test",
                changes,
                "stale" if case == "stale-page" else fingerprint(session.page),
                "stale" if case == "stale-report" else fingerprint(session.report_content),
                dry_run=False,
            )
        assert session.writes == 0 and not session.config.state_dir.exists()

    asyncio.run(scenario())


def test_unchanged_bindings_do_not_create_an_operation(tmp_path):
    async def scenario():
        session = FilterSession(tmp_path)
        result = await ComponentFilters(Studio(BrowserAdapter(session))).patch(
            "p_test",
            [ComponentFilterChange(component_id="cd-test", inherit_filters=True)],
            fingerprint(session.page),
            fingerprint(session.report_content),
            dry_run=False,
        )
        assert result["status"] == "unchanged" and session.writes == 0
        assert not session.config.state_dir.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwargs", [{}, {"filter_ids": ["f", "f"]}, {"filter_ids": [""]}, {"inherit_filters": "false"}]
)
def test_strict_models_distinguish_no_change_clear_and_false(kwargs):
    with pytest.raises(ValidationError):
        ComponentFilterChange(component_id="c", **kwargs)
