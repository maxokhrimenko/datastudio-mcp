"""Chart-local field selections; datasource definitions are never changed here."""

from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .browser import StudioError
from .components import Components, select_top_level
from .field_models import ChartField
from .document import fingerprint, json_equal
from .formulas import Formulas
from .settings import ComponentSettings, PropertyChange, Settings

if TYPE_CHECKING:
    from .service import Studio


def formula_concept_type(column: dict) -> int:
    """Translate query-column types to concept types, as the native editor does.

    These are distinct enums: boolean is 1 in validation responses but 3 in
    concept transformations. Integer column types 3/4 both map to concept 1.
    Unknown/new types must not silently produce a different working field.
    """
    mapping = {0: 0, 1: 3, 2: 2, 3: 1, 4: 1, 8: 6, 9: 7}
    data_type = column.get("dataType")
    if type(data_type) is not int or data_type not in mapping:
        raise StudioError("UNSUPPORTED_FORMULA_TYPE", "Unknown validated formula column type")
    return mapping[data_type]


class FieldSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    component_id: str = Field(min_length=1)
    dimensions: list[ChartField] | None = Field(default=None, max_length=100)
    metrics: list[ChartField] | None = Field(default=None, max_length=100)


def selection_changes(
    selections: list[FieldSelection],
    validated: dict[tuple[str, str], dict] | None = None,
    existing: dict[tuple[str, str], dict] | None = None,
) -> list[ComponentSettings]:
    changes = []
    for selection in selections:
        properties = []
        for role, key in (("dimensions", "actionConceptList"), ("metrics", "metrics")):
            fields = getattr(selection, role)
            if fields is None:
                continue
            definitions = []
            for field in fields:
                if role == "metrics" and field.aggregation is None:
                    raise StudioError(
                        "MISSING_AGGREGATION", "Each metric needs explicit aggregation"
                    )
                name = field.concept_name or "qt_" + uuid.uuid4().hex[:20]
                if field.concept_name:
                    definition = copy.deepcopy((existing or {}).get((selection.component_id, name)))
                    if definition is None:
                        raise StudioError(
                            "CONCEPT_NOT_FOUND", "Existing concept must belong to this component"
                        )
                    if definition.get("namespace") != "t0":
                        raise StudioError(
                            "CONCEPT_NAMESPACE_UNSUPPORTED", "Only t0 concepts supported here"
                        )
                else:
                    definition = {"id": "t0." + name, "name": name, "namespace": "t0"}
                transformation = definition.setdefault("queryTimeTransformation", {})
                data = transformation.setdefault("dataTransformation", {})
                if field.expression is None:
                    for formula_key in (
                        "sourceType",
                        "textFormula",
                        "frontendTextFormula",
                        "formulaOutputDataType",
                    ):
                        data.pop(formula_key, None)
                data["sourceFieldName"] = field.name
                if field.aggregation is not None:
                    data["aggregation"] = field.aggregation
                if field.expression is not None:
                    column = (validated or {}).get((selection.component_id, field.expression))
                    if column is None:
                        raise StudioError(
                            "UNVALIDATED_FORMULA", "Formula must pass server validation"
                        )
                    if column.get("type") and field.aggregation != 7:
                        raise StudioError(
                            "INVALID_AGGREGATION", "Aggregate expressions require AUTO (7)"
                        )
                    data.update(
                        {
                            "sourceFieldName": name,
                            "sourceType": 1,
                            "textFormula": field.expression,
                            "frontendTextFormula": field.expression,
                            "formulaOutputDataType": formula_concept_type(column),
                        }
                    )
                    if column.get("outputSemanticInfo"):
                        transformation.setdefault("displayTransformation", {})[
                            "outputSemanticInfo"
                        ] = column["outputSemanticInfo"]
                if field.display_name is not None:
                    transformation.setdefault("displayTransformation", {})["displayName"] = (
                        field.display_name
                    )
                definitions.append(definition)
            properties.append(PropertyChange(key=key, value=definitions))
        if not properties:
            raise StudioError("EMPTY_SELECTION", "Provide dimensions and/or metrics")
        changes.append(
            ComponentSettings(component_id=selection.component_id, properties=properties)
        )
    return changes


class Fields:
    def __init__(self, studio: Studio):
        self.studio = studio
        self.settings = Settings(studio, field_mode=True)

    async def replace(
        self, page_id: str, selections: list[FieldSelection], expected: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        validated, expected_sources, existing = {}, {}, {}
        retained = [
            s
            for s in selections
            if any(f.concept_name for f in (s.dimensions or []) + (s.metrics or []))
        ]
        if retained:
            _, frame = await Components(self.studio)._checked(page_id, expected)
            components = select_top_level(frame["page"], [s.component_id for s in retained])
            for selection, component in zip(retained, components, strict=True):
                names = [
                    f.concept_name
                    for f in (selection.dimensions or []) + (selection.metrics or [])
                    if f.concept_name
                ]
                if len(set(names)) != len(names):
                    raise StudioError(
                        "DUPLICATE_CONCEPT_EDIT", "Edit each retained concept once per batch"
                    )
                for definition in component.get("conceptDefs", []):
                    existing[(selection.component_id, definition["name"])] = definition
        formula_selections = [
            s
            for s in selections
            if any(f.expression is not None for f in (s.dimensions or []) + (s.metrics or []))
        ]
        if formula_selections:
            _, frame = await Components(self.studio)._checked(page_id, expected)
            components = select_top_level(
                frame["page"], [s.component_id for s in formula_selections]
            )
            for selection, component in zip(formula_selections, components, strict=True):
                source_id = component.get("datasourceId")
                if not source_id:
                    raise StudioError("MISSING_DATASOURCE", "Formula needs an attached datasource")
                source = await self.studio.datasource(source_id)
                if not json_equal(source["draft"], source["published"]):
                    raise StudioError(
                        "UNPUBLISHED_SOURCE_VALIDATION_PENDING",
                        "Formula writes need matching draft/published source definitions",
                    )
                token = fingerprint(source["draft"])
                for field in (selection.dimensions or []) + (selection.metrics or []):
                    if field.expression is None:
                        continue
                    result = await Formulas(self.studio).validate(source_id, field.expression)
                    if not result["valid"]:
                        raise StudioError("FORMULA_VALIDATION_FAILED", str(result["diagnostics"]))
                    if result["source_draft_fingerprint"] != token:
                        raise StudioError("VERSION_CONFLICT", "Formula source changed")
                    validated[(selection.component_id, field.expression)] = result["column"]
                expected_sources[source_id] = token
        result = await self.settings.patch(
            page_id,
            selection_changes(selections, validated, existing),
            expected,
            dry_run=dry_run,
            expected_sources=expected_sources,
        )
        return {
            **result,
            "sort_policy": "native_preserve_existing_sort_dependencies",
            "field_ids": "concept_name retains ID; otherwise new provisional chart-local IDs",
        }
