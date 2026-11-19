"""BigQuery sources: browse projects/datasets/tables and create a datasource, as Add data does.

The editor's BigQuery connector lists the account tree, reads the connector's columns
for the chosen table or custom query and creates the datasource with them; created
with a report it is embedded in (and claimed by) that report, which then names it with
a datasourceAlias. The first-party OAuth token those calls need is minted and spent
inside the page and never reaches Python, the journal or a tool result.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .browser import REQUEST_SCRIPT, StudioError
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio

IDENTIFIER = re.compile(r"^[A-Za-z0-9_\-.:]+$")


def _query_parameter(name: str, data_type: int, display_type: int) -> dict[str, Any]:
    """A connector query parameter exactly as the custom query checkboxes write it."""
    value = {"name": name, "dataType": data_type, "lock": 2, "options": [],
             "defaultValue": [{"label": name, "stringValue": ""}]}
    return {"key": name, "value": {
        "parametersModifiedByRoles": [{"key": role, "value": copy.deepcopy(value)}
                                      for role in (1, 2)],
        "parameterDisplayConfiguration": {"displayName": name, "displayType": display_type,
                                          "name": name}}}


class BigQuerySource(BaseModel):
    """A table (project_id + dataset_id + table_id) or a custom query (sql)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str = Field(min_length=1, description="Table project, or billing project for sql")
    dataset_id: str | None = None
    table_id: str | None = None
    sql: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    legacy_sql: bool = False
    billing_project_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=1000)
    date_range_params: bool = Field(
        default=False, description="Custom query: Enable date range parameters "
        "(@DS_START_DATE / @DS_END_DATE, YYYYMMDD strings from the report date range)")
    viewer_email_param: bool = Field(
        default=False, description="Custom query: Enable viewer email address parameter "
        "(@DS_USER_EMAIL)")

    @model_validator(mode="after")
    def check(self) -> BigQuerySource:
        table = self.dataset_id is not None and self.table_id is not None
        if table == (self.sql is not None):
            raise ValueError("Provide either dataset_id+table_id or sql")
        if (self.dataset_id is None) != (self.table_id is None):
            raise ValueError("dataset_id and table_id go together")
        if self.sql is None and (self.date_range_params or self.viewer_email_param):
            raise ValueError("Query parameters apply to a custom query (sql)")
        for value in (self.project_id, self.dataset_id, self.table_id, self.billing_project_id):
            if value is not None and not IDENTIFIER.fullmatch(value):
                raise ValueError(f"Invalid BigQuery identifier: {value!r}")
        return self

    def connector_config(self, project_name: str | None = None) -> dict[str, Any]:
        config: dict[str, Any] = {
            "meta": {},
            "sqlType": 1 if self.legacy_sql else 2,
            "projectId": self.project_id,
            "isPartitioned": False,
            "isSemanticGraph": False,
            "tableLabels": [],
        }
        if self.billing_project_id:
            config["billingProjectId"] = self.billing_project_id
        block: dict[str, Any] = {"datasourceType": 2, "bigQueryConnectorConfig": config,
                                 "useDatetimeType": not self.legacy_sql}
        if self.sql is not None:
            block["sql"] = self.sql
            names = [(n, 5, 6) for n in ("DS_START_DATE", "DS_END_DATE")
                     if self.date_range_params]
            if self.viewer_email_param:
                names.append(("DS_USER_EMAIL", 1, 1))
            if names:
                block["parameterConfiguration"] = {"parameters": [
                    _query_parameter(*n) for n in names]}
        else:
            config.update(datasetId=self.dataset_id, tableId=self.table_id)
            config["meta"] = {
                "projectName": project_name or self.project_id,
                "isSharedProject": False,
                "datasetName": self.dataset_id,
                "tableName": self.table_id,
            }
        return {"alias": "t0", "connectorConfig": block}

    def default_name(self) -> str:
        return self.name or self.table_id or "BigQuery custom query"


_TREE = REQUEST_SCRIPT + """
    const access = await request('getFirstPartyAccessToken', {});
    if (access.error || !access.json?.accessToken) return {error:'TOKEN_UNAVAILABLE'};
    const response = await request('getAccountTree', {datasourceType: 2,
        bigqueryOptions: args.options, googleAuthOAuthToken: access.json.accessToken});
    if (response.error) return response;
    return {status: response.status, tree: response.json?.accountTree?.bigQueryAccountTree};
}"""

