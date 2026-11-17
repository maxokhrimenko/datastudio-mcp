import pytest
from pydantic import ValidationError

from datastudio_mcp.panels import Target


@pytest.mark.parametrize("values", [
    {"kind": "report", "page_id": "p"},
    {"kind": "page"},
    {"kind": "component", "page_id": "p"},
    {"kind": "page", "page_id": "p", "component_id": "c"},
    {"kind": "component", "component_id": "c"},
])
def test_target_rejects_inconsistent_scope(values):
    with pytest.raises(ValidationError):
        Target(**values)


def test_target_accepts_each_scope():
    Target(kind="report")
    Target(kind="page", page_id="p")
    Target(kind="component", page_id="p", component_id="c")
