import asyncio
import copy

import pytest

from test_component_filters import BrowserAdapter, FilterSession
from datastudio_mcp.browser import StudioError
from datastudio_mcp.component_filters import FilterBinding
from datastudio_mcp.document import fingerprint
from datastudio_mcp.page_filters import PageFilters
from datastudio_mcp.service import Studio


class PageFilterSession(FilterSession):
    def __init__(self, root, mode="valid"):
        super().__init__(root, mode)
        self.page["propertyConfig"] = {
            "pageProperty": {"filters": ["f1"], "inheritFilters": True, "opaque": {"keep": 4}}
        }

    async def evaluate(self, script, args):
        assert args["beforeIds"] == ["f1"] and args["beforeInherit"] is True
        if self.mode == "source-change":
            self.source["revision"] += 1
        if self.mode == "invalid":
            return {"error": "INVALID_FILTER"}
        return {
            "consumers": [
                {
                    "component_id": "cd-test",
                    "status": "available",
                    "compatibility": [
                        {"filter_id": f["id"], "valid": False} for f in args["filters"]
                    ],
                }
            ]
        }


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"filter_ids": []}, {"filters": [], "inheritFilters": True}),
        ({"inherit_filters": False}, {"filters": ["f1"], "inheritFilters": False}),
        ({"filter_ids": ["f2", "f1"]}, {"filters": ["f2", "f1"], "inheritFilters": True}),
    ],
)
def test_page_local_binding_keeps_children_and_metadata_and_undo(tmp_path, change, expected):
    async def scenario():
        session = PageFilterSession(tmp_path)
        studio = Studio(BrowserAdapter(session))
        before = copy.deepcopy(session.page)
        args = (
            "p_test",
            FilterBinding(**change),
            fingerprint(before),
            fingerprint(session.report_content),
        )
        preview = await PageFilters(studio).patch(*args)
        assert preview["status"] == "planned" and session.page == before
        assert session.writes == 0 and not session.config.state_dir.exists()
        result = await PageFilters(studio).patch(*args, dry_run=False)
        assert result["status"] == "verified"
        intended = copy.deepcopy(before)
        intended["propertyConfig"]["pageProperty"].update(expected)
        assert session.page == intended
        assert (await studio.restore_operation(result["operation_id"], dry_run=False))[
            "status"
        ] == "verified"
        assert session.page == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,code",
    [
        ("invalid", "INVALID_FILTER"),
        ("source-change", "VERSION_CONFLICT"),
        ("draft", "UNPUBLISHED_DATASOURCE"),
        ("resource-race", "VERSION_CONFLICT"),
    ],
)
def test_page_filter_validation_and_dependency_failure_never_dispatches(tmp_path, mode, code):
    async def scenario():
        session = PageFilterSession(tmp_path, mode)
        before = copy.deepcopy(session.page)
        with pytest.raises(StudioError) as caught:
            await PageFilters(Studio(BrowserAdapter(session))).patch(
                "p_test",
                FilterBinding(filter_ids=["f2"]),
                fingerprint(before),
                fingerprint(session.report_content),
                dry_run=False,
            )
        assert caught.value.code == code and session.writes == 0 and session.page == before

    asyncio.run(scenario())


def test_missing_filter_is_not_silently_dropped(tmp_path):
    async def scenario():
        session = PageFilterSession(tmp_path)
        with pytest.raises(StudioError) as caught:
            await PageFilters(Studio(BrowserAdapter(session))).patch(
                "p_test",
                FilterBinding(filter_ids=["f2", "missing"]),
                fingerprint(session.page),
                fingerprint(session.report_content),
                dry_run=False,
            )
        assert caught.value.code == "NOT_FOUND"
        assert session.writes == 0 and not session.config.state_dir.exists()

    asyncio.run(scenario())
