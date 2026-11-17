import pytest

from datastudio_mcp.browser import StudioError
from datastudio_mcp.inheritance import saved_inheritance


def component(component_id, filters, inherit=True):
    return {
        "componentId": component_id,
        "type": "simple-table",
        "datasourceId": "source",
        "propertyConfig": {
            "componentProperty": {"filters": filters, "inheritFilters": inherit}
        },
    }


def group(group_id, *, components=(), children=(), filters=(), inherit=True, passthrough=False):
    return {
        "groupId": group_id,
        "componentId": list(components),
        "childGroupId": list(children),
        "propertyConfig": {
            "groupProperty": {
                "filters": list(filters),
                "inheritFilters": inherit,
                "controlStatePassThrough": passthrough,
            }
        },
    }


def config(groups):
    return {
        "report": {
            "propertyConfig": {"reportProperty": {"filters": ["report-filter"]}}
        },
        "page": [
            {
                "pageId": "page",
                "page": {
                    "componentConfig": [component("chart", ["chart-filter"])],
                    "groupConfig": groups,
                    "propertyConfig": {
                        "pageProperty": {
                            "filters": ["page-filter"],
                            "inheritFilters": True,
                        }
                    },
                },
            }
        ],
    }


def test_nested_saved_inheritance_and_control_passthrough_scope():
    result = saved_inheritance(
        config(
            [
                group(
                    "outer",
                    children=["inner"],
                    filters=["outer-filter"],
                    passthrough=True,
                ),
                group(
                    "inner",
                    components=["chart"],
                    filters=["inner-filter"],
                    passthrough=True,
                ),
            ]
        ),
        "page",
    )
    item = result["components"][0]
    assert item["group_ids"] == ["outer", "inner"]
    assert item["effective_saved_filter_ids"] == [
        "report-filter",
        "page-filter",
        "outer-filter",
        "inner-filter",
        "chart-filter",
    ]
    assert item["runtime_control_scope"] == {"scope": "page", "id": "page"}


def test_inheritance_false_restarts_chain_at_exact_scope():
    result = saved_inheritance(
        config(
            [
                group("outer", children=["inner"], filters=["outer-filter"]),
                group(
                    "inner",
                    components=["chart"],
                    filters=["inner-filter"],
                    inherit=False,
                ),
            ]
        ),
        "page",
    )
    item = result["components"][0]
    assert item["effective_saved_filter_ids"] == [
        "inner-filter",
        "chart-filter",
    ]
    assert item["runtime_control_scope"] == {"scope": "group", "id": "inner"}


@pytest.mark.parametrize(
    "groups",
    [
        [group("a", children=["missing"])],
        [group("a", children=["b"]), group("b", children=["a"], components=["chart"])],
        [group("a", components=["chart"]), group("b", components=["chart"])],
    ],
)
def test_invalid_group_graph_fails_closed(groups):
    with pytest.raises(StudioError):
        saved_inheritance(config(groups), "page")
