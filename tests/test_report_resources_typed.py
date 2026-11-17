"""Offline guards of field overrides, community visualization registration, the
theme-from-image options and the community creation guard."""

import asyncio
import copy

import pytest
from test_scope_variables import Session

from datastudio_mcp.browser import StudioError
from datastudio_mcp.community_viz import CommunityViz
from datastudio_mcp.creation import Creation, NewComponent
from datastudio_mcp.document import fingerprint
from datastudio_mcp.field_overrides import FieldOverride, FieldOverrides, FieldRef
from datastudio_mcp.report_resources import report_content
from datastudio_mcp.service import Studio
from datastudio_mcp.theme_image import _preview

GAUGE = "google://devrel/googlecharts gauge"


def studio(tmp_path):
    session = Session(tmp_path)
    s = Studio(session)

    async def sources():
        return [{"id": "ds1"}]

    async def datasource(source_id):
        fields = [{"field": {"name": "_biz_"}, "outputName": "Business"}]
        draft = {"datasourceBlock": {"fields": fields}}
        return {"draft": draft, "published": copy.deepcopy(draft)}

    s.sources, s.datasource = sources, datasource
    return s, session


def run(coro):
    return asyncio.run(coro)


def token(session):
    return fingerprint(report_content(session.raw))


def test_field_override_plan_uses_source_names_and_guards(tmp_path):
    s, session = studio(tmp_path)
    plan = run(FieldOverrides(s).write(
        [FieldOverride(datasource_id="ds1", field="_biz_", display_name="Product",
                       id_override="business")], [], token(session)))
    assert plan["after"] == [{"originalId": "_biz_", "originalName": "Business",
                              "datasourceId": "ds1", "displayNameOverride": "Product",
                              "idOverride": "business"}]
    cases = [
        ([FieldOverride(datasource_id="ds1", field="nope", display_name="x")], [],
         "FIELD_NOT_FOUND"),
        ([FieldOverride(datasource_id="ds9", field="_biz_", display_name="x")], [],
         "DATASOURCE_NOT_ATTACHED"),
        ([], [FieldRef(datasource_id="ds1", field="_biz_")], "NOT_FOUND"),
    ]
    for upserts, deletes, code in cases:
        with pytest.raises(StudioError) as error:
            run(FieldOverrides(s).write(upserts, deletes, token(session)))
        assert error.value.code == code
    with pytest.raises(ValueError):
        FieldOverride(datasource_id="ds1", field="_biz_")
    with pytest.raises(ValueError):
        FieldOverride(datasource_id="ds1", field="_biz_", id_override="bad id")


def test_community_viz_register_and_refuse_removal_in_use(tmp_path):
    s, session = studio(tmp_path)
    plan = run(CommunityViz(s).write(GAUGE, True, token(session)))
    assert plan["after"] == [{"vizId": GAUGE, "value": [{}]}]
    with pytest.raises(StudioError) as error:
        run(CommunityViz(s).write("http://x", True, token(session)))
    assert error.value.code == "INVALID_VIZ_ID"
    session.raw["report"]["resource"]["communityVizResource"] = [{"vizId": GAUGE, "value": [{}]}]
    components = session.raw["page"][0]["page"]["componentConfig"]
    components.append({"componentId": "cd-g", "type": GAUGE})
    listed = run(CommunityViz(s).list())
    assert listed["visualizations"] == [{"viz_id": GAUGE, "components": ["cd-g"]}]
    with pytest.raises(StudioError) as error:
        run(CommunityViz(s).write(GAUGE, False, token(session)))
    assert error.value.code == "VIZ_IN_USE"
    assert run(CommunityViz(s).write(GAUGE, True, token(session)))["status"] == "unchanged"


def test_community_components_take_native_default_fields(tmp_path):
    s, _ = studio(tmp_path)
    spec = NewComponent(type=GAUGE, datasource_id="ds1", metrics=[{"name": "_biz_",
                                                                     "aggregation": 7}])
    with pytest.raises(StudioError) as error:
        run(Creation(s).create("p_a", [spec], "x"))
    assert error.value.code == "COMMUNITY_VIZ_FIELDS"


def test_theme_option_preview():
    scheme = {"grid": [{"colors": ["#111111", "#222222"]}], "themeMappings": [
        {"type": 1, "color": {"color": "#FFFFFF"}}, {"type": 3, "color": {"color": "#FF0000"}},
        {"type": 14, "colorArray": {"colors": [{"color": "#000000"}]}}]}
    assert _preview(scheme) == {"colors": {"fill": "#FFFFFF", "accent_fill": "#FF0000"},
                                "series": ["#000000"], "palette": [["#111111", "#222222"]]}


def test_field_override_update_keeps_unnamed_values_and_ids_stay_unique(tmp_path):
    s, session = studio(tmp_path)
    existing = {"originalId": "_biz_", "originalName": "Business", "datasourceId": "ds1",
                "idOverride": "business", "serverField": 1}
    session.raw["report"]["resource"]["fieldOverridesResource"] = [existing]
    plan = run(FieldOverrides(s).write(
        [FieldOverride(datasource_id="ds1", field="_biz_", display_name="Product")], [],
        token(session)))
    assert plan["after"] == [{**existing, "displayNameOverride": "Product"}]

    async def datasource(source_id):
        fields = [{"field": {"name": n}} for n in ("_biz_", "_other_")]
        draft = {"datasourceBlock": {"fields": fields}}
        return {"draft": draft, "published": copy.deepcopy(draft)}

    s.datasource = datasource
    with pytest.raises(StudioError) as error:
        run(FieldOverrides(s).write(
            [FieldOverride(datasource_id="ds1", field="_biz_", id_override="_other_")], [],
            token(session)))
    assert error.value.code == "DUPLICATE_FIELD_ID"


def test_viz_in_use_sees_grouped_and_report_components(tmp_path):
    s, session = studio(tmp_path)
    manifest = "gs://bucket/viz"
    session.raw["report"]["resource"]["communityVizResource"] = [{"vizId": manifest}]
    session.raw["report"]["componentConfig"].append(
        {"componentId": "cd-group", "componentConfig": [
            {"componentId": "cd-in", "type": manifest + " chart"}]})
    assert run(CommunityViz(s).list())["visualizations"][0]["components"] == ["cd-in"]
    with pytest.raises(StudioError) as error:
        run(CommunityViz(s).write(manifest, False, token(session)))
    assert error.value.code == "VIZ_IN_USE"


def test_theme_image_refuses_non_images(tmp_path):
    from datastudio_mcp.theme_image import ThemeFromImage
    s, _ = studio(tmp_path)
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN KEY-----")
    with pytest.raises(StudioError) as error:
        run(ThemeFromImage(s).extract(str(secret), None, ""))
    assert error.value.code == "INVALID_IMAGE"
