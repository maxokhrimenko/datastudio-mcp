"""Validate expressions without saving chart or datasource fields."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .browser import StudioError
from .bundle import native_script
from .document import fingerprint, json_equal

if TYPE_CHECKING:
    from .service import Studio


class FormulaOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field_id: str = Field(min_length=1)
    expression: str = Field(min_length=1, max_length=100_000)


def validation_result(response: dict[str, Any]) -> dict[str, Any]:
    column = response.get("column")
    diagnostics = {
        key: response[key]
        for key in ("errorStatus", "errorMessage", "status", "connectorErrorStatus")
        if response.get(key)
    }
    if isinstance(column, dict) and not diagnostics:
        if type(column.get("dataType")) is not int or column.get("enabled") is not True:
            raise StudioError(
                "INVALID_RESPONSE", "Formula validation returned incomplete column metadata"
            )
        return {"status": "valid", "valid": True, "column": column}
    if diagnostics:
        # Connector/authorization errors are not proof of invalid syntax. Preserve
        # the actual diagnostics instead of assigning a guessed error category.
        return {"status": "validation_failed", "valid": False, "diagnostics": diagnostics}
    raise StudioError("INVALID_RESPONSE", "Formula validation returned neither a column nor errors")


def mask_formula_comments(expression: str) -> str:
    """Mask GoogleSQL comments, preserving quotes and UTF-16 source offsets.

    This is not syntax validation. The original expression must first pass
    Google's validator. Quoted identifiers and single/triple string delimiters
    follow the GoogleSQL lexical contract; unknown AST nodes still fail partial.
    """
    chunks = []
    index = 0
    while index < len(expression):
        start = index
        char = expression[index]
        if char in "\"'`":
            delimiter = char * 3 if char != "`" and expression.startswith(char * 3, index) else char
            index += len(delimiter)
            while index < len(expression):
                if expression[index] == "\\":
                    index += 2
                elif expression.startswith(delimiter, index):
                    index += len(delimiter)
                    break
                else:
                    index += 1
            chunks.append(expression[start:index])
        elif expression.startswith("/*", index):
            end = expression.find("*/", index + 2)
            index = len(expression) if end < 0 else end + 2
            chunks.append(
                "".join(
                    c if c in "\r\n" else " " * (2 if ord(c) > 0xFFFF else 1)
                    for c in expression[start:index]
                )
            )
        elif char == "#" or expression.startswith("--", index):
            while index < len(expression) and expression[index] not in "\r\n":
                index += 1
            chunks.append(
                "".join(" " * (2 if ord(c) > 0xFFFF else 1) for c in expression[start:index])
            )
        else:
            chunks.append(char)
            index += 1
    return "".join(chunks)


def reference_identifiers(fields: list[dict[str, Any]]) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    folded: dict[str, str] = {}
    for key, kind in (("field", "datasource"), ("immutableVersion", "connector")):
        for field in fields:
            identity = field.get(key, {})
            if key == "immutableVersion" and not identity:
                continue
            if not identity.get("ns") or not identity.get("name"):
                raise StudioError("INVALID_RESPONSE", "Source field identity is missing")
            name = f"{identity['ns']}.{identity['name']}"
            prior = folded.get(name.lower())
            if prior is not None:
                if key == "immutableVersion" and prior == name and identifiers[prior] == kind:
                    continue  # Several exposed fields can map to one connector column.
                raise StudioError("AMBIGUOUS_FIELDS", "Reference identities overlap across layers")
            identifiers[name] = kind
            folded[name.lower()] = name
    return identifiers


def dependency_summary(graph: dict[str, list[str]]) -> dict[str, Any]:
    """Reverse edges and one concrete cycle, without recursion-depth limits."""
    reverse: dict[str, list[str]] = {name: [] for name in graph}
    for owner, dependencies in graph.items():
        for dependency in sorted(set(dependencies)):
            if dependency not in graph:
                raise StudioError("UNKNOWN_DEPENDENCY", "Reference is absent from source inventory")
            reverse[dependency].append(owner)
    done: set[str] = set()
    for start in graph:
        if start in done:
            continue
        path = [start]
        active = {start: 0}
        stack = [iter(graph[start])]
        while stack:
            target = next(stack[-1], None)
            if target is None:
                finished = path.pop()
                done.add(finished)
                del active[finished]
                stack.pop()
            elif target in active:
                return {"dependents": reverse, "cycle": path[active[target] :] + [target]}
            elif target not in done:
                active[target] = len(path)
                path.append(target)
                stack.append(iter(graph[target]))
    return {"dependents": reverse, "cycle": None}


class Formulas:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def dependencies(
        self, datasource_id: str, overrides: list[FormulaOverride] | None = None
    ) -> dict[str, Any]:
        overrides = overrides or []
        replacements = {item.field_id: item.expression for item in overrides}
        if len(overrides) > 100 or len(replacements) != len(overrides):
            raise StudioError("INVALID_BATCH", "Provide at most 100 distinct formula overrides")
        source = await self.studio.datasource(datasource_id)
        if not json_equal(source["draft"], source["published"]):
            raise StudioError(
                "UNPUBLISHED_SOURCE_CHANGES", "Draft-aware validation is not available"
            )
        token = fingerprint(source["draft"])
        fields = source["draft"].get("datasourceBlock", {}).get("fields")
        if not isinstance(fields, list):
            raise StudioError("INVALID_RESPONSE", "Datasource fields are missing")
        graph: dict[str, list[str]] = {}
        for field in fields:
            identity = field.get("field", {})
            if not identity.get("ns") or not identity.get("name"):
                raise StudioError("INVALID_RESPONSE", "Datasource field identity is missing")
            name = f"{identity['ns']}.{identity['name']}"
            if name.lower() in {key.lower() for key in graph}:
                raise StudioError("AMBIGUOUS_FIELDS", "Datasource field identities overlap")
            graph[name] = []
            if name in replacements and (
                field.get("createdBy") != 2 or not field.get("textFormula")
            ):
                raise StudioError(
                    "NOT_CALCULATED_FIELD", "Overrides require existing calculated fields"
                )
        if not replacements.keys() <= graph.keys():
            raise StudioError("FIELD_NOT_FOUND", "Override field is absent from datasource")
        diagnostics = []
        connector_fields = {}
        formula_connector_references = {}
        for field in fields:
            expression = field.get("textFormula")
            if not expression:
                continue
            identity = field["field"]
            name = f"{identity['ns']}.{identity['name']}"
            expression = replacements.get(name, expression)
            physical = field.get("immutableVersion", {})
            physical_id = f"{physical.get('ns', '')}.{physical.get('name', '')}"
            if field.get("createdBy") == 1 and physical.get("name") and expression == physical_id:
                # Connector projection is a different namespace layer. Its raw
                # name is not a calculated-field reference in datasourceBlock.
                connector_fields[name] = physical_id
                continue
            result = await self.references(datasource_id, expression)
            if result["source_draft_fingerprint"] != token:
                raise StudioError("VERSION_CONFLICT", "Datasource changed during graph inspection")
            if not result["valid"] or result["status"] == "partial":
                diagnostics.append({"field_id": name, "validation": result})
                continue
            graph[name] = sorted({item["field_id"] for item in result["references"]})
            if result["connector_references"]:
                formula_connector_references[name] = sorted(
                    {item["field_id"] for item in result["connector_references"]}
                )
        fresh = await self.studio.datasource(datasource_id)
        if fingerprint(fresh["draft"]) != token or not json_equal(
            fresh["published"], source["published"]
        ):
            raise StudioError("VERSION_CONFLICT", "Datasource changed during graph inspection")
        return {
            "status": "partial" if diagnostics else "inspected",
            "datasource_id": datasource_id,
            "source_draft_fingerprint": token,
            "graph_mode": "proposed" if replacements else "current",
            "overrides": replacements,
            "validation_scope": "current schema; not proposed graph type propagation",
            "dependencies": graph,
            "connector_fields": connector_fields,
            "formula_connector_references": formula_connector_references,
            **dependency_summary(graph),
            "diagnostics": diagnostics,
            "scope": "datasource formula field references; not report consumers or parameters",
            "deletion_safe": False,
        }

    async def references(self, datasource_id: str, expression: str) -> dict[str, Any]:
        validation = await self.validate(datasource_id, expression)
        if not validation["valid"]:
            return validation
        source = await self.studio.datasource(datasource_id)
        token = validation["source_draft_fingerprint"]
        if fingerprint(source["draft"]) != token:
            raise StudioError("VERSION_CONFLICT", "Datasource changed before parsing")
        fields = source["draft"].get("datasourceBlock", {}).get("fields")
        if not isinstance(fields, list):
            raise StudioError("INVALID_RESPONSE", "Datasource fields are missing")
        identifiers = reference_identifiers(fields)
        session = self.studio.session
        page = await session.page()
        try:
            parsed = await asyncio.wait_for(
                page.evaluate(
                    native_script("""({expression,identifiers}) => {
                const trie=new (R.formulaTrie())(), tokensFn=R.formulaTokensKey();
                for(const name of Object.keys(identifiers))trie.add(name.toLowerCase(),
                    {name,displayName:name,fieldConfig:{isMetric:false}});
                const root=R.formulaParse(expression,trie), stack=[root], references=[], unparsed=[];
                let count=0;
                while(stack.length){
                    const node=stack.pop();if(!node)continue;
                    if(++count>100000)return {error:'FORMULA_AST_LIMIT'};
                    if(typeof node.getChildren!=='function')
                        return {error:'FORMULA_AST_CONTRACT_CHANGED'};
                    const children=node.getChildren();
                    if(node.suggestion){
                        const name=node.suggestion.name;
                        if(!Object.hasOwn(identifiers,name))
                            return {error:'FORMULA_UNKNOWN_REFERENCE'};
                        references.push({field_id:name,kind:identifiers[name],position:node.getPosition()});
                    }else if(children.length===0){
                        // tokensFn is the leaf token method, calibrated by the resolver
                        // because its minified name changes with every bundle build.
                        if(typeof node[tokensFn]!=='function'
                           ||typeof node.getBackEndTextFormula!=='function')
                            return {error:'FORMULA_AST_CONTRACT_CHANGED'};
                        const text=node.getBackEndTextFormula();
                        // Date-part / interval-unit keywords (the MONTH in
                        // DATETIME_TRUNC(x, MONTH) or INTERVAL 1 MONTH) are their own leaf
                        // class and legitimately resolve to zero tokens, unlike CASE/WHEN
                        // which resolve to one. Treating them as unparsed text voided the
                        // reference list of every formula using them, which in turn blocked
                        // metadata edits on the whole datasource. They are reserved words in
                        // this grammar and can never denote a field: a field reference is
                        // always namespace-qualified (t0.<name>).
                        const DATE_PARTS=new Set(['YEAR','QUARTER','MONTH','WEEK','ISOWEEK',
                            'ISOYEAR','DAY','DAYOFWEEK','DAYOFYEAR','HOUR','MINUTE','SECOND',
                            'MILLISECOND','MICROSECOND','DATE','DATETIME','TIME']);
                        // A U+E000-prefixed leaf is a parameter reference (the editor's
                        // own marker), never a datasource field.
                        if(text.trim()&&node[tokensFn]().length===0
                           &&!DATE_PARTS.has(text.trim().toUpperCase())
                           &&!text.trim().startsWith('\ue000'))
                            unparsed.push({text,position:node.getPosition()});
                    }
                    stack.push(...children.slice().reverse());
                }
                return {references,unparsed};
                }"""),
                    {"expression": mask_formula_comments(expression), "identifiers": identifiers},
                ),
                timeout=session.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError("NATIVE_CONTRACT_FAILED", "Cannot parse formula references") from exc
        if parsed.get("error"):
            raise StudioError(parsed["error"], "Native formula reference parsing failed")
        if not isinstance(parsed.get("references"), list) or not isinstance(
            parsed.get("unparsed"), list
        ):
            raise StudioError("INVALID_RESPONSE", "Formula references are missing")
        fresh = await self.studio.datasource(datasource_id)
        if fingerprint(fresh["draft"]) != token:
            raise StudioError("VERSION_CONFLICT", "Datasource changed during parsing")
        return {
            **validation,
            "status": "partial" if parsed["unparsed"] else "valid",
            "references": None
            if parsed["unparsed"]
            else [item for item in parsed["references"] if item["kind"] == "datasource"],
            "connector_references": None
            if parsed["unparsed"]
            else [item for item in parsed["references"] if item["kind"] == "connector"],
            "reference_candidates": parsed["references"] if parsed["unparsed"] else [],
            "unparsed": parsed["unparsed"],
            "scope": "native AST field occurrences; not transitive dependencies or cycle proof",
            "position_units": "UTF-16 code units",
            "parameters": "not characterized; this inventory covers datasource fields only",
        }

    async def validate(self, datasource_id: str, expression: str) -> dict[str, Any]:
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 100_000:
            raise StudioError(
                "INVALID_FORMULA", "Provide an expression between 1 and 100000 characters"
            )
        source = await self.studio.datasource(datasource_id)
        source_token = fingerprint(source["draft"])
        session = self.studio.session
        page = await session.page()
        try:
            request = await asyncio.wait_for(
                page.evaluate(
                    native_script("""({datasourceId,expression}) => {
                    const builders=R.candidates('formulaRequestBuilder');
                    if(builders.length!==1)return {error:'FORMULA_BUILDER_CONTRACT_CHANGED'};
                    // The native builder's no-cached-metadata fallback is revision 0.
                    // Blends require an actual blend wrapper and are separate scope.
                    const dataRequest=builders[0](R.formulaBuilderContext(builders[0]),
                        expression,datasourceId);
                    return {dataRequest,
                        detectDatatype:true,allowAutoDetectInFormula:false};
                }"""),
                    {"datasourceId": datasource_id, "expression": expression},
                ),
                timeout=session.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError(
                "NATIVE_CONTRACT_FAILED", "Cannot build formula validation request"
            ) from exc
        if request.get("error"):
            raise StudioError(request["error"], "Live formula builder is unavailable")
        result = validation_result(await session.post("validateFormula", request))
        fresh = await self.studio.datasource(datasource_id)
        if fingerprint(fresh["draft"]) != source_token:
            raise StudioError("VERSION_CONFLICT", "Datasource changed during formula validation")
        return {
            **result,
            "datasource_id": datasource_id,
            "expression": expression,
            "source_draft_fingerprint": source_token,
            "revision_mode": "native_uncached_revision_0",
            "scope": "expression validation only; not saved or evaluated for result values",
        }
