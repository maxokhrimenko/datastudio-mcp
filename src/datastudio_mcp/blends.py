"""Lossless inventory of report-local data-view resources and query trees."""

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .browser import StudioError
from .bundle import native_script
from .document import fingerprint, tokens
from .filters import pointer
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio


def blend_inventory(config: dict, attached: set[str], *, include_definitions: bool = False) -> dict:
    report = config.get("report")
    if not isinstance(report, dict) or not isinstance(config.get("page"), list):
        raise StudioError("INVALID_REPORT", "Full report/pages required")
    resources = report.get("resource", {})
    if not isinstance(resources, dict):
        raise StudioError("INVALID_REPORT", "Malformed report resources")
    resource = resources.get("dataViewResource", {})
    if not isinstance(resource, dict) or not isinstance(resource.get("entry", []), list):
        raise StudioError("INVALID_REPORT", "Malformed data-view registry")
    entries = resource.get("entry", [])
    known = {}
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("key"), str)
                or not entry["key"] or not isinstance(entry.get("value"), dict)):
            raise StudioError("INVALID_REPORT", "Malformed data-view entry")
        if entry["key"] in known:
            raise StudioError("INVALID_REPORT", "Duplicate data-view ID")
        known[entry["key"]] = entry
    references = {key: [] for key in known}
    dangling = []

    def visit(value: Any, path=(), owner=None, parent=None):
        if (len(path) == 6 and path[:4] == ("report", "resource", "dataViewResource", "entry")
                and path[-1] == "key"):
            return
        if isinstance(value, dict):
            context = dict(owner or {})
            for key in ("pageId", "componentId", "groupId"):
                if isinstance(value.get(key), str):
                    context[key] = value[key]
            for key, item in value.items():
                visit(item, path + (key,), context, value)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, path + (index,), owner, value)
        elif isinstance(value, str):
            binding = (path and path[-1] == "datasetId" and isinstance(parent, dict)
                       and parent.get("datasetType") == 2)
            ref = {"path": pointer(path), "owner": owner or {},
                   "kind": "dataset_binding" if binding else "unclassified_exact_reference"}
            if value in known:
                references[value].append(ref)
            elif binding:
                dangling.append({"blend_id": value, **ref})

    visit(config)
    blends = []
    for blend_id, entry in known.items():
        definition = entry["value"]
        block_source = definition.get("blockDatasource")
        inputs, joins, issues = [], [], []

        def tree(node, path):
            if not isinstance(node, dict) or sum(key in node for key in ("query", "join", "union")) != 1:
                issues.append({"path": pointer(path), "kind": "unrecognized_tree_node"})
                return
            if "query" in node:
                query = node["query"]
                if not isinstance(query, dict):
                    issues.append({"path": pointer(path), "kind": "malformed_query"})
                    return
                source_id = query.get("datasourceId")
                filters = query.get("filterConfig", {})
                if (not isinstance(filters, dict)
                        or not isinstance(filters.get("filterIds", []), list)
                        or any(not isinstance(value, str) or not value
                               for value in filters.get("filterIds", []))):
                    issues.append({"path": pointer(path + ("query", "filterConfig")),
                                   "kind": "malformed_filter_config"})
                item = {"path": pointer(path + ("query",)), "datasource_id": source_id,
                        "display_name": query.get("displayName"),
                        "datasource_attached": isinstance(source_id, str) and source_id in attached,
                        "concept_count": len(query["concepts"]) if isinstance(query.get("concepts"), list) else None,
                        "filter_ids": copy.deepcopy(filters.get("filterIds", [])) if isinstance(filters, dict) else None,
                        "has_date_range_dimension": "dateRangeDimension" in query}
                if not isinstance(source_id, str) or not source_id:
                    issues.append({"path": item["path"], "kind": "missing_query_datasource"})
                if include_definitions:
                    item["query"] = copy.deepcopy(query)
                inputs.append(item)
                return
            kind = "union" if "union" in node else "join"
            join = node[kind]
            if not isinstance(join, dict):
                issues.append({"path": pointer(path), "kind": "malformed_" + kind})
                return
            merge = {"path": pointer(path + (kind,)), "node_kind": kind,
                     "type_code": 5 if kind == "union" else join.get("type"),
                     "condition": copy.deepcopy(join.get("condition"))}
            if kind == "union":
                merge["union_key_pairs"] = copy.deepcopy(join.get("unionKeyPair"))
            joins.append(merge)
            for side in ("left", "right"):
                tree(join.get(side), path + (kind, side))

        if not isinstance(block_source, dict):
            issues.append({"path": "/blockDatasource", "kind": "unrecognized_block_datasource"})
            block_source = {}
        blocks = block_source.get("blocks")
        if not isinstance(blocks, list):
            issues.append({"path": "/blockDatasource/blocks", "kind": "malformed_blocks"})
            blocks = []
        for index, block in enumerate(blocks):
            path = ("blockDatasource", "blocks", index)
            if not isinstance(block, dict) or "treeQueryBlockConfig" not in block:
                issues.append({"path": pointer(path), "kind": "unrecognized_block"})
                continue
            tree(block["treeQueryBlockConfig"], path + ("treeQueryBlockConfig",))
        output = block_source.get("datasourceBlock")
        if not isinstance(output, dict):
            issues.append({"path": "/blockDatasource/datasourceBlock", "kind": "malformed_output_block"})
            output = {}
        item = {"id": blend_id, "name": output.get("name"), "fingerprint": fingerprint(entry),
                "output_field_count": len(output["fields"]) if isinstance(output.get("fields"), list) else None,
                "inputs": inputs, "joins": joins, "issues": issues,
                "references": references[blend_id], "has_no_exact_references": not references[blend_id]}
        if include_definitions:
            item["definition"] = copy.deepcopy(definition)
        blends.append(item)
    return {"blends": blends, "report_content_fingerprint": fingerprint(report),
            "resource_fingerprint": fingerprint(resource), "dangling_dataset_references": dangling,
            "scope": "Saved data-view registry, including single-query views. Input/join paths are "
                     "relative to each value; references are absolute report-config paths. "
                     "Unknown shapes preserved in blend_get, not inferred valid or unused."}


