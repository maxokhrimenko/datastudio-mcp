"""Shared field inputs, independent of component creation/edit orchestration."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    aggregation: int | None = Field(default=None, ge=0)


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