_CREATE = REQUEST_SCRIPT + """
    // A source is created in a Cloud project: the report's, or for a reusable source the
    // account's own Data Studio project, which every page carries in its preload config.
    const preload = (() => {
        try {return typeof window.preload === 'string' ? JSON.parse(window.preload)
                                                         : window.preload}
        catch {return null}
    })();
    args.projectNumber = args.projectNumber || preload?.myDataStudioConfig?.projectNumber;
    if (!args.dryRun && !args.projectNumber)
        return {error: 'PROJECT_NUMBER_MISSING',
                details: 'No Data Studio project for the new datasource; nothing was created'};
    const access = await request('getFirstPartyAccessToken', {});
    if (access.error || !access.json?.accessToken) return {error:'TOKEN_UNAVAILABLE'};
    const oauth = access.json.accessToken;
    const columns = await request('getColumns', {connectorBlockConfig: args.connector,
        isBigQueryConnection: true, useThirdpartyAuth: false,
        ...(args.projectNumber ? {projectNumber: args.projectNumber} : {}),
        googleAuthOAuthToken: oauth});
    if (columns.error) return columns;
    const list = columns.json?.columns;
    if (columns.status !== 200 || !Array.isArray(list))
        return {error: 'CONNECTOR_UNAVAILABLE',
            details: `connector returned HTTP ${columns.status}; check access to the table/SQL`};
    // The connector block keeps the table's own columns (mutableVersion); the datasource
    // block gets the derived fields the editor shows, Record Count included.
    const raw = list.filter(c => c.mutableVersion), fields = list.filter(c => !c.mutableVersion);
    if (!fields.length) return {error: 'CONNECTOR_UNAVAILABLE', details: 'no fields'};
    if (args.dryRun) return {status: 200, fields: fields.map(f => ({name: f.field?.name,
        label: f.outputName, data_type: f.dataType, type: f.type,
        default_aggregation: f.defaultAggregationType}))};
    // The editor names and identifies both blocks client-side; without block IDs the
    // created source cannot be published later.
    const blockId = () => 'block_' + Array.from(crypto.getRandomValues(new Uint8Array(10)),
        b => 'abcdefghijklmnopqrstuvwxyz0123456789'[b % 36]).join('');
    const named = f => ({...f,
        field: {...f.field, simpleName: f.field?.simpleName ?? f.field?.name}});
    const body = {name: args.name, oauthType: 1, accessToken: oauth,
        ...(args.reportId ? {reportId: args.reportId} : {}),
        ...(args.projectNumber ? {uiCreateOptions: {projectNumber: args.projectNumber,
            region: 'global'}} : {}),
        datasource: {datasourceId: '', allowThirdPartyViz: true,
            allowThirdPartyVizExternalAccess: false, isAutoRefreshEnabled: false,
            requestFilters: {filterIds: []}, allowNativeFunctions: false,
            delegatedAccessEnabled: true, isUnlocked: true, isCacheable: false, resource: {},
            queryCachePolicy: {shelfLife: {amount: 12, chronoUnit: 2}, isDefaultPolicy: true},
            datasourceBlock: {id: blockId(), name: args.name, type: 1, inputBlockIds: [],
                outputBlockIds: [], fields: fields.map(named)},
            blocks: [{id: blockId(), name: 'BigQuery', type: 2, inputBlockIds: [],
                outputBlockIds: [], fields: raw, connectorBlockConfig: args.connector}]}};
    const created = await request('createBlockDatasource', body);
    // Sent: from here any failure is an unknown outcome until the account is checked.
    if (created.error) return {error: created.error, dispatched: true};
    const id = created.json?.shareable?.id || created.json?.datasource?.datasourceId;
    if (created.status !== 200 || !id) return {error: 'CREATE_OUTCOME_UNKNOWN', dispatched: true,
        details: `createBlockDatasource returned HTTP ${created.status}`};
    return {status: created.status, id, name: created.json?.shareable?.name};
}"""