class Blends:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def join_types(self) -> dict:
        page = await self.studio.session.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(native_script(_JOIN_TYPES), {"reportId": self.studio.config.report_id}),
                timeout=self.studio.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError("NATIVE_CONTRACT_FAILED", "Native blend join catalog unavailable") from exc
        if result.get("error"):
            raise StudioError(result["error"], "Native blend join catalog refused")
        return result

    async def validate(self, blend_id: str, *, rebuild_output_schema: bool = False) -> dict:
        before = (await self.studio.report())["reportConfig"]
        inventory = blend_inventory(before, set(), include_definitions=True)
        item = next((b for b in inventory["blends"] if b["id"] == blend_id), None)
        if item is None:
            raise StudioError("NOT_FOUND", "Data-view resource not found in this report")
        return await self.validate_candidate(
            before, blend_id, item["definition"], rebuild_output_schema=rebuild_output_schema
        )

    async def validate_candidate(
        self, before: dict, blend_id: str, candidate: dict, *, rebuild_output_schema: bool = False,
        new: bool = False,
    ) -> dict:
        inventory = blend_inventory(before, set(), include_definitions=True)
        item = next((b for b in inventory["blends"] if b["id"] == blend_id), None)
        if item is None and not new:
            raise StudioError("NOT_FOUND", "Data-view resource not found in this report")
        sources = {}
        if rebuild_output_schema:
            candidate_inventory = blend_inventory({"report": {"resource": {"dataViewResource": {
                "entry": [{"key": blend_id, "value": candidate}]}}}, "page": []}, set())
            for query in candidate_inventory["blends"][0]["inputs"]:
                source_id = query["datasource_id"]
                if not isinstance(source_id, str) or not source_id:
                    raise StudioError("INVALID_BLEND", "Input datasource missing for schema rebuild")
                if source_id not in sources:
                    source = await self.studio.datasource(source_id)
                    if fingerprint(source["draft"]) != fingerprint(source["published"]):
                        raise StudioError("UNPUBLISHED_DATASOURCE", "Matching draft/published required")
                    sources[source_id] = fingerprint(source)
        page = await self.studio.session.page()
        try:
            result = await asyncio.wait_for(page.evaluate(native_script(_VALIDATE_BLEND), {
                "reportId": self.studio.config.report_id, "version": str(before["version"]),
                "blendId": blend_id, "definition": None if new else item["definition"],
                "candidate": candidate,
                "rebuildOutputSchema": rebuild_output_schema,
            }), timeout=self.studio.config.timeout_ms / 1000)
        except Exception as exc:
            raise StudioError("NATIVE_CONTRACT_FAILED", "Native blend validation unavailable") from exc
        if result.get("error"):
            raise StudioError(result["error"], "Native blend validation refused")
        for source_id, token in sources.items():
            if fingerprint(await self.studio.datasource(source_id)) != token:
                raise StudioError("VERSION_CONFLICT", "Source changed during output schema rebuild")
        if fingerprint((await self.studio.report())["reportConfig"]) != fingerprint(before):
            raise StudioError("VERSION_CONFLICT", "Report changed during blend validation")
        return {"blend_id": blend_id, "report_content_fingerprint": inventory["report_content_fingerprint"],
                **result}

    async def inventory(self, blend_id: str | None = None) -> dict:
        config = (await self.studio.report())["reportConfig"]
        attached = {source["id"] for source in await self.studio.sources()}
        result = blend_inventory(config, attached, include_definitions=blend_id is not None)
        if blend_id is None:
            return result
        for item in result["blends"]:
            if item["id"] == blend_id:
                return {"blend": item, "report_content_fingerprint": result["report_content_fingerprint"],
                        "scope": result["scope"]}
        raise StudioError("NOT_FOUND", "Data-view resource not found in this report")

    async def rename(
        self, changes: list[BlendRename], expected: str, *, dry_run: bool = True
    ) -> dict:
        ids = [change.blend_id for change in changes]
        if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct data-view IDs")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = blend_inventory(before, set())
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            known = {item["id"]: item for item in inventory["blends"]}
            if any(blend_id not in known for blend_id in ids):
                raise StudioError("NOT_FOUND", "Data-view resource not found in this report")
            target = report_content(before)
            entries = {entry["key"]: entry for entry in target["resource"]["dataViewResource"]["entry"]}
            plans = []
            for change in changes:
                value = entries[change.blend_id]["value"]
                source = value.get("blockDatasource")
                output = source.get("datasourceBlock") if isinstance(source, dict) else None
                if not isinstance(output, dict):
                    raise StudioError("INVALID_BLEND", "Data-view has no output datasource block")
                plans.append({"blend_id": change.blend_id, "before": output.get("name"),
                              "after": change.name, "references": known[change.blend_id]["references"]})
                output["name"] = change.name
            plan = {"changes": plans, "atomic_scope": "report_resources"}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await ReportResources(self.studio).write(before, target), **plan}

    async def rename_inputs(
        self, changes: list[BlendInputRename], expected: str, *, dry_run: bool = True
    ) -> dict:
        keys = [(c.blend_id, c.input_path) for c in changes]
        if not 1 <= len(keys) <= 100 or len(set(keys)) != len(keys):
            raise StudioError("INVALID_BATCH", "Provide 1-100 distinct blend inputs")
        async with self.studio.mutation_lock():
            before = (await self.studio.report())["reportConfig"]
            inventory = blend_inventory(before, set())
            if inventory["report_content_fingerprint"] != expected:
                raise StudioError("VERSION_CONFLICT", "Report resources/content changed")
            known = {b["id"]: b for b in inventory["blends"]}
            target = report_content(before)
            entries = {e["key"]: e["value"] for e in
                       target.get("resource", {}).get("dataViewResource", {}).get("entry", [])}
            plans = []
            for change in changes:
                item = known.get(change.blend_id)
                if item is None or change.input_path not in {q["path"] for q in item["inputs"]}:
                    raise StudioError("NOT_FOUND", "Use an input path from blends_list")
                query = entries[change.blend_id]
                for token in tokens(change.input_path):
                    query = query[int(token)] if isinstance(query, list) else query[token]
                old_name = query.get("displayName")
                if change.name:
                    query["displayName"] = change.name
                else:
                    query.pop("displayName", None)
                plans.append({"blend_id": change.blend_id, "input_path": change.input_path,
                              "before": old_name, "after": query.get("displayName")})
            plan = {"changes": plans, "atomic_scope": "report_resources",
                    "scope": "Input table labels only; no source/concept/field IDs are renamed"}
            if dry_run:
                return {"status": "planned", **plan}
            return {**await ReportResources(self.studio).write(before, target), **plan}


