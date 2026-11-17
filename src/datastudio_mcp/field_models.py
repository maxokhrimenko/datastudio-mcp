"""Shared field inputs, independent of component creation/edit orchestration."""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

# The editor's aggregation codes, confirmed on known data. A number is still accepted for
# the rest (FIRST 8, LAST 9, PERCENTILE 13, APPROX_COUNT_DISTINCT 14 are unconfirmed).
AGGREGATIONS = {"NONE": 0, "AVG": 1, "COUNT": 2, "COUNT_DISTINCT": 3, "MAX": 4, "MIN": 5,
                "SUM": 6, "AUTO": 7, "MEDIAN": 10, "VARIANCE": 11, "STDDEV": 12}


def _aggregation_code(value):
    if isinstance(value, str):
        key = value.strip().upper().replace(" ", "_")
        if key not in AGGREGATIONS:
            raise ValueError(f"Unknown aggregation {value!r}; use one of {', '.join(AGGREGATIONS)}")
        return AGGREGATIONS[key]
    if isinstance(value, int) and not isinstance(value, bool) and not 0 <= value <= 14:
        raise ValueError("Aggregation codes run from 0 to 14")
    return value


Aggregation = Annotated[
    int | Literal["NONE", "AVG", "COUNT", "COUNT_DISTINCT", "MAX", "MIN", "SUM", "AUTO",
                  "MEDIAN", "VARIANCE", "STDDEV"],
    BeforeValidator(_aggregation_code),
    Field(description="Aggregation name (SUM, COUNT_DISTINCT, ...) or the editor's code"),
]


class SourceField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    aggregation: Aggregation | None = None


class ChartField(SourceField):
    concept_name: str | None = Field(default=None, pattern=r"^qt_[A-Za-z0-9_]+$")
    name: str | None = Field(default=None, min_length=1)
    expression: str | None = Field(default=None, min_length=1, max_length=100_000)
    display_name: str | None = None

    @model_validator(mode="after")
    def exactly_one_definition(self):
        if (self.name is None) == (self.expression is None):
            raise ValueError("Provide exactly one of name or expression")
        return self
