from datastudio_mcp.document import json_equal
from datastudio_mcp.inventory import shared_options
from datastudio_mcp.publishing import content


def config(page_text="a", modifier="u1"):
    return {
        "report": {"attributeConfig": {"x": 1}, "lastModifierGaiaId": modifier},
        "navigationInfo": {"navItems": [{"page": {"pageId": "p1"}}]},
        "page": [{"pageId": "p1", "page": {"componentConfig": [{"text": page_text}]}}],
    }


def test_publish_diff_ignores_modifier_identity_but_sees_page_edits():
    assert json_equal(content(config(modifier="u1")), content(config(modifier="u2")))
    assert not json_equal(content(config("a")), content(config("b")))


def test_long_option_lists_are_shared_by_reference():
    fonts = [f"Font{i}" for i in range(20)]
    catalog = {
        "report": {"settings": {"themeFontFamily": {"options": fonts}}},
        "page": None,
        "types": {"t/default": {"panel": {"settings": {
            "fontFamily": {"options": list(fonts)}, "align": {"options": ["left", "right"]}}}}},
    }
    result = shared_options(catalog)
    ref = result["report"]["settings"]["themeFontFamily"]["options"]
    assert result["types"]["t/default"]["panel"]["settings"]["fontFamily"]["options"] == ref
    assert result["option_lists"] == {ref: fonts}
    settings = result["types"]["t/default"]["panel"]["settings"]
    assert settings["align"]["options"] == ["left", "right"]
