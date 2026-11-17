"""Typed component creation from the live editor's native presets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .browser import StudioError
from .document import fingerprint, json_equal, walk_components
from .field_models import ChartField, SourceField
from .fields import FieldSelection, selection_changes
from .formulas import Formulas
from .nativeops import NativeOps
from .report_resources import report_content

if TYPE_CHECKING:
    from .service import Studio


# Community visualization types: manifest path (gs://) or Google-hosted (google://).
COMMUNITY_PREFIXES = ("gs://", "google://")


class NewComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    type: str = Field(min_length=1)
    preset: str = "default"
    datasource_id: str | None = None
    dimensions: list[ChartField | SourceField] = Field(default_factory=list, max_length=100)
    metrics: list[ChartField | SourceField] = Field(default_factory=list, max_length=100)
    left: float = 20
    top: float = 20
    width: float = Field(default=400, ge=0)
    height: float = Field(default=200, ge=0)
    rotation: float = 0


# Datasource dataType of dates, date parts (month, week, quarter) and timestamps.
TIME_TYPES = frozenset({8, 9, 10})


def time_axis(spec: NewComponent, index: int, fields: dict[str, dict] | None,
              validated: dict | None) -> bool:
    """The first dimension is a date: the chart reads along time, not by size."""
    if not spec.dimensions:
        return False
    first = spec.dimensions[0]
    expression = getattr(first, "expression", None)
    if expression is not None:
        column = (validated or {}).get((str(index), expression)) or {}
        return column.get("dataType") in TIME_TYPES
    return (fields or {}).get(first.name, {}).get("dataType") in TIME_TYPES


def creation_requests(specs: list[NewComponent], validated: dict | None = None,
                      source_fields: dict[str, dict[str, dict]] | None = None) -> list[dict]:
    requests = []
    for index, spec in enumerate(specs):
        request = spec.model_dump()
        request["time_axis"] = time_axis(
            spec, index, (source_fields or {}).get(spec.datasource_id), validated)
        selection = FieldSelection(
            component_id=str(index),
            dimensions=[ChartField(**field.model_dump()) for field in spec.dimensions],
            metrics=[ChartField(**field.model_dump()) for field in spec.metrics],
        )
        properties = selection_changes([selection], validated)[0].properties
        request["field_definitions"] = {
            "dimensions" if prop.key == "actionConceptList" else "metrics": prop.value
            for prop in properties
        }
        requests.append(request)
    return requests


class Creation:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def create(
        self, page_id: str, specs: list[NewComponent], expected: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        if not 1 <= len(specs) <= 100:
            raise StudioError("INVALID_BATCH", "Create between 1 and 100 components")
        for spec in specs:
            if any(getattr(field, "concept_name", None) for field in spec.dimensions + spec.metrics):
                raise StudioError("CONCEPT_NOT_FOUND", "New components cannot retain existing concepts")
            if spec.type.startswith(COMMUNITY_PREFIXES) and (spec.dimensions or spec.metrics):
                raise StudioError(
                    "COMMUNITY_VIZ_FIELDS",
                    "Community visualizations take the editor's default fields; their data "
                    "slots are manifest-defined (edit them with components_patch)")
            if (spec.dimensions or spec.metrics) and not spec.datasource_id:
                raise StudioError("MISSING_DATASOURCE", "Field bindings require a datasource")
            if any(field.aggregation is None for field in spec.metrics):
                raise StudioError("MISSING_AGGREGATION", "Each metric needs explicit aggregation")
        # Read-only preparation: sources must be readable with equal draft/published
        # schemas; every expression is validated natively before anything is created.
        source_tokens = {}
        source_fields: dict[str, dict[str, dict]] = {}
        resources = report_content((await self.studio.report())["reportConfig"]).get(
            "resource", {})
        blends = {e["key"] for e in resources.get("dataViewResource", {}).get("entry", [])}
        for spec in specs:
            if spec.datasource_id in blends and any(
                    getattr(f, "expression", None) for f in spec.dimensions + spec.metrics):
                raise StudioError("BLEND_FORMULA_UNSUPPORTED",
                                  "Use blend output field names; add formulas to the blend inputs")
        for source_id in sorted({spec.datasource_id for spec in specs
                                 if spec.datasource_id and spec.datasource_id not in blends}):
            source = await self.studio.datasource(source_id)
            if source["published"].get("isUnlocked") is False and any(
                    getattr(f, "expression", None) for spec in specs
                    if spec.datasource_id == source_id for f in spec.dimensions + spec.metrics):
                # The viewer renders such a chart, and every chart batched with it, as a
                # "User Configuration Error".
                raise StudioError(
                    "FIELD_EDITING_DISABLED",
                    "Field editing in reports is off for this datasource: a chart formula "
                    "breaks the chart. Add it as a datasource calculated field "
                    "(datasource_calculated_fields_create) or turn field editing on "
                    "(datasource_settings_set)")
            if not json_equal(source["draft"], source["published"]):
                raise StudioError(
                    "UNPUBLISHED_DATASOURCE",
                    "Component creation requires matching draft/published schema",
                )
            source_tokens[source_id] = fingerprint(source)
            source_fields[source_id] = {
                f["field"]["name"]: f
                for f in source["published"].get("datasourceBlock", {}).get("fields", [])
                if isinstance(f.get("field"), dict)
            }
        validated = {}
        for index, spec in enumerate(specs):
            for field in spec.dimensions + spec.metrics:
                expression = getattr(field, "expression", None)
                if expression is None or (str(index), expression) in validated:
                    continue
                result = await Formulas(self.studio).validate(spec.datasource_id, expression)
                if not result["valid"]:
                    raise StudioError("FORMULA_VALIDATION_FAILED", str(result["diagnostics"]))
                validated[(str(index), expression)] = result["column"]

        async def unchanged_sources() -> None:
            for source_id, token in source_tokens.items():
                if fingerprint(await self.studio.datasource(source_id)) != token:
                    raise StudioError("VERSION_CONFLICT", "Datasource changed during creation")

        result = await NativeOps(self.studio).run(
            _PREPARE,
            {"specs": creation_requests(specs, validated, source_fields)},
            page_id=page_id,
            expected_fingerprint=expected,
            dry_run=dry_run,
            plan={"page_id": page_id, "atomic_scope": "page"},
            guard=unchanged_sources,
        )
        if result.get("status") == "planned":
            ids = set(result["created_component_ids"])
            result["added_components"] = [
                c for c in walk_components(result["after"]["componentConfig"])
                if c["componentId"] in ids
            ]
        return result


_PREPARE = """async args => {
    // The editor's own component-creation service (Insert menu / drag-draw): native type
    // and preset defaults, default fields and behaviours exactly as the UI stores them.
    const X=OPS(args),{owner,registry}=X;
    if(owner.isResponsiveLayout())X.stop('RESPONSIVE_LAYOUT_REQUIRES_SEPARATE_OPERATION');
    const creator=R.componentCreator(X.i);
    const before=X.serialize();
    const ids=[];
    for(const spec of args.specs){
        // A community visualization's type exists once registered, as the Insert gallery
        // and the resource panel do: load its registration, then register its type.
        const community=/^(gs|google):[/][/]/.test(spec.type);
        if(community&&!registry.getDisplayTypeObj(spec.type)){
            const listed=Object.keys(X.report.getResourcesByKey('communityVizResource')||{});
            if(!listed.includes(spec.type))X.stop('COMMUNITY_VIZ_NOT_REGISTERED',spec.type);
            try{await R.exported('vizRegistrationLoader')(R.vizRegistry(X.i),spec.type,
                X.report.getId());}
            catch(error){X.stop('COMMUNITY_VIZ_UNAVAILABLE',String(error?.message||error));}
            R.exported('registerDisplayType')(registry,spec.type,
                'Community visualizations and components','viz',
                R.vizTypeFactory(X.i).get(spec.type));
        }
        const type=registry.getDisplayTypeObj(spec.type);
        if(!type?.getPresetConfigNames().includes(spec.preset))
            X.stop('UNKNOWN_TYPE_OR_PRESET',`${spec.type}/${spec.preset}`);
        if(registry.getDisplayTypeGroup(spec.type)==='composite')
            X.stop('COMPOSITE_CREATION_REQUIRES_CHILDREN',spec.type);
        if(type.getNeedData()&&!spec.datasource_id)
            X.stop('MISSING_DATASOURCE',spec.type);
        const c=await creator.create({type:spec.type,presetConfigName:spec.preset,
            creationMethod:'Clickdraw',datasourceId:spec.datasource_id||'',parent:owner,
            geometry:{top:spec.top,left:spec.left,width:spec.width,height:spec.height}});
        if(typeof c?.getId!=='function')
            X.stop('NATIVE_CONTRACT_CHANGED','creation returned no model');
        if(c.isReady$?.value!==true)await c.getReadyAsync();
        // A line is its two end points in page coordinates, drawn from the box's top-left to
        // its bottom-right (height 0: horizontal); its box follows them.
        // The editor snaps geometry to its grid; requested geometry wins, as a drag would.
        c.setAttributeValues?.({top:spec.top,left:spec.left,width:spec.width,height:spec.height,
            rotation:spec.rotation});
        const ends=c.getDisplayKeyValue?.('terminals');
        if(ends)c.setDisplayKeyValue('terminals',{...ends,
            start:{...ends.start,top:spec.top,left:spec.left},
            end:{...ends.end,top:spec.top+spec.height,left:spec.left+spec.width}});
        const explicit=spec.dimensions.length||spec.metrics.length;
        if(explicit){
            const defs=c.getConfig().conceptDefs;
            for(const role of ['dimensions','metrics']){
                const names=spec.field_definitions[role].map(definition=>{
                    defs[definition.name]=structuredClone(definition);
                    return definition.name;
                });
                const current=c.getDataSourceKeyValue(role);
                // An empty role clears preset fields; with nothing to clear it stays absent
                // (a bullet has no dimensions, and the server refuses an empty list there).
                if(!names.length&&!current?.primary?.length)continue;
                // A chart with a breakdown slot (bars, lines, areas) takes its second
                // dimension there, as the editor's "Breakdown dimension" does. With one
                // dimension the preset's breakdown stays: an area chart requires one.
                if(role==='dimensions'&&Array.isArray(current?.breakdown)&&names.length>1)
                    c.setDataSourceKeyValue(role,{...current,primary:names.slice(0,1),
                        breakdown:names.slice(1)});
                else c.setDataSourceKeyValue(role,{...current,primary:names});
            }
            // A breakdown preset (stacked bars, ...) takes one metric: the editor greys out the
            // second metric slot, and the server refuses the page otherwise.
            const breakdown=(c.getDataSourceKeyValue('dimensions')||{}).breakdown;
            if(spec.metrics.length>1&&Array.isArray(breakdown)&&breakdown.length)
                X.stop('INVALID_BINDINGS',`preset ${spec.preset} breaks one metric down by a `+
                    'dimension; for several metrics side by side or stacked use preset default '+
                    'and settings_set showStack');
            const fields=spec.field_definitions;
            // A date axis reads along time: oldest first, as the editor sorts a date dimension.
            // Otherwise the largest values come first.
            const byTime=spec.time_axis&&fields.dimensions[0]?.name;
            const sort=byTime||fields.metrics[0]?.name||fields.dimensions[0]?.name;
            // Only a preset that sorts (bars, tables: a default sortData) takes the sort; a
            // bullet or gauge has none, and the server refuses one.
            const presetSort=c.getDataSourceKeyValue('sortData');
            if(sort&&Array.isArray(presetSort)&&presetSort.length){
                c.setDataSourceKeyValue('sortData',[{sortColumn:sort,sortDir:byTime?0:1}]);
                // The chart sorts by its behavior, which keeps the preset's sort until synced.
                R.exported('sortBehaviorSync')(c);
            }
            // A line over dates joins its points: with the default "line to zero" a monthly
            // series drops to zero on every day between two months (a comb of spikes).
            if(byTime&&/^simple-(line|area)chart$/.test(spec.type)&&
                    'missingDataDisplayType' in (c.getDefaultDisplayKeys?.()||{}))
                c.setDisplayKeyValue('missingDataDisplayType','linearInterpolation');
            c.syncState();
            const schema=c.getSchema();schema.getConcepts();
            for(const field of [...spec.dimensions,...spec.metrics])
                if(field.name&&!R.schemaField(schema,field.name).isValid())
                    X.stop('UNKNOWN_FIELD',field.name);
        }
        c.validate?.();
        const errors=X.clean(c.getConfigErrors?.()||{});
        if(Object.values(errors).some(v=>Object.keys(v||{}).length))
            X.stop('INVALID_COMPONENT_CONFIGURATION',errors);
        ids.push(c.getId());
    }
    return {before,after:X.serialize(),created_component_ids:ids};
}"""