class BlendInputRename(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    blend_id: str = Field(min_length=1)
    input_path: str = Field(min_length=1)
    name: str = Field(max_length=100)

    @field_validator("name")
    @classmethod
    def native_length(cls, value: str) -> str:
        try:
            size = len(value.encode("utf-16-le")) // 2
        except UnicodeEncodeError as exc:
            raise ValueError("Input name contains invalid Unicode") from exc
        if size > 100:
            raise ValueError("Native input names are limited to 100 UTF-16 code units")
        return value


class BlendRename(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    blend_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=1000)


_JOIN_TYPES = """({reportId}) => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService'))return {error:'EDITOR_NOT_READY'};
    if(i.get('reportContextService').getReport()?.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    const constructors=R.candidates('joinTypeSelector');
    if(constructors.length!==1)return {error:'BLEND_CONTRACT_CHANGED'};
    // Detached selector: constructor creates local UI state only. No save/setter call.
    const selector=new constructors[0](null);
    const type=selector.JoinType;
    const names=['INNER_JOIN','FULL_OUTER_JOIN','LEFT_OUTER_JOIN','RIGHT_OUTER_JOIN','CROSS_JOIN'];
    if(!type||names.some(name=>!Number.isInteger(type[name])||type[type[name]]!==name))
        return {error:'BLEND_CONTRACT_CHANGED'};
    const unionCodes=Object.keys(type).filter(key=>/^[0-9]+$/.test(key)&&type[key]==='UNION');
    const unions=R.unionsEnabled(selector);
    if(unionCodes.length!==1||typeof unions!=='boolean')return {error:'BLEND_CONTRACT_CHANGED'};
    return {types:[...names.map(name=>({name,code:type[name],available:true,node_kind:'join'})),
        {name:'UNION',code:Number(unionCodes[0]),available:unions,node_kind:'union'}],
        code_space:'native_runtime_merge_type_not_saved_wire_type',
        scope:'Native merge selector enum and current Union feature flag. '
            +'Codes are runtime values, not values to copy into saved join.type. '
            +'Availability is not proof of a valid configuration, write support or query execution.'};
}"""


_VALIDATE_BLEND = """({reportId,version,blendId,definition,candidate=definition,rebuildOutputSchema=false}) => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService'))return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport();
    if(report?.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    if(String(report.getVersion())!==version)return {error:'EDITOR_STALE'};
    if(Object.values(report.isChanged||{}).some(Boolean))return {error:'EDITOR_DIRTY'};
    const equal=(a,b)=>{
        if(a===b)return true;
        if(!a||!b||typeof a!=='object'||typeof b!=='object'||Array.isArray(a)!==Array.isArray(b))return false;
        const keys=Object.keys(a);
        return keys.length===Object.keys(b).length&&keys.every(k=>Object.hasOwn(b,k)&&equal(a[k],b[k]));
    };
    if(definition!==null&&!equal(report.getResourceById('dataViewResource',blendId),definition))
        return {error:'EDITOR_STALE'};
    const constructors=R.candidates('blendModel');
    const validators=R.candidates('blendValidator');
    if(constructors.length!==1||validators.length!==1)return {error:'BLEND_CONTRACT_CHANGED'};
    // Native parser expects exactly one left-deep tree, with query-only right inputs.
    const blocks=candidate.blockDatasource?.blocks;
    if(!Array.isArray(blocks)||blocks.length!==1)return {error:'UNSUPPORTED_BLEND_SHAPE'};
    let node=blocks[0].treeQueryBlockConfig;
    while(node){
        const kinds=['query','join','union'].filter(k=>Object.hasOwn(node,k));
        if(kinds.length!==1)return {error:'UNSUPPORTED_BLEND_SHAPE'};
        if(kinds[0]==='query'){if(!node.query||typeof node.query!=='object')return {error:'UNSUPPORTED_BLEND_SHAPE'};break;}
        const merge=node[kinds[0]];
        if(!merge?.right?.query||merge.right.join||merge.right.union||!merge.left)
            return {error:'UNSUPPORTED_BLEND_SHAPE'};
        node=merge.left;
    }
    if(!node)return {error:'UNSUPPORTED_BLEND_SHAPE'};
    const model=new constructors[0](structuredClone(candidate),'');
    const tree=R.blendTree(model);
    const issues=validators[0](tree);
    if(!Array.isArray(issues))return {error:'BLEND_CONTRACT_CHANGED'};
    if(rebuildOutputSchema){
        if(!i.has('blendSchemaService'))return {error:'SCHEMA_NOT_READY'};
        const schemaService=i.get('blendSchemaService');
        const builders=R.candidates('blendOutputSchemaBuilder');
        if(builders.length!==1)return {error:'BLEND_CONTRACT_CHANGED'};
        for(const query of tree.queries){
            const schema=R.metadataHolder(schemaService).getMeta(query.datasourceId)?.getSchema();
            if(!schema||schema.isDummy())return {error:'SCHEMA_NOT_READY'};
            for(const concept of query.concepts){
                const data=concept.queryTimeTransformation?.dataTransformation;
                if(data?.sourceType===1)continue;
                const field=data?.sourceFieldName&&R.schemaField(schema,data.sourceFieldName);
                if(!field||field.isDummy?.())return {error:'FIELD_NOT_FOUND'};
            }
        }
        builders[0](schemaService,model);
    }
    const serialized=R.serializeBlend(model);
    const escape=k=>String(k).replaceAll('~','~0').replaceAll('/','~1');
    const diffs=(a,b,path='')=>{
        if(equal(a,b))return [];
        if(!a||!b||typeof a!=='object'||typeof b!=='object'||Array.isArray(a)!==Array.isArray(b))return [path];
        return [...new Set([...Object.keys(a),...Object.keys(b)])].flatMap(k=>diffs(a[k],b[k],path+'/'+escape(k)));
    };
    return {structurally_valid:issues.length===0,issues,
        runtime_joins:structuredClone(tree.joins),
        input_datasource_ids:tree.queries.map(q=>q.datasourceId),
        normalization_paths:diffs(candidate,serialized),
        ...(rebuildOutputSchema?{rebuilt_output_fields:serialized.blockDatasource.datasourceBlock.fields,
            output_schema_rebuilt:true}:{}),
        scope:'Detached native structural validation only. No source access/type validation, '
            +'feature availability, query execution or publication is inferred. Serialization is not saved.'};
}"""
