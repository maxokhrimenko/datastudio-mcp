import asyncio
import copy

import pytest
from test_service import Session

from datastudio_mcp.browser import StudioError
from datastudio_mcp.document import fingerprint
from datastudio_mcp.service import Studio
from datastudio_mcp.settings import ComponentSettings, PropertyChange, Settings
from test_switching import SwitchAdapter, SwitchSession


@pytest.mark.parametrize(
    "mode,code", [("draft", "UNPUBLISHED_DATASOURCE"), ("concurrent", "VERSION_CONFLICT")]
)
def test_source_field_edits_refuse_unpublished_or_changed_schema(tmp_path, mode, code):
    class Adapter(SwitchAdapter):
        async def evaluate(self, script, arguments):
            assert arguments["fieldMode"] is True
            self.saved.draft["revision"] += 1
            self.saved.published = copy.deepcopy(self.saved.draft)
            return {"edits": [], "properties": []}

    async def scenario():
        session = SwitchSession(tmp_path)
        before = copy.deepcopy(session.page)
        if mode == "draft":
            session.draft["unpublished"] = True
        settings = Settings(Studio(Adapter(session, [])), field_mode=True)
        with pytest.raises(StudioError) as caught:
            await settings.patch(
                "p_test",
                [
                    ComponentSettings(
                        component_id="c",
                        properties=[
                            PropertyChange(key="actionConceptList", value=[]),
                        ],
                    )
                ],
                fingerprint(before),
                dry_run=False,
            )
        assert caught.value.code == code
        assert session.writes == 0 and session.page == before

    asyncio.run(scenario())


def test_native_delta_preserves_saved_fields_and_supports_undo(tmp_path, monkeypatch):
    async def scenario():
        session = Session(tmp_path)
        original = copy.deepcopy(session.page)
        studio = Studio(session)
        settings = Settings(studio)

        async def prepare(frame, changes):
            return {
                "edits": [
                    {
                        "before": {"componentId": "draft", "text": "before"},
                        "after": {"componentId": "draft", "text": "after"},
                    }
                ],
                "properties": [{"key": "text", "before": "before", "after": "after"}],
            }

        monkeypatch.setattr(settings, "_prepare", prepare)
        changes = [
            ComponentSettings(
                component_id="cd-test", properties=[PropertyChange(key="text", value="after")]
            )
        ]
        token = fingerprint(session.page)
        plan = await settings.patch("p_test", changes, token)
        assert session.writes == 0 and plan["status"] == "planned"
        result = await settings.patch("p_test", changes, token, dry_run=False)
        assert result["status"] == "verified"
        assert session.page["componentConfig"][0]["opaque"] == [1, 2]
        assert session.page["componentConfig"][0]["componentId"] == "cd-test"
        restored = await studio.restore_operation(result["operation_id"], dry_run=False)
        assert restored["status"] == "verified" and session.page == original

    asyncio.run(scenario())


def test_style_refuses_source_side_effects(tmp_path, monkeypatch):
    async def scenario():
        session = Session(tmp_path)
        settings = Settings(Studio(session))

        async def prepare(frame, changes):
            return {
                "edits": [
                    {
                        "before": {"componentId": "draft"},
                        "after": {"componentId": "draft", "datasourceId": "foreign"},
                    }
                ],
                "properties": [],
            }

        monkeypatch.setattr(settings, "_prepare", prepare)
        changes = [
            ComponentSettings(
                component_id="cd-test", properties=[PropertyChange(key="fontSize", value="18px")]
            )
        ]
        with pytest.raises(StudioError, match="field/identity"):
            await settings.patch("p_test", changes, fingerprint(session.page), dry_run=False)
        assert session.writes == 0

    asyncio.run(scenario())