class BigQuery:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def _evaluate(
        self, script: str, args: dict[str, Any], *, mutation: bool = False
    ) -> dict[str, Any]:
        session = self.studio.session
        page = await session.page()
        timeout = session.config.timeout_ms
        try:
            return await asyncio.wait_for(
                page.evaluate(script, {**args, "timeout": timeout}), timeout=timeout / 1000 * 3
            )
        except Exception as exc:
            raise StudioError(
                "TRANSPORT_FAILURE", "Browser request interrupted", outcome_unknown=mutation
            ) from exc

    async def browse(
        self, project_id: str | None = None, dataset_id: str | None = None, recent: bool = False
    ) -> dict[str, Any]:
        """Projects (no arguments), datasets of a project, or tables of a dataset."""
        for value in (project_id, dataset_id):
            if value is not None and not IDENTIFIER.fullmatch(value):
                raise StudioError("INVALID_IDENTIFIER", "Invalid BigQuery identifier")
        if dataset_id is not None and project_id is None:
            raise StudioError("INVALID_REQUEST", "dataset_id needs project_id")
        if project_id is None:
            options: dict[str, Any] = {"type": 0, "isRecent": recent}
            level = "projects"
        elif dataset_id is None:
            options = {"type": 1, "connectorConfig": {"projectId": project_id}}
            level = "datasets"
        else:
            options = {"type": 3, "connectorConfig": {"projectId": project_id,
                                                      "datasetId": dataset_id}}
            level = "tables"
        result = await self._evaluate(_TREE, {"options": options})
        if result.get("error") or result.get("status") != 200:
            raise StudioError(result.get("error") or "CONNECTOR_UNAVAILABLE",
                              "BigQuery account tree could not be read")
        tree = result.get("tree") or {}
        return {"level": level, "project_id": project_id, "dataset_id": dataset_id,
                "items": tree.get(level, [])}

    async def create(
        self, source: BigQuerySource, *, attach: bool = True, dry_run: bool = True
    ) -> dict[str, Any]:
        """Create a BigQuery datasource; embedded in the report when attach and report-scoped.

        dry_run reads the connector's fields (validating table/query access) and creates
        nothing. Creation is not undone by operation_restore: remove an embedded source
        from the report with datasources_remove_from_report.
        """
        report_scoped = self.studio.report_scoped and attach
        project_number = None
        if report_scoped:
            raw = await self.studio.report()
            project_number = raw["reportConfig"]["shareable"].get("projectNumber") or None
        connector = source.connector_config()
        args = {"connector": connector, "name": source.default_name(),
                "reportId": self.studio.config.report_id if report_scoped else None,
                "projectNumber": project_number}
        if dry_run:
            result = await self._evaluate(_CREATE, {**args, "dryRun": True})
            if result.get("error"):
                raise StudioError(result["error"], str(result.get("details", "")))
            return {"status": "planned", "name": args["name"], "embedded": report_scoped,
                    "connector": connector, "fields": result["fields"]}
        async with self.studio.mutation_lock():
            operation_id = str(uuid.uuid4())
            record = {"kind": "datasource_create", "operation_id": operation_id,
                      "report_id": self.studio.config.report_id,
                      "started_at": datetime.now(UTC).isoformat(), "state": "dispatched",
                      "connector": connector, "name": args["name"], "embedded": report_scoped}
            self.studio._journal(operation_id, record)
            try:
                result = await self._evaluate(_CREATE, {**args, "dryRun": False}, mutation=True)
            except StudioError:
                record["state"] = "outcome_unknown"
                self.studio._journal(operation_id, record)
                raise
            if result.get("error"):
                unknown = bool(result.get("dispatched"))
                record.update(state="outcome_unknown" if unknown else "failed",
                              error=result["error"])
                self.studio._journal(operation_id, record)
                raise StudioError(result["error"], str(result.get("details", "")) or (
                    "Check datasources_list(all_accessible=true) before creating again"
                    if unknown else "Nothing was created"), outcome_unknown=unknown)
            record.update(state="created", datasource_id=result["id"])
            self.studio._journal(operation_id, record)
            response = {"operation_id": operation_id, "datasource_id": result["id"],
                        "name": result.get("name"), "embedded": report_scoped}
            step = "initial_publication"
            try:
                await self._settle_new_source(result["id"])
                alias = None
                if report_scoped:
                    step = "attach"
                    alias = await self._alias(result["id"])  # claims it when not yet attached
                step = "read_back"
                source_state = await self.studio.datasource(result["id"])
            except StudioError as exc:
                # The datasource exists: report what is done and what is not, never re-create.
                record.update(state="partial", failed_step=step, error=exc.code)
                self.studio._journal(operation_id, record)
                return {"status": "partial", "failed_step": step, "error": exc.code,
                        "outcome_unknown": exc.outcome_unknown, **response}
            record.update(state="verified", alias=alias)
            self.studio._journal(operation_id, record)
            return {"status": "verified", **response, "alias": alias,
                    "fields": [{"name": f["field"]["name"], "label": f.get("outputName"),
                                "data_type": f.get("dataType")}
                               for f in source_state["draft"]["datasourceBlock"]["fields"]]}

    async def _settle_new_source(self, datasource_id: str) -> None:
        """Publish the new source's own initial draft once, as the editor's first save does.

        Without it the server rejects (HTTP 500) the first publication of an edited draft.
        A failure here is reported, never retried: the source exists, only unsettled.
        """
        raw = await self.studio.session.post("getBlockDatasource", {"datasourceId": datasource_id})
        body = {"draft": raw["draft"], "clearThumbnails": False}
        if self.studio.report_scoped:
            body["reportId"] = self.studio.config.report_id
        await self.studio.session.post("publishDatasource", body)

    async def _alias(self, datasource_id: str) -> str:
        """Name the source in the report's datasourceAlias, as the editor does; the report
        then lists it among its attached (claimed) sources."""
        raw = (await self.studio.report())["reportConfig"]
        aliases = raw["report"].get("resource", {}).get("datasourceAlias", [])
        alias = next(
            (a["alias"] for a in aliases if a.get("datasourceId") == datasource_id), None
        )
        if alias is None:
            used = {a.get("alias") for a in aliases}
            alias = next(f"ds{n}" for n in range(len(aliases), len(aliases) + 10_000)
                         if f"ds{n}" not in used)
            target = report_content(raw)
            target.setdefault("resource", {})["datasourceAlias"] = copy.deepcopy(aliases) + [
                {"datasourceId": datasource_id, "alias": alias}]
            written = await ReportResources(self.studio).write(raw, target)
            if written["status"] not in ("verified", "unchanged"):
                raise StudioError("ALIAS_NOT_VERIFIED", f"Alias write: {written['status']}")
        if datasource_id not in {s["id"] for s in await self.studio.sources()}:
            await self.studio.session.post(
                "claimDatasourceForReport",
                {"reportId": self.studio.config.report_id, "datasourceId": datasource_id},
            )
        if datasource_id not in {s["id"] for s in await self.studio.sources()}:
            raise StudioError("NOT_ATTACHED", "The report does not list the datasource as attached")
        return alias

    async def detach(self, datasource_id: str, *, dry_run: bool = True) -> dict[str, Any]:
        """Remove a datasource from the report (Resource > Manage added data sources >
        Remove), refusing while any component, filter, blend or parameter still uses it."""
        async with self.studio.mutation_lock():
            raw = (await self.studio.report())["reportConfig"]
            if datasource_id not in {s["id"] for s in await self.studio.sources()}:
                return {"status": "unchanged", "datasource_id": datasource_id}
            users = sorted(set(usages(raw, datasource_id)))
            if users:
                raise StudioError(
                    "DATASOURCE_IN_USE", f"Still used by: {', '.join(users[:20])}"
                )
            aliases = raw["report"].get("resource", {}).get("datasourceAlias", [])
            plan = {"datasource_id": datasource_id, "removes_alias": any(
                a.get("datasourceId") == datasource_id for a in aliases)}
            if dry_run:
                return {"status": "planned", **plan}
            if plan["removes_alias"]:
                target = report_content(raw)
                target["resource"]["datasourceAlias"] = [
                    a for a in aliases if a.get("datasourceId") != datasource_id]
                written = await ReportResources(self.studio).write(raw, target)
                plan["alias_operation_id"] = written.get("operation_id")
                if written["status"] not in ("verified", "unchanged"):
                    return {"status": "partial", "failed_step": "alias",
                            "alias_status": written["status"], **plan}
            record = self.studio.journal_action("datasource_detach", **plan)
            await self.studio.session.post(
                "deleteClaim",
                {"reportId": self.studio.config.report_id, "datasourceId": datasource_id},
            )
            attached = datasource_id in {s["id"] for s in await self.studio.sources()}
            # The alias write above is its own journalled operation; a claim left in place
            # after it is reported as a partial result, not hidden.
            record["state"] = "verification_failed" if attached else "verified"
            self.studio._journal(record["operation_id"], record)
            return {"status": record["state"], "operation_id": record["operation_id"],
                    "partial": attached and plan["removes_alias"], **plan}

    async def attach(self, datasource_id: str) -> dict[str, Any]:
        """Add an existing datasource (reusable, or embedded here) to the report."""
        async with self.studio.mutation_lock():
            source = await self.studio.session.post(
                "getBlockDatasource", {"datasourceId": datasource_id}
            )
            if source.get("draft", {}).get("datasourceId") != datasource_id:
                raise StudioError("NOT_FOUND", "Datasource is not readable by this account")
            alias = await self._alias(datasource_id)
            return {"status": "verified", "datasource_id": datasource_id, "alias": alias}


def usages(config: dict[str, Any], datasource_id: str) -> list[str]:
    """Where the saved report mentions a datasource, apart from its alias entry.

    A whole-content search: any component, filter, blend, parameter or override naming
    the source blocks its removal, including places this module does not model.
    """
    found = []

    def walk(components, where):
        for component in components:
            own = {k: v for k, v in component.items() if k != "componentConfig"}
            if datasource_id in json.dumps(own):
                found.append(f"{where}/{component.get('componentId')}")
            walk(component.get("componentConfig", []), where)

    walk(config["report"].get("componentConfig", []), "report")
    for page in config["page"]:
        walk(page["page"].get("componentConfig", []), page["pageId"])
    for key, value in config["report"].get("resource", {}).items():
        if key != "datasourceAlias" and datasource_id in json.dumps(value):
            found.append(f"resource:{key}")
    rest = {k: v for k, v in config["report"].items() if k not in ("resource", "componentConfig")}
    if datasource_id in json.dumps(rest):
        found.append("report")
    return found
