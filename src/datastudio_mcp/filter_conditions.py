"""Typed saved-filter trees checked against the native condition/schema contract."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .browser import StudioError
from .bundle import native_script
from .document import fingerprint, json_equal
from .field_models import ChartField
from .fields import FieldSelection, formula_concept_type, selection_changes
from .formulas import Formulas

if TYPE_CHECKING:
    from .service import Studio


class FilterPredicate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    kind: Literal["predicate"] = "predicate"
    field: ChartField
    role: Literal["dimension", "metric"] = "dimension"
    condition: str = Field(pattern=r"^[A-Z]+$", max_length=20)
    include: bool = True
    string_values: list[str] = Field(default_factory=list, max_length=1000)
    number_values: list[float] = Field(default_factory=list, max_length=1000)

    @field_validator("number_values", mode="before")
    @classmethod
    def avoid_integer_rounding(cls, values):
        if isinstance(values, list) and any(
            type(value) is int and abs(value) > 2**53 - 1 for value in values
        ):
            raise ValueError("Integer filter values must fit JavaScript's safe integer range")
        return values


class FilterGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["and", "or"]
    children: list[FilterNode] = Field(min_length=1, max_length=100)


FilterNode = Annotated[FilterPredicate | FilterGroup, Field(discriminator="kind")]
FilterGroup.model_rebuild()


def predicates(tree: FilterNode) -> list[tuple[str, FilterPredicate]]:
    result = []
    count = 0

    def visit(node, path="root", depth=0):
        nonlocal count
        count += 1
        if depth > 8 or count > 100:
            raise StudioError("FILTER_TOO_COMPLEX", "At most 100 nodes and 8 nested groups")
        if isinstance(node, FilterPredicate):
            if node.field.concept_name:
                raise StudioError("CONCEPT_NOT_FOUND", "A new filter cannot retain a chart-local concept ID")
            result.append((path, node))
        else:
            for index, child in enumerate(node.children):
                visit(child, f"{path}/{index}", depth + 1)

    visit(tree)
    return result


def filter_definition(tree: FilterNode, validated: dict | None = None) -> dict:
    predicates(tree)

    def build(node, path="root"):
        if isinstance(node, FilterGroup):
            return {node.kind: {"filterDefinitions": [
                build(child, f"{path}/{index}") for index, child in enumerate(node.children)]}}
        role = "dimensions" if node.role == "dimension" else "metrics"
        selected = FieldSelection(component_id=path, **{role: [node.field]})
        concept = selection_changes([selected], validated)[0].properties[0].value[0]
        return {"filterExpression": {
            "include": node.include, "conceptType": 0 if node.role == "dimension" else 1,
            "concept": {"ns": concept["namespace"], "name": concept["name"]},
            "filterConditionType": node.condition,
            "stringValues": node.string_values, "numberValues": node.number_values,
            "queryTimeTransformation": concept["queryTimeTransformation"],
        }}

    return build(tree)


class FilterConditions:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def catalog(self) -> dict[str, Any]:
        return await self._native({"catalogOnly": True})

    async def validate(self, datasource_id: str, tree: FilterNode) -> dict[str, Any]:
        leaves = predicates(tree)
        source = await self.studio.datasource(datasource_id)
        if not json_equal(source["draft"], source["published"]):
            raise StudioError("UNPUBLISHED_DATASOURCE", "Filter validation requires matching draft/published")
        token = fingerprint(source)
        fields = source["draft"].get("datasourceBlock", {}).get("fields")
        if not isinstance(fields, list):
            raise StudioError("INVALID_DATASOURCE", "Datasource fields are missing")
        by_name = {field["field"]["name"]: field for field in fields
                   if field.get("field", {}).get("ns") == "t0"}
        validated, physical = {}, {}
        for path, leaf in leaves:
            if leaf.field.name is not None:
                field = by_name.get(leaf.field.name)
                if not field or field.get("enabled") is not True:
                    raise StudioError("FIELD_NOT_FOUND", "Filter field is missing or disabled")
                physical[leaf.field.name] = formula_concept_type(field)
            else:
                result = await Formulas(self.studio).validate(datasource_id, leaf.field.expression)
                if not result["valid"]:
                    return {"valid": False, "status": "formula_validation_failed", "path": path,
                            "diagnostics": result, "datasource_id": datasource_id}
                validated[(path, leaf.field.expression)] = result["column"]
        definition = filter_definition(tree, validated)
        if fingerprint(await self.studio.datasource(datasource_id)) != token:
            raise StudioError("VERSION_CONFLICT", "Datasource changed before native filter validation")
        result = await self._native({"datasourceId": datasource_id, "definition": definition,
                                     "physicalTypes": physical, "catalogOnly": False})
        if fingerprint(await self.studio.datasource(datasource_id)) != token:
            raise StudioError("VERSION_CONFLICT", "Datasource changed during native filter validation")
        return {**result, "datasource_id": datasource_id, "source_fingerprint": token,
                "definition": definition, "filter_version": 3,
                "scope": "Native condition validation only; no saved resource or query-result proof. "
                         "Does not independently compile regex or validate backend-specific execution."}

    async def _native(self, arguments: dict) -> dict:
        page = await self.studio.session.page()
        try:
            result = await asyncio.wait_for(page.evaluate(native_script(_VALIDATE), {
                "reportId": self.studio.config.report_id, **arguments,
            }), timeout=self.studio.config.timeout_ms / 1000)
        except Exception as exc:
            raise StudioError("NATIVE_CONTRACT_FAILED", "Cannot inspect native filter contract") from exc
        if result.get("error"):
            raise StudioError(result["error"], "Native filter catalog/schema is unavailable")
        return result


_VALIDATE = """args => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService'))return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport();
    if(!report||report.getId()!==args.reportId)return {error:'OUT_OF_SCOPE'};
    if(Object.values(report.isChanged||{}).some(Boolean))return {error:'EDITOR_DIRTY'};
    const values=Object.values(window.default_datastudio||{});
    const catalogs=values.filter(value=>value&&typeof value==='object'&&
        ['T','F','IN','BT','ANY','RM','IBL'].every(code=>
            value[code]?.label&&Array.isArray(value[code].dataTypes)));
    if(catalogs.length!==1)return {error:'FILTER_CONTRACT_CHANGED'};
    if(args.catalogOnly)return {conditions:Object.entries(catalogs[0]).map(([code,metadata])=>
        ({code,...JSON.parse(JSON.stringify(metadata))})),
        scope:'Native condition catalog; individual UI controls may expose a subset'};
    const validators=R.candidates('filterValidator');
    if(validators.length!==1||!i.has('schemaService'))return {error:'FILTER_CONTRACT_CHANGED'};
    const schema=i.get('schemaService').getSchema(args.datasourceId);
    if(!schema||schema.isDummy())return {error:'SCHEMA_NOT_READY'};
    for(const [name,type] of Object.entries(args.physicalTypes||{})){
        const field=R.schemaField(schema,name);
        if(!field.isValid()||R.fieldType(field)!==type)return {error:'SCHEMA_STALE'};
    }
    const valid=validators[0]({version:3,filterDefinition:args.definition},schema);
    return {valid,status:valid?'valid':'invalid'};
}"""
