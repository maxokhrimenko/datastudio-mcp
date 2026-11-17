import pytest
from pydantic import ValidationError

from datastudio_mcp.creation import NewComponent
from datastudio_mcp.datasources import CalculatedField
from datastudio_mcp.field_models import AGGREGATIONS, ChartField


@pytest.mark.parametrize(("given", "code"), [
    ("SUM", 6), ("count_distinct", 3), ("Count Distinct", 3), ("AVG", 1), (2, 2)])
def test_aggregation_takes_a_name_or_a_code(given, code):
    assert ChartField(name="revenue", aggregation=given).aggregation == code


@pytest.mark.parametrize("given", ["DISTINCT", 15, -1])
def test_unknown_aggregation_is_refused_before_anything_is_sent(given):
    with pytest.raises(ValidationError):
        ChartField(name="revenue", aggregation=given)


def test_names_reach_every_aggregation_input():
    spec = NewComponent(type="kpi-metric", datasource_id="ds",
                        metrics=[{"name": "product", "aggregation": "COUNT_DISTINCT"}])
    assert spec.metrics[0].aggregation == AGGREGATIONS["COUNT_DISTINCT"] == 3
    field = CalculatedField(output_name="Total", expression="SUM(t0.x)", aggregation="AUTO")
    assert field.aggregation == 7
