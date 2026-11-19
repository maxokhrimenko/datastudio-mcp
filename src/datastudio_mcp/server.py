"""MCP transport: typed tools delegate to the report operation service."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import StrictBool

from . import __version__
from .audit import Audit
from .bigquery import BigQuerySource
from .blend_create import NewBlend
from .blend_filters import BlendInputFilters
from .blend_joins import BlendJoinChange
from .blends import BlendInputRename, BlendRename
from .browser import (
    REPORT_ID,
    Browser,
    BrowserConfig,
    StudioError,
    parse_datasource_reference,
    parse_report_reference,
)
from .bundle import NativeContracts
from .component_filters import ComponentFilterChange, FilterBinding
from .components import Arrangement, ComponentGeometry, GroupMove, LayerAction, NewGroup
from .creation import NewComponent
from .datasources import (
    CalculatedField,
    CalculatedFieldEdit,
    DatasourceParameter,
    FieldMetadataEdit,
    FieldRename,
)
from .document import Patch, find_page, fingerprint
from .field_overrides import FieldOverride, FieldRef
from .fields import FieldSelection
from .filter_conditions import FilterNode
from .filters import FilterConditionChange, FilterRename, NewFilter
from .formulas import FormulaOverride
from .sharing import SharingMember
from .group_filters import GroupFilterChange
from .opening import list_reports, open_report
from .page_import import PageImport, ThemeCopy
from .pages import NewPage
from .panels import ComponentSettingChanges, SettingChange, Target
from .patches import DimensionColor
from .runtime_controls import (
    RuntimeDateRange,
    RuntimeFilterValue,
    RuntimeParameterValue,
)
from .service import ComponentChange
from .switching import TypeChange
from .variables import ReportVariable
from .workspace import Workspace


def journal_record(state_dir: Path, operation_id: str) -> dict[str, Any]:
    """Read a journal record by canonical UUID; it names the report it belongs to."""
    try:
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError("Noncanonical ID")
        record = json.loads((state_dir / f"{operation_id}.json").read_text())
    except (ValueError, OSError) as exc:
        raise ToolError("NOT_FOUND: Operation journal not found") from exc
    if not isinstance(record, dict) or not REPORT_ID.fullmatch(str(record.get("report_id", ""))):
        raise ToolError("INVALID_JOURNAL: Operation journal has no valid report_id")
    return record


def native_cause(exc: StudioError) -> str:
    """First line of the page-side error behind a native failure, if any.

    Native failures wrap a browser exception whose message names the broken
    contract (NATIVE_CONTRACT_CHANGED[schema.field]: 0 candidates) or the JS error.
    Page messages carry code identifiers, not session data; they are still bounded.
    """
    cause = exc.__cause__
    if cause is None or isinstance(cause, StudioError):
        return ""
    line = next((part.strip() for part in str(cause).splitlines() if part.strip()), "")
    return f" (cause: {line[:300]})" if line else ""


def create_server(config: BrowserConfig, *, browser: Browser | None = None) -> FastMCP:
    browser = browser or Browser(config)
    workspace = Workspace(browser)

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        idle = asyncio.create_task(browser.close_when_idle(workspace.tool_lock))
        try:
            yield {}
        finally:
            idle.cancel()
            await browser.close()

    mcp = FastMCP(
        "Data Studio",
        version=__version__,
        lifespan=lifespan,
        instructions=(
            "Start with report_open(<report URL or UUID>): it starts the MCP's own Chrome "
            "profile in the background (headless, no window) if needed, opens or reuses the "
            "report tab and makes that report the default. Only when Google needs a sign-in "
            "does a Chrome window appear: report_open returns login_required; ask the user to "
            "sign in in that window, then call report_open again (the window closes and work "
            "continues in the background). reports_list shows already open reports. "
            "Each user signs in with their own Google account; no author account is needed. "
            "For multiple Google accounts, pass the report URL with its account selector. "
            "Every other tool works on the active report unless report_id is given. "
            "Read current entities before planning edits. Use dry_run for preview and the "
            "page fingerprint for conflict protection. Mutations are not automatically "
            "retried. A result other than verified/unchanged requires inspection before "
            "continuing. Writes go to the report draft; publish with report_publish when the "
            "report uses manual publishing. Tabs, Edit/View mode and draft loading are "
            "managed automatically, including reloading the model after writes; "
            "page_screenshot shows a page as viewers will see it. Datasource tools "
            "need no report: give a datasource UUID or URL; a source attached to the active "
            "report is checked against that report, any other one is edited on its own "
            "through a Data Studio home tab, with no report consumers scanned."
        ),
    )

    async def guarded(action):
        try:
            return await action()
        except StudioError as exc:
            raise ToolError(
                f"{exc.code}: {exc}{native_cause(exc)}; outcome_unknown={exc.outcome_unknown}"
            ) from exc

    async def call(report_id: str | None, action):
        async def run():
            # One report tool at a time: tab navigation, the live draft model and writes
            # are shared by every call, so concurrent calls must never interleave.
            async with workspace.tool_lock:
                bundle = workspace.resolve(report_id)
                session = bundle.studio.session
                # Whatever tab/mode/version the user left, bring the draft View in first.
                await session.acquire()
                dispatched = session.dispatches
                try:
                    return await action(bundle)
                except StudioError as exc:
                    # A loaded model older than the saved report: reload and run once more,
                    # but only if this call sent no write (a rerun must never replay one).
                    if (exc.code != "EDITOR_STALE" or exc.outcome_unknown
                            or session.dispatches != dispatched):
                        raise
                    await session.acquire(reload=True)
                    return await action(bundle)

        return await guarded(run)

    async def source_call(datasource_id: str, report_id: str | None, action):
        """Run a datasource tool in the narrowest scope that knows the source.

        An explicit report_id wins. Otherwise the active report is used when the source is
        attached to it, so its saved references are checked; any other source is worked on
        in the datasource scope, without a report.
        """

        async def in_report(bundle, source_id):
            # As call(): one tool at a time, and the report tab in the draft View with
            # the editor bundle loaded (formula parsing needs it).
            await bundle.studio.session.acquire()
            return await action(bundle, source_id)

        async def run():
            source_id = parse_datasource_reference(datasource_id)
            async with workspace.tool_lock:
                if report_id is not None:
                    return await in_report(workspace.resolve(report_id), source_id)
                if workspace.active is not None:
                    bundle = workspace.resolve(None)
                    try:
                        attached = any(s["id"] == source_id
                                       for s in await bundle.studio.sources())
                    except StudioError:
                        attached = False
                    if attached:
                        return await in_report(bundle, source_id)
                return await action(workspace.datasources(), source_id)

        return await guarded(run)

    async def scoped(report_id: str | None, attach: bool, action):
        """A report tool when a report is named/active and wanted, else the datasource scope."""
        if attach and (report_id is not None or workspace.active is not None):
            return await call(report_id, action)
        return await guarded(lambda: action(workspace.datasources()))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def bigquery_browse(
        project_id: str | None = None,
        dataset_id: str | None = None,
        recent: bool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Browse BigQuery as Add data > BigQuery does: projects (no args; recent=true for
        Recent projects), datasets of project_id, or tables of project_id+dataset_id.
        Uses the signed-in account's access. Read-only."""
        return await scoped(
            report_id, True, lambda b: b.bigquery.browse(project_id, dataset_id, recent)
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def datasource_create_bigquery(
        source: BigQuerySource,
        attach_to_report: bool = True,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a BigQuery datasource from a table or a custom query (Add data > BigQuery).

        source: {project_id, dataset_id, table_id} for a table, or {project_id (billing
        project), sql, legacy_sql?, date_range_params?, viewer_email_param?} for a custom
        query (the params enable @DS_START_DATE/@DS_END_DATE and @DS_USER_EMAIL); optional
        name, billing_project_id.
        With a report (active or report_id) and attach_to_report it is embedded in that
        report and named in its datasourceAlias, ready for components; otherwise a reusable
        datasource owned by the account. dry_run reads the connector fields (validating
        access/SQL) without creating anything. Field names in the result are what
        components_create and formulas use.
        """
        return await scoped(
            report_id,
            attach_to_report,
            lambda b: b.bigquery.create(source, attach=attach_to_report, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def datasource_add_to_report(
        datasource_id: str, report_id: str | None = None
    ) -> dict[str, Any]:
        """Add an existing datasource to the report (Add data > My data sources): names it
        in the report's datasourceAlias and verifies the report lists it as attached.
        Idempotent. Remove with datasources_remove_from_report."""
        return await call(
            report_id,
            lambda b: b.bigquery.attach(parse_datasource_reference(datasource_id)),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasources_remove_from_report(
        datasource_id: str, dry_run: bool = True, report_id: str | None = None
    ) -> dict[str, Any]:
        """Remove a datasource from the report (Manage added data sources > Remove): drops
        its datasourceAlias and the report's claim. Refused while components, filters,
        blends or parameters use it. The datasource itself is not deleted."""
        return await call(
            report_id,
            lambda b: b.bigquery.detach(parse_datasource_reference(datasource_id), dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def report_publishing(report_id: str | None = None) -> dict[str, Any]:
        """Publishing state: auto/manual, published revision, whether the draft has
        unpublished changes and which pages viewers currently see. Read-only."""
        return await call(report_id, lambda b: b.publishing.status())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def report_publish(
        revision_number: int | None = None, dry_run: bool = True, report_id: str | None = None
    ) -> dict[str, Any]:
        """Publish the draft to viewers (Publish button), or republish an earlier revision
        (Version history > Publish this version). Only for manual publishing. Viewers see
        the result immediately; verified by the published revision read-back."""
        return await call(
            report_id, lambda b: b.publishing.publish(revision_number, dry_run=dry_run)
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def report_publishing_mode(
        mode: Literal["auto", "manual"], dry_run: bool = True, report_id: str | None = None
    ) -> dict[str, Any]:
        """File > Publishing settings: auto publishes every saved change (including the
        current draft, immediately); manual keeps edits in a draft until report_publish."""
        return await call(report_id, lambda b: b.publishing.set_mode(mode, dry_run=dry_run))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def revisions_list(
        entity: Literal["report", "datasource"] = "report",
        entity_id: str | None = None,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Version history of the report (default) or of a datasource (entity_id)."""
        return await call(report_id, lambda b: b.publishing.revisions(entity, entity_id))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def revision_name_current(name: str, report_id: str | None = None) -> dict[str, Any]:
        """File > Version history > Name current version."""
        return await call(report_id, lambda b: b.publishing.name_current(name))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def revision_restore(
        revision_number: int,
        entity: Literal["report", "datasource"] = "report",
        entity_id: str | None = None,
        dry_run: bool = True,
        allow_shared_source: bool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore a revision of this report (or of a datasource attached to it) as the
        current draft, as Version history > Restore does; history keeps every revision.
        Reusable datasources need allow_shared_source. Journalled; verified by the new
        head revision. Publish separately."""
        return await call(
            report_id,
            lambda b: b.publishing.restore(
                revision_number, entity, entity_id, dry_run=dry_run,
                allow_shared_source=allow_shared_source,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False})
    async def report_open(report: str, wait_seconds: int = 90) -> dict[str, Any]:
        """Open a report by URL or UUID in the MCP Chrome profile and make it the default.

        Starts the dedicated Chrome profile headless (no window) when it is not running;
        a window opens only for a Google sign-in and closes after it. Reuses an
        existing tab of the report or opens one, closes duplicate tabs (editors after
        their autosave), reopens with the requested account selector, and loads the
        report draft in View. Returns status=ready with the report inventory;
        status=login_required when Google needs a sign-in (a window is open: ask the user to
        sign in there, then call again). access=view means the account cannot edit: saved reads
        work, native inspection and writes do not.
        """
        try:
            async with workspace.tool_lock:
                return await open_report(workspace, report, wait_seconds)
        except StudioError as exc:
            raise ToolError(f"{exc.code}: {exc}; outcome_unknown=False") from exc

    @mcp.tool(annotations={"readOnlyHint": True})
    async def reports_list() -> list[dict[str, Any]]:
        """List report tabs open in the MCP Chrome profile with mode and which one is active."""
        return await list_reports(workspace)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_fields_rename(
        datasource_id: str,
        changes: list[FieldRename],
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Rename datasource field labels, preserving internal IDs and all other definitions.

        Shared sources require allow_shared_source=true and report external impact.
        Requires matching draft/published to avoid publishing human drafts.
        Get fingerprint from datasource_get. Publishes full draft, independently reads
        both states, journals before/after for operation_restore. No automatic retry.
        This API has no characterized server CAS; preflight cannot lock out other clients.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.rename(
                source_id,
                changes,
                expected_fingerprint,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_calculated_fields_create(
        datasource_id: str,
        fields: list[CalculatedField],
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate and create calculated fields in an attached datasource.

        Requires matching draft/published and datasource_get fingerprint. Expressions
        reference existing internal fields (t0._revenue_); no within-batch references.
        Explicit aggregation is required; aggregate expressions require AUTO (7).
        Preview IDs are provisional. Publishes the full source draft once and reads
        both states back. operation_restore restores its recorded before-state.
        Does not create a datasource asset. Row/aggregate creation and undo were
        verified live; numeric results and all formula types remain unverified.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.create_calculated_fields(
                source_id,
                fields,
                expected_fingerprint,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_calculated_fields_edit(
        datasource_id: str,
        fields: list[CalculatedFieldEdit],
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Edit datasource formulas/labels/aggregation while retaining calc field IDs.

        Shared sources require explicit opt-in; unrelated unpublished drafts are rejected.
        Publishes full draft
        with journal/read-back/operation_restore. A formula may reference other
        calculated fields (t0.calc_*); an edit that would close a reference loop is
        refused with FORMULA_CYCLE.
        Existing chart references keep their IDs, but new semantics may affect them.
        ABS/SUM edits with retained IDs, reload and undo verified live. Consumer
        effects, numeric results and dependency-aware formula edits remain pending.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.edit_calculated_fields(
                source_id,
                fields,
                expected_fingerprint,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_calculated_fields_delete(
        datasource_id: str,
        field_names: list[str],
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        allow_unscanned_shared_consumers: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete unused user-created calculated fields from an attached datasource.

        Fail-closed safety checks cover the native datasource formula dependency graph
        and identifier-token references across the full saved report, all pages and
        resources. Ownerless report matches are blockers; matches explicitly scoped to
        another datasource are informational. A batch may delete fields that depend on
        each other, but any dependent outside the batch blocks it. Requires complete
        formula parsing, matching draft/published, datasource fingerprint, and an
        unchanged report through the final preflight. Preview by default; publication
        is journaled, read back, and reversible with operation_restore.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.delete_calculated_fields(
                source_id,
                field_names,
                expected_fingerprint,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
                allow_unscanned_shared_consumers=allow_unscanned_shared_consumers,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_fields_edit_metadata(
        datasource_id: str,
        changes: list[FieldMetadataEdit],
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        allow_in_use: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Edit field labels, descriptions, types/semantics, aggregation or visibility.

        Works for physical and calculated fields while retaining identity and all
        unspecified/unknown metadata. data_type and semantic_config must be supplied
        together because changing one alone creates inconsistent field semantics.
        Type, aggregation, and disabling changes inspect datasource formula dependents
        and full-report saved references; used fields require allow_in_use=true.
        Shared sources additionally require allow_shared_source=true and preview reports
        their external impact scope. Matching draft/published and source fingerprint are
        required. The report fingerprint is guarded through publication; full-draft
        publish is journaled, read back, and reversible with operation_restore.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.edit_field_metadata(
                source_id,
                changes,
                expected_fingerprint,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
                allow_in_use=allow_in_use,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def datasource_parameters_list(
        datasource_id: str, report_id: str | None = None
    ) -> dict[str, Any]:
        """List lossless datasource parameter definitions and their source fingerprint."""
        return await source_call(
            datasource_id, report_id, lambda b, source_id: b.datasources.parameters(source_id)
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_parameters_upsert(
        datasource_id: str,
        parameters: list[DatasourceParameter],
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        allow_in_use: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or replace complete datasource parameter definitions losslessly.

        The definition is Data Studio's typed wire from datasource_parameters_list;
        roles, defaults, options, validation and display metadata are retained exactly.
        New parameter (Add a parameter): {"parametersModifiedByRoles": [{"key": 1, "value":
        {"name": K, "dataType": 1 text | 2 number | 4 boolean, "lock": 2, "options": [],
        "validation": {}, "defaultValue": [{"label": "0.9", "stringValue": "0.9"}]}}, the
        same under key 2 without label], "parameterDisplayConfiguration": {"name": K,
        "displayName": "...", "displayType": 1}}; values are strings for every type.
        Replacing a referenced parameter requires allow_in_use=true. Shared sources need
        explicit opt-in. Matching draft/published, fingerprint, full-report guard,
        preview, read-back and operation_restore apply to the datasource-atomic publish.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.upsert_parameters(
                source_id,
                parameters,
                expected_fingerprint,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
                allow_in_use=allow_in_use,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_parameters_delete(
        datasource_id: str,
        parameter_keys: list[str],
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        allow_unscanned_shared_consumers: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete unused datasource parameters after conservative dependency checks.

        References in datasource formulas and the full configured report block deletion.
        Shared sources additionally require acceptance that external consumers cannot be
        inventoried. Publication is previewed, guarded, journalled, read back and undoable.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.delete_parameters(
                source_id,
                parameter_keys,
                expected_fingerprint,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
                allow_unscanned_shared_consumers=allow_unscanned_shared_consumers,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_refresh_fields(
        datasource_id: str,
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        allow_in_use: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Refresh fields from the connector, as the datasource editor's REFRESH FIELDS does.

        New columns become fields with the connector's defaults. Fields whose column is gone
        are removed; if a formula or the report in scope uses one, the refresh is blocked
        unless allow_in_use=true. Surviving fields keep every edit (labels, types,
        aggregation, descriptions); a column whose type changed is listed in
        retyped_columns, not retyped. Preview by default; publication is journaled, read
        back and reversible with operation_restore. Single-connector sources only.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.refresh_fields(
                source_id,
                expected_fingerprint,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
                allow_in_use=allow_in_use,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_connection_set(
        datasource_id: str,
        source: BigQuerySource,
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        allow_in_use: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Point a BigQuery datasource at another table or a custom query, as the editor's
        Edit connection > Reconnect does (table to custom query, query to table, or a new
        query).

        source: {project_id, dataset_id, table_id} for a table, or {project_id (billing
        project), sql, legacy_sql?, date_range_params?, viewer_email_param?} for a custom
        query; name is ignored. Fields match the new columns by column name and keep every
        edit; calculated fields and parameters stay. Columns the new connection lacks are
        removed: blocked when a formula or the report in scope uses them unless
        allow_in_use=true. The dry run reads the new columns (validating access and SQL)
        and lists connection from/to, added, missing and retyped columns. Publication is
        journaled, read back and reversible with operation_restore.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.refresh_fields(
                source_id,
                expected_fingerprint,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
                allow_in_use=allow_in_use,
                connector_config=source.connector_config()["connectorConfig"],
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_credentials_set(
        datasource_id: str,
        service_account: str | None = None,
        dry_run: bool = True,
        allow_shared_source: StrictBool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Switch a datasource to Service Account Credentials.

        Without service_account, the DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE environment
        variable names it ({project} = the BigQuery project the source runs in); with
        neither, the call is refused. datasource_get shows the current
        credentials. The editing user needs Service Account User on the account. Preview
        by default; the change is journaled and read back but not restorable, because
        owner's credentials belong to a person and cannot be replayed.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.set_credentials(
                source_id,
                service_account,
                dry_run=dry_run,
                allow_shared_source=allow_shared_source,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_settings_set(
        datasource_id: str,
        expected_fingerprint: str,
        cache_hours: int | None = None,
        field_editing_in_reports: bool | None = None,
        community_viz_access: bool | None = None,
        native_functions: bool | None = None,
        dry_run: bool = True,
        allow_shared_source: bool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Datasource editor settings: data freshness (cache_hours 0 = every 15 minutes, 1,
        4, 12 = default), field editing in reports, community visualizations access,
        native functions. datasource_get fingerprint; publishes and reads back; restorable."""
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.datasources.settings(
                source_id, expected_fingerprint, cache_hours=cache_hours,
                field_editing_in_reports=field_editing_in_reports,
                community_viz_access=community_viz_access, native_functions=native_functions,
                dry_run=dry_run, allow_shared_source=allow_shared_source,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def datasource_rename(
        datasource_id: str,
        name: str,
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_shared_source: bool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Rename a datasource (the title in its editor and in Add data / home lists).
        datasource_get fingerprint; journalled; read back on draft and published."""
        return await source_call(
            datasource_id, report_id,
            lambda b, source_id: b.datasources.rename_source(
                source_id, name, expected_fingerprint, dry_run=dry_run,
                allow_shared_source=allow_shared_source),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def datasource_sharing_get(
        datasource_id: str, report_id: str | None = None
    ) -> dict[str, Any]:
        """Who can open or edit a reusable datasource: members with owner/editor/viewer
        role (former_account_uid marks a deleted or recreated account), domains that reach
        it through its link (link_sharing_domains), whether this account may share it, and
        the fingerprint for datasource_sharing_set."""
        return await source_call(
            datasource_id, report_id, lambda b, source_id: b.sharing.get(source_id))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def datasource_sharing_set(
        datasource_id: str,
        members: list[SharingMember],
        expected_fingerprint: str,
        notify: bool = False,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Give people viewer or editor access to a reusable datasource.

        members: [{"email": "...", "role": "viewer" | "editor"}], people without access
        yet; changing or removing an existing member is refused (a person does it in the
        Share dialog). Runs the datasource's Share dialog as a person does; notify sends
        Google's invitation email. Google checks each update with reCAPTCHA and may refuse
        automated shares (SHARING_RATE_LIMITED: nothing changed; do not retry now, wait
        hours or let the user share by the link in the message). Journalled; read back.
        Use the exact email the user gave or confirmed; never guess an address.
        """
        return await source_call(
            datasource_id, report_id,
            lambda b, source_id: b.sharing.grant(
                source_id, [m.model_dump() for m in members], expected_fingerprint,
                notify=notify, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def datasource_dependencies(
        datasource_id: str,
        overrides: list[FormulaOverride] | None = None,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Inspect datasource formula field graph, reverse edges and one cycle if found.

        Validates each calculated field sequentially; cost grows with formula count.
        Requires matching draft/published. Validation failures produce partial output,
        never proof of no dependencies. Does not cover report consumers or parameters
        and does not authorize field deletion. No source/report mutation.
        Optional overrides replace expressions in the inspected graph only, using
        fully qualified field_id (t0.calc_*). Detects proposed cycles, but expression
        types are validated against the CURRENT source, not propagated future types.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.formulas.dependencies(source_id, overrides),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def formula_references(
        datasource_id: str, expression: str, report_id: str | None = None
    ) -> dict[str, Any]:
        """Validate expression and read direct field occurrences from the native editor AST.

        Uses internal ns.field names from datasource_get. String literals are not
        references. Source fingerprint checked across validation and parsing.
        Positions are UTF-16 code units, not Python character indices. This is
        not a transitive dependency graph, parameter inventory or deletion approval.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.formulas.references(source_id, expression),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def formula_validate(
        datasource_id: str, expression: str, report_id: str | None = None
    ) -> dict[str, Any]:
        """Validate an expression for an attached datasource without saving any field.

        Use internal field references, e.g. ABS(t0._revenue_), from datasource_get.
        Returns Google column metadata (dataType, aggregation/type and semantics)
        or diagnostics. A validation failure may be syntax, schema or connector
        failure: inspect diagnostics. Uses native uncached revision-0 validation;
        isolated unpublished-draft and blend contexts are not yet characterized.
        Success does not prove business correctness or evaluate resulting values.
        """
        return await source_call(
            datasource_id,
            report_id,
            lambda b, source_id: b.formulas.validate(source_id, expression),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def component_interaction_state(
        page_id: str, component_id: str, report_id: str | None = None
    ) -> dict[str, Any]:
        """Read current native viewer selection, reset availability and inherited filters.

        Returns onSelect behavior/default init separately from its runtime value,
        inherited cross-filters and slider filters. Effective filters are reported as
        unavailable (not_exposed_by_bundle), never as an empty-filter success. This is
        browser-session state, not a saved configuration edit or proof of query/data
        results. Rejects stale/dirty native models; report_refresh resets viewer state.
        No selection, reset, query refresh or report write is performed by this tool.
        """
        return await call(report_id, lambda b: b.interactions.inspect(page_id, component_id))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def page_filter_inheritance(page_id: str, report_id: str | None = None) -> dict[str, Any]:
        """Resolve saved and current runtime filter inheritance for every page component.

        The saved view walks report → page → outer/inner groups → component, applies
        every inheritFilters boundary, and reports nested-group controlStatePassThrough
        scope. The native view adds provenance layers, runtime cross-filters, validator
        compatibility for every saved filter against each loaded component schema.
        Effective runtime definitions are reported as not_exposed_by_bundle: the current
        bundle no longer exports that resolver. It fails on malformed/ambiguous group
        ownership, cycles, dirty/stale editor state, or a report change during
        inspection. Read-only; compatibility does not prove backend query results.
        """
        return await call(report_id, lambda b: b.inheritance.inspect(page_id))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def control_checkbox_set(
        page_id: str,
        component_id: str,
        value: StrictBool | None,
        expected_interaction_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Set a rendered non-parameter checkbox in the current View document.

        True/False select a boolean filter; null clears it (not reset-to-default).
        Get the fingerprint from component_interaction_state. Durable before-state,
        native same-document conflict guard and read-back; no automatic retries.
        operation_restore restores its previous runtime state only in that same
        document, rejecting intervening changes. Reload expires runtime recovery.
        Does not change saved defaults. Verification covers control state and saved
        page, not consumer query completion. Parameters/Analysis are unsupported.
        """
        return await call(
            report_id,
            lambda b: b.checkbox.set(
                page_id, component_id, value, expected_interaction_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def control_filter_set(
        page_id: str,
        component_id: str,
        value: RuntimeFilterValue,
        expected_interaction_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Set the runtime condition/value of a rendered dimension filter control.

        Supports non-parameter list/input/advanced/slider presets through the native
        canvas-control editor. The saved bound concept is immutable here: the tool
        builds the filter on the control's actual concept and validates it against
        the native condition/schema contract. Get the interaction fingerprint from
        component_interaction_state. Browser-session state only; preview by default,
        journalled dispatch/read-back, and operation_restore while the document lives.
        Checkbox has a stricter dedicated tool; parameters and Analysis are rejected.
        """
        return await call(
            report_id,
            lambda b: b.runtime_controls.set_filter(
                page_id,
                component_id,
                value,
                expected_interaction_fingerprint,
                dry_run=dry_run,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def control_parameter_set(
        page_id: str,
        component_id: str,
        values: list[RuntimeParameterValue],
        expected_interaction_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Set typed runtime values on the parameter bound to a rendered control.

        The parameter key is read from the native component and cannot be redirected by
        the caller. Values use stringValue, numberValue or boolValue wire fields. The
        action is browser-session-only, previewed, conflict-guarded, journalled, read
        back and reversible in the same document with operation_restore.
        """
        return await call(
            report_id,
            lambda b: b.runtime_controls.set_parameter(
                page_id,
                component_id,
                values,
                expected_interaction_fingerprint,
                dry_run=dry_run,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def control_date_range_set(
        page_id: str,
        component_id: str,
        value: RuntimeDateRange,
        expected_interaction_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Set an absolute YYYYMMDD runtime range on a date-range control.

        Uses the component's native onSelect behavior. Browser-session-only, previewed,
        conflict-guarded, journalled, read back and reversible in the same document.
        Saved defaults are unchanged; control_reset returns to the saved default.
        """
        return await call(
            report_id,
            lambda b: b.runtime_controls.set_date_range(
                page_id,
                component_id,
                value,
                expected_interaction_fingerprint,
                dry_run=dry_run,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def control_reset(
        page_id: str,
        component_id: str,
        expected_interaction_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Reset a rendered filter, parameter or date control to its saved default.

        Uses stateActionService's
        addressable reset, not a page-wide reset or DOM keystrokes. Requires a current
        interaction fingerprint. Browser-session state only; dispatch is journalled
        and read back, and operation_restore can restore the pre-reset runtime value
        while the same browser document remains alive.
        """
        return await call(
            report_id,
            lambda b: b.runtime_controls.reset(
                page_id,
                component_id,
                expected_interaction_fingerprint,
                dry_run=dry_run,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def page_screenshot(page_id: str, quality: int = 70, report_id: str | None = None):
        """Image of a page's whole canvas as viewers will see it (draft content, after data
        loads). Use it to check layout and visual design after edits. Read-only."""
        return await call(report_id, lambda b: b.visual.page_screenshot(page_id, quality))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def image_export(
        page_id: str, component_id: str, path: str, report_id: str | None = None
    ) -> dict[str, Any]:
        """Save an image component's uploaded file to a local path (a folder keeps the
        original file name). Read-only for the report; use it to copy images between
        reports (then image_upload_set)."""
        return await call(report_id, lambda b: b.visual.image_export(page_id, component_id, path))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def report_pdf(
        path: str,
        page_ids: list[str] | None = None,
        print_background: bool = True,
        add_report_link: bool = False,
        expand_tables: bool = False,
        page_numbers: bool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """File > Download as > PDF: render the report (all pages in navigation order, or
        page_ids) with Google's PDF service and save it to a local .pdf path."""
        return await call(
            report_id,
            lambda b: b.visual.report_pdf(
                path, page_ids, print_background=print_background,
                add_report_link=add_report_link, expand_tables=expand_tables,
                page_numbers=page_numbers,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def native_catalog(
        types: list[str] | None = None,
        panels: bool = False,
        full: bool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """The editor's own catalog of what can be configured.

        Default: every component type/preset (name, group, field cardinalities). types
        (e.g. ["simple-table", "kpi-metric/default"]) adds each one's complete property panel:
        tabs, sections, every setting's label, control, allowed values, range, binding.
        panels adds the report (theme/layout/settings) and page panels. full returns all
        (large; for regenerating docs/inventory/native-catalog.json). Types are created only
        on the unsaved viewer model, then it is reloaded; nothing is written.
        """
        return await call(
            report_id, lambda b: b.inventory.native_catalog(types, panels, full)
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def report_content_get(report_id: str | None = None) -> dict[str, Any]:
        """Saved report-level content (attributes, properties, theme and every resource:
        datasource aliases, parameters, dimension colors, field overrides, report-level
        components) with the fingerprint report_patch needs. Read-only."""
        return await call(report_id, lambda b: b.patches.read())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def report_patch(
        patches: list[Patch],
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """RFC 6902 operations on report_content_get's content, for anything no typed tool
        covers. Paths are relative to that content (/attributeConfig/reportAttribute/...,
        /propertyConfig/reportProperty/..., /resource/...). Prefer settings_set: it applies
        the editor's own rules. One journalled write; read-back; operation_restore."""
        return await call(
            report_id,
            lambda b: b.patches.report(patches, expected_fingerprint, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def report_rename(
        name: str, dry_run: bool = True, report_id: str | None = None
    ) -> dict[str, Any]:
        """Rename the report (the title in the header). Verified by read-back. The report
        description is a settings_set key (reportDescription) on {"kind":"report"}."""
        return await call(report_id, lambda b: b.patches.rename(name, dry_run=dry_run))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def dimension_value_colors_set(
        colors: list[DimensionColor],
        replace: bool = False,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Resource > Manage dimension value colors: fixed color per dimension value in
        every chart (when the theme colors by dimension values). Merges by value unless
        replace. Max 1500 values. Journalled, verified, restorable."""
        return await call(
            report_id,
            lambda b: b.patches.dimension_colors(colors, replace=replace, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def filters_list(report_id: str | None = None) -> dict[str, Any]:
        """Read saved filter resources, full definitions and exact reference locations.

        Searches all saved pages, nested objects, report-level content and resources
        (including blends), not only top-level charts. Distinguishes filter-list
        bindings from conservative unclassified exact references and reports dangling
        filter-list IDs. No references is not permission to delete: this is a saved
        document inventory, not proof of runtime inheritance or external usage.
        A source absent from attached datasources is labelled, never silently removed.
        """
        return await call(report_id, lambda b: b.filters.inventory())

    @mcp.tool(annotations={"readOnlyHint": True})
    async def filter_conditions_list(report_id: str | None = None) -> dict[str, Any]:
        """Read native condition codes, labels and compatible concept data/semantic types.

        Current editor catalog, including legacy conditions not offered by every UI.
        Use filter_validate to check an actual field/tree; catalog membership alone
        does not establish valid values, regex syntax or datasource compatibility.
        """
        return await call(report_id, lambda b: b.filter_conditions.catalog())

    @mcp.tool(annotations={"readOnlyHint": True})
    async def filter_validate(
        datasource_id: str, tree: FilterNode, report_id: str | None = None
    ) -> dict[str, Any]:
        """Validate predicate/and/or filter trees against an attached datasource.

        Predicate: kind=predicate, field={name or expression}, condition=GT/T/IN/etc,
        include=true/false, string_values/number_values. Group: kind=and/or, children.
        Metric role requires explicit field aggregation; formulas use server validation.
        No retained chart concept IDs. Matching source draft/published required; blends
        and parameter filters are not characterized here. Returned concept IDs are
        provisional. Success validates configuration, not query results or saved state.
        """
        return await call(report_id, lambda b: b.filter_conditions.validate(datasource_id, tree))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def filters_create(
        filters: list[NewFilter],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Create saved filter resources from native-validated predicate/and/or trees.

        Each item has name, datasource_id and tree (see filter_validate). Requires
        attached source with matching draft/published; formulas validated by Google.
        Preserves all existing resources; one journalled report-resource write and
        read-back. Does not attach new filters to charts/pages/blends or publish drafts.
        Preview IDs are provisional. Source checks are preflight, not cross-entity CAS.
        operation_restore removes the created resources if no later consumers/edits conflict.
        """
        return await call(
            report_id,
            lambda b: b.filters.create(
                filters, expected_report_content_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def filters_update_conditions(
        changes: list[FilterConditionChange],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace complete condition trees of saved filters by ID in one resource write.

        Each change has filter_id and tree (see filter_validate). Keeps the existing
        datasource, name, filter ID, bindings and opaque entry/value metadata. Replaces
        the entire filterDefinition and its format version, not individual leaves.
        Native validation requires an attached source with matching draft/published.
        Preview includes old/new definitions and exact saved references: changes can
        affect multiple charts and blend inputs. Source validation is not proof of
        every consumer query result. Journal, conflict preflight, full read-back and
        operation_restore; no automatic publication, detachment or mutation retry.
        """
        return await call(
            report_id,
            lambda b: b.filters.update_conditions(
                changes, expected_report_content_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def filters_rename(
        changes: list[FilterRename],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Rename saved filters by exact ID in one report-resource write.

        Get report_content_fingerprint from filters_list. Preserves definitions,
        IDs, bindings, other resources and report-level components/concepts. A rename
        may affect labels on many consumers; preview lists saved references. No name
        matching, source editing or automatic publication. Durable journal, conflict
        preflight, independent read-back and operation_restore. Does not change filter
        conditions or create/delete a filter. Unknown outcomes are not retried.
        """
        return await call(
            report_id,
            lambda b: b.filters.rename(
                changes, expected_report_content_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def filters_delete(
        filter_ids: list[str],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete unreferenced saved filter resources by ID, never datasource/report assets.

        Get fingerprint and inspect exact saved references with filters_list. Rejects
        bindings on pages, nested/report components, blend inputs and unclassified
        exact references. No automatic detachment/cascade. Full definitions retained
        in the journal; operation_restore restores original IDs and registry ordering
        if no later edits conflict. Preflight/read-back is not a cross-entity lock
        against another editor adding references concurrently. Preview by default;
        no automatic publication or mutation retries.
        """
        return await call(
            report_id,
            lambda b: b.filters.delete(
                filter_ids, expected_report_content_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_filters(
        page_id: str,
        changes: list[ComponentFilterChange],
        expected_page_fingerprint: str,
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Set saved filter bindings and inheritance on components within one page.

        Each change: component_id, optional filter_ids (complete ordered list; []
        clears local bindings), optional inherit_filters. Omitted/null properties
        stay unchanged. Get fingerprints from page_get and filters_list; refresh
        the viewer before preparation. Native component-schema validation rejects
        incompatible filters; direct attached datasources with matching draft/published
        required. Does not create/delete resources or mutate viewer control selections.
        Page journal/read-back/undo also guard report resource changes. Not a cross-
        entity transaction or proof of query results. Page/group/report-level binding
        and blend datasets need their own operations. Preview by default; no publication.
        """
        return await call(
            report_id,
            lambda b: b.component_filters.patch(
                page_id,
                changes,
                expected_page_fingerprint,
                expected_report_content_fingerprint,
                dry_run=dry_run,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def page_filters(
        page_id: str,
        change: FilterBinding,
        expected_page_fingerprint: str,
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Set page-local saved filter IDs and/or inheritance from report scope.

        change.filter_ids replaces the complete list ([] clears); inherit_filters
        changes inheritance independently; omitted/null properties stay unchanged.
        Native validation uses each filter's attached source with matching draft and
        published schema. Preview consumer compatibility is advisory: incompatible
        consumers may ignore inherited filters, and a compatible one may disable
        inheritance. Does not rewrite child settings or runtime control selections.
        Refresh viewer first, use page_get and filters_list fingerprints. One guarded
        page write/read-back/undo; no automatic publication or cross-entity transaction.
        """
        return await call(
            report_id,
            lambda b: b.page_filters.patch(
                page_id,
                change,
                expected_page_fingerprint,
                expected_report_content_fingerprint,
                dry_run=dry_run,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def report_filters(
        filter_ids: list[str],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace report-level saved filter IDs; [] clears this scope's bindings.

        Get fingerprint and IDs from filters_list; refresh viewer before preparation.
        No inherit flag exists above report scope. Validates filters on their attached
        sources with matching draft/published. May affect eligible consumers on every
        page; does not change child inheritance settings. Compatibility diagnostics
        cover loaded report-level components only, not every child-page consumer.
        Full report write preserving all other settings/resources and pages, durable
        journal, conflict/read-back/undo guards; no publication or automatic retries.
        """
        return await call(
            report_id,
            lambda b: b.report_filters.patch(
                filter_ids, expected_report_content_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def groups_filters(
        page_id: str,
        changes: list[GroupFilterChange],
        expected_page_fingerprint: str,
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Set group filter IDs and/or inheritance in one page-atomic batch.

        Each change: group_id, optional filter_ids (complete list, [] clears), optional
        inherit_filters. Omitted/null properties stay unchanged. Preserves members,
        child-group links, controlStatePassThrough, geometry and child settings.
        Native source validation and advisory direct-member compatibility; no claim
        of full nested-group/blend traversal or effective runtime inheritance.
        Refresh viewer first; fingerprints from page_get and filters_list. Attached
        filter sources with matching draft/published required. Guarded journalled
        page write/read-back/undo; no publication, retries or cross-entity transaction.
        """
        return await call(
            report_id,
            lambda b: b.group_filters.patch(
                page_id,
                changes,
                expected_page_fingerprint,
                expected_report_content_fingerprint,
                dry_run=dry_run,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def blends_list(report_id: str | None = None) -> dict[str, Any]:
        """List report-local data views: inputs, joins, filter IDs and exact references.

        Includes single-query views, not only multi-source blends. Join type codes
        are returned without guessing enum meanings. Missing attached sources,
        unknown tree shapes and dangling dataset bindings are reported. No mutation,
        schema validity, runtime usage or deletion safety is inferred from this list.
        """
        return await call(report_id, lambda b: b.blends.inventory())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def blend_create(
        blend: NewBlend,
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a blend (Resource > Manage blends > Add a blend).

        blend: {name, tables: [{datasource_id, dimensions: [field names], metrics:
        [{name, aggregation?}]}] (2-5 tables attached to the report, fields from
        datasource_get), joins: one per table after the first, in order: {join_type
        (LEFT_OUTER_JOIN default, INNER_JOIN, RIGHT_OUTER_JOIN, FULL_OUTER_JOIN,
        CROSS_JOIN, UNION), keys: [{left_table (earlier table index), left_field,
        right_field}]}} - key fields must be listed as dimensions. Output fields are
        built by the editor's own blend schema builder. Fingerprint from blends_list.
        Result blend_id is the datasource ID for components_create; output field
        names are what charts use. Input datasources need their schema loaded in the
        editor (SCHEMA_NOT_READY: use one in any chart first, or report_refresh). Table
        names, input filters and the date range
        dimension are set afterwards with blends_inputs_rename / blends_input_filters.
        One journalled report write, restorable with operation_restore.
        """
        return await call(
            report_id,
            lambda b: b.blend_create.create(
                blend, expected_report_content_fingerprint, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def blends_delete(
        blend_ids: list[str],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Remove blends from the report (Manage blends > Remove). Refused while a
        component, filter or other resource still references a blend. One journalled
        report write, restorable with operation_restore."""
        return await call(
            report_id,
            lambda b: b.blend_create.delete(
                blend_ids, expected_report_content_fingerprint, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def blends_joins(
        changes: list[BlendJoinChange],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Change merge types/key pairs on 1-100 distinct saved blend nodes.

        Use exact join_path and fingerprint from blends_list. key_pairs omitted
        preserves existing keys; Cross explicitly removes conditions. Non-cross
        merges need keys; provide them when converting from Cross. Pair names are
        input concept IDs (not output labels), as shown by blend_validate/blend_get.
        Union requires the current native feature flag. Source draft/published
        equality and native structural candidate validation precede a single guarded
        report write. Unknown unrelated fields and all input/output fields survive.
        No query-result or cross-source type-compatibility guarantee. Preview by
        default; applied changes are reversible through operation_restore.
        """
        return await call(
            report_id,
            lambda b: b.blend_joins.patch(
                changes, expected_report_content_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def blend_validate(
        blend_id: str, rebuild_output_schema: bool = False, report_id: str | None = None
    ) -> dict[str, Any]:
        """Validate a saved blend using the current native detached runtime model.

        Returns native structural issue codes, runtime joins and normalization paths.
        Requires fresh saved/native report state; does not save or execute queries.
        Structural validity is not source/schema compatibility or feature availability.
        Use blend_join_types separately for the current Union flag. Non-left-deep
        trees remain visible in blend_get but this native validator rejects them.
        Optional rebuild_output_schema recomputes output fields on the detached
        model using current native source metadata and returns rebuilt_output_fields.
        Requires attached inputs with equal draft/published source definitions and
        source freshness checks. Does not save normalization or query the data.
        """
        return await call(
            report_id,
            lambda b: b.blends.validate(blend_id, rebuild_output_schema=rebuild_output_schema),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def blend_join_types(report_id: str | None = None) -> dict[str, Any]:
        """Read current native blend merge types and Union feature availability.

        Resolves the selector enum from the connected browser, without saving or
        changing the report. Union uses its own saved tree node, unlike ordinary
        joins. Availability does not imply this MCP can yet write all join types
        or that any particular input/key configuration is valid.
        """
        return await call(report_id, lambda b: b.blends.join_types())

    @mcp.tool(annotations={"readOnlyHint": True})
    async def blend_get(blend_id: str, report_id: str | None = None) -> dict[str, Any]:
        """Read one full saved data-view definition, queries, joins and reference paths.

        Preserves opaque server fields. Input/join paths are relative to the returned
        definition, reference paths are absolute within reportConfig. No live-query
        execution or datasource/report asset operation is performed.
        """
        return await call(report_id, lambda b: b.blends.inventory(blend_id))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def blends_input_filters(
        changes: list[BlendInputFilters],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace filter lists on 1-100 distinct saved blend inputs in one report write.

        Use exact input_path and report fingerprint from blends_list. filter_ids is
        the complete list; [] clears it. Only saved filters belonging to the input
        datasource can be attached; source must be attached with matching draft and
        published definitions. Clearing does not require access to an old source.
        Preserves joins, fields, other input settings and consumers. Validates filters
        against native source schemas, not query execution. Preview by default; undo
        uses operation_restore. Does not edit or publish datasource assets.
        """
        return await call(
            report_id,
            lambda b: b.blend_filters.patch(
                changes, expected_report_content_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def blends_inputs_rename(
        changes: list[BlendInputRename],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Rename 1-100 input table labels using exact paths from blends_list.

        Changes only query.displayName. Empty name removes the custom label and
        restores the native default; maximum 100 UTF-16 code units. Duplicate
        names are permitted, repeated target inputs are not. Preserves source IDs,
        concept IDs, fields, joins, filters and unknown properties. Historical
        missing sources do not prevent label-only edits. Preview by default;
        one guarded report write and operation_restore for applied changes.
        """
        return await call(
            report_id,
            lambda b: b.blends.rename_inputs(
                changes, expected_report_content_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def blends_rename(
        changes: list[BlendRename],
        expected_report_content_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Rename 1-100 report-local data views by ID, in one guarded report write.

        Get the fingerprint from blends_list. Changes only datasourceBlock.name;
        preserves queries, joins, fields, IDs, bindings and unknown values. Names
        need not be unique. Does not rename/create/publish datasource assets.
        Missing historical input sources do not prevent this label-only edit.
        Preview by default. Applied edits have durable read-back and operation_restore.
        """
        return await call(
            report_id,
            lambda b: b.blends.rename(
                changes, expected_report_content_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def settings_describe(
        target: Target,
        search: str = "",
        offset: int = 0,
        limit: int = 60,
        include_hidden: bool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """List every setting of an editor property panel with its current value.

        target: {"kind":"report"} (theme, layout, canvas, grid, navigation, report settings),
        {"kind":"page","page_id":...} (page style, canvas, page data settings) or
        {"kind":"component","page_id":...,"component_id":...} (every Setup and Style input of
        any chart/control/text/image/shape, common background/border/header included).
        Each item: key, label, control, tab, section, value, options (allowed values),
        min/max, bindings, writable. Search filters by key/label/section. Only inputs the
        panel currently shows are listed; include_hidden adds the rest (per-series slots,
        inputs whose UI is conditional). Returns the target fingerprint for settings_set.
        Read-only.
        """
        return await call(
            report_id, lambda b: b.panels.describe(target, search, offset, limit, include_hidden)
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def settings_set(
        target: Target,
        changes: list[SettingChange],
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Change any editor panel settings natively, in one journalled write.

        Keys and value shapes come from settings_describe (value = what describe shows,
        an option's value for dropdowns). Changes apply in order through the editor's own
        bindings, so dependent settings follow as in the UI; theme settings on a built-in
        theme first copy it into the report's custom theme (as Customize does). Only the
        resulting native delta is written over the saved content; read-back verified;
        operation_restore undoes it. dry_run previews before/after without writing.
        """
        return await call(
            report_id,
            lambda b: b.panels.set(target, changes, expected_fingerprint, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_scope(
        page_id: str,
        component_ids: list[str],
        to: Literal["report", "page"],
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Arrange > Make report-level (to="report": the components show on every page) or
        Make page-level (to="page": report-level components move onto page_id). Ungrouped
        top-level components without conditional formatting. Two journalled writes (target
        first, then source); a failure between them is returned as partial, never lost."""
        return await call(
            report_id, lambda b: b.scope.move(page_id, component_ids, to, dry_run=dry_run)
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def report_variables_list(report_id: str | None = None) -> dict[str, Any]:
        """Resource > Manage variables: report variables with their full wire definitions
        and the report fingerprint for report_variables_write. Read-only."""
        return await call(report_id, lambda b: b.variables.list())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def report_variables_write(
        expected_fingerprint: str,
        upserts: list[ReportVariable] | None = None,
        deletes: list[str] | None = None,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Create, replace or delete report variables (Manage variables). Each upsert is
        {parameter_key, parameter}: parameter is the wire shape report_variables_list
        returns (parametersModifiedByRoles roles 1 and 5 with dataType, defaultValue,
        options, optional parameterQueryConfig; parameterDisplayConfiguration.displayName).
        Deleting a variable still bound by Control visibility is refused. Journalled,
        verified, restorable with operation_restore."""
        return await call(
            report_id,
            lambda b: b.variables.write(
                upserts or [], deletes or [], expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def theme_from_image(
        path: str,
        choice: int | None = None,
        expected_report_fingerprint: str | None = None,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Theme > Extract theme from image. Without choice: sends the local image to
        Datastudio's theme generator and returns up to 3 generated themes (colors,
        series, palette) plus report_fingerprint; nothing changes. With choice (0-based)
        and that fingerprint: stores the chosen theme as the report's custom theme and
        activates it, exactly as the dialog's Apply (fonts and other slots reset to the
        editor defaults, as in the UI). One journalled report write, restorable."""
        if choice is not None and expected_report_fingerprint is None:
            raise ToolError("MISSING_FINGERPRINT: pass report_fingerprint from the options call")
        return await call(
            report_id,
            lambda b: b.theme_image.extract(
                path, choice, expected_report_fingerprint or "", dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def community_viz_list(report_id: str | None = None) -> dict[str, Any]:
        """Community visualizations registered in the report (Manage community
        visualizations), the components using each, and the fingerprint for writes."""
        return await call(report_id, lambda b: b.community_viz.list())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def community_viz_register(
        viz_id: str,
        expected_fingerprint: str,
        register: bool = True,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Add (or with register=false remove) a community visualization to the report
        (needed before components_create can use its type). viz_id: manifest path
        (gs://bucket/path or google://...) plus U+00A0 and the component ID, e.g.
        "google://devrel/googlecharts\u00a0gauge", "gs://datastudio_visualizations/funnel/v4/prod\u00a0pmaMetricFunnel".
        Then components_create with type=viz_id (no explicit fields: the editor binds its
        defaults; adjust data slots with components_patch). Removal is refused while
        components use it. Journalled, verified, restorable."""
        return await call(
            report_id,
            lambda b: b.community_viz.write(viz_id, register, expected_fingerprint,
                                            dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def field_overrides_list(report_id: str | None = None) -> dict[str, Any]:
        """Resource > Manage field names and IDs: the report's field overrides
        ({displayNameOverride, idOverride, originalId, originalName, datasourceId})
        and the fingerprint field_overrides_write needs."""
        return await call(report_id, lambda b: b.field_overrides.list())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def field_overrides_write(
        expected_fingerprint: str,
        upserts: list[FieldOverride] | None = None,
        deletes: list[FieldRef] | None = None,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Override a field's display name and/or ID across this report (Manage field
        names and IDs). upserts: {datasource_id, field (source field name), display_name?,
        id_override?}; deletes: {datasource_id, field}. Giving fields of several
        datasources the same id_override lets one filter or control apply to all of
        them. The datasource itself is unchanged. Journalled, verified, restorable."""
        return await call(
            report_id,
            lambda b: b.field_overrides.write(
                upserts or [], deletes or [], expected_fingerprint, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def visibility_set(
        page_id: str,
        expected_fingerprint: str,
        component_id: str | None = None,
        source_key: str | None = None,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Control visibility: show a component (or the page when component_id is omitted)
        only when a variable is true / the viewer is in its group. source_key is
        "REPORT_VARIABLE:<parameterKey>" or "DATASOURCE_VARIABLE:<datasourceId>:<key>";
        null removes the rule. page_get fingerprint; journalled and restorable."""
        return await call(
            report_id,
            lambda b: b.variables.visibility(
                page_id, component_id, source_key, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_copy_to_page(
        source_page_id: str,
        component_ids: list[str],
        target_page_id: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Copy components to another page (copy + paste): same geometry, settings and
        fields, new IDs. Ungrouped top-level components; conditional formatting rules are
        not copied. One journalled, verified, restorable write of the target page."""
        return await call(
            report_id,
            lambda b: b.scope.copy_to_page(
                source_page_id, component_ids, target_page_id, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def report_links(page_id: str | None = None, report_id: str | None = None):
        """Share > Get report link / Embed report: view, edit and embed URLs plus an iframe
        snippet sized to the canvas; `embeddable` says whether embedding is enabled
        (settings_set on the report to change it). Read-only."""
        return await call(report_id, lambda b: b.publishing.links(page_id))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def settings_describe_page(
        page_id: str,
        component_ids: list[str] | None = None,
        search: str = "",
        include_hidden: bool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """settings_describe for every component of a page (or the listed ones) in ONE
        call: {component_id: {type, items}} plus the page fingerprint for
        settings_set_batch. Use search to keep it small (e.g. "boxShadow"). Far faster
        than one settings_describe per component. Read-only."""
        return await call(
            report_id,
            lambda b: b.panels.describe_page(page_id, component_ids, search, include_hidden),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def settings_set_batch(
        page_id: str,
        components: list[ComponentSettingChanges],
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """settings_set for many components of one page in ONE write: each item is
        {component_id, changes} (changes exactly as for settings_set). Much faster than one
        settings_set per component (one read-back and editor reload in total). All or
        nothing: any refused change aborts the batch with nothing written. page_get
        fingerprint; journalled; operation_restore undoes it."""
        return await call(
            report_id,
            lambda b: b.panels.set_batch(page_id, components, expected_fingerprint,
                                         dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def image_upload_set(
        page_id: str,
        component_id: str,
        path: str,
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local image file (png/jpeg/gif/svg/webp, < 25 MB) into an image component
        (create one with components_create type image-component). Uses the panel's own
        upload; one journalled page write, read back, restorable. page_get fingerprint."""
        return await call(
            report_id,
            lambda b: b.panels.image(
                page_id, component_id, path, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def report_audit(
        live: bool = True, live_timeout_seconds: int = 30, page_ids: list[str] | None = None,
        report_id: str | None = None
    ) -> dict[str, Any]:
        """Find everything that silently breaks the report or differs for viewers.

        Saved rules check every page, filter, blend and attached datasource: filters on
        ad-hoc formulas ('New field'), fields missing or disabled in their datasource,
        filters bound where their fields do not exist (silently not applied), detached
        or dangling datasources/filters/blends, broken datasource field formulas
        (including columns dropped by a source schema change), unpublished datasource
        drafts and viewer-credential datasources, chart-local formulas, unused filters
        and blends. With live=true it also opens each page in the viewer and reports
        components that render with an error or an invalid configuration, then returns
        to the starting page (page_ids limits the live pass to those pages). Each finding
        has rule, severity, meaning, fix and location.
        healthy=false means at least one error. Read-only; needs the tab in View mode
        for the live pass.
        """
        return await call(
            report_id,
            lambda b: Audit(b.studio).run(live=live, live_timeout_seconds=live_timeout_seconds,
                                          page_ids=page_ids),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def bundle_contracts(report_id: str | None = None) -> dict[str, Any]:
        """Check every native capability the MCP relies on against the loaded editor.

        Data Studio's minified bundle renames members on each release. Tools resolve
        them by behaviour at call time; this reports, per contract, ok (with the name
        it resolved to), changed (with the reason) or not_testable (no live object of
        that kind in this report). Run it first when a tool fails with
        NATIVE_CONTRACT_FAILED or *_CONTRACT_CHANGED: a changed entry names exactly
        which signature in bundle.py must be updated. Read-only.
        """
        return await call(report_id, lambda b: NativeContracts(b.studio).check())

    @mcp.tool(annotations={"readOnlyHint": True})
    async def report_inspect(report_id: str | None = None) -> dict[str, Any]:
        """Get report identity, navigation, pages, versions and page fingerprints."""
        return await call(report_id, lambda b: b.studio.inventory())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_fields(
        page_id: str,
        selections: list[FieldSelection],
        expected_page_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace primary field lists and chart-local display names with native setters.

        Omit a role to keep it; [] requests clearing it, subject to native validation.
        Dimension-filter controls use native controlField/filterMetric bindings.
        Fields use names from datasource_get and metrics need explicit aggregation.
        Sources and source field definitions are unchanged. Set concept_name to
        an existing qt_* name from page_get to retain its ID and unmentioned metadata;
        otherwise creates a fresh chart-local concept. Independent sort dependencies are
        retained by Google, even when their fields are no longer visible. Preview
        returns the full updated components for review. Loaded static ungrouped
        components only; unsupported preset/UI dependencies are rejected.
        Formula selections use expression instead of name and run formula_validate
        before writing. Aggregate expressions require aggregation=7 (AUTO); row
        formulas need an explicit outer aggregation when used as metrics. Formula
        writes currently require identical draft/published datasource definitions.
        Other field roles, drill-down and datasource switching remain unsupported.
        """
        return await call(
            report_id,
            lambda b: b.fields.replace(
                page_id, selections, expected_page_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_create(
        page_id: str,
        new_components: list[NewComponent],
        expected_page_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a batch of native preset components on a loaded static page.

        Use native_catalog for type/preset IDs and datasource_get for field names.
        Metrics require explicit aggregation using the source/editor enum, not a
        guessed default. Fields accept name or validated expression, plus optional
        display_name; aggregate expressions require AUTO (7). New components cannot
        retain concept_name from another component. Primary dimensions/metrics are
        supported; presets needing
        other bindings fail native validation. Composite/responsive creation is not
        yet supported. All drafts are validated before one versioned page write.
        Preview IDs are provisional. operation_restore removes the added components
        if no later changes conflict. Does not create datasource/report assets.
        """
        return await call(
            report_id,
            lambda b: b.creation.create(
                page_id, new_components, expected_page_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def pages_create(
        new_pages: list[NewPage],
        expected_structure_fingerprint: str,
        index: int | None = None,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Create blank pages inside this report, with names, hidden flag and icons.

        index is the top-level navigation insertion position; omitted means append.
        Get structure_fingerprint from report_inspect. Preview IDs are provisional.
        Multiple API requests may be needed; inspect partial outcomes, never replay
        blindly. operation_restore reverses a creation when no later edits conflict.
        """
        return await call(
            report_id,
            lambda b: b.pages.create(
                new_pages, expected_structure_fingerprint, index, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def pages_import(
        source_report_id: str,
        page_ids: list[str],
        expected_structure_fingerprint: str,
        section_name: str | None = None,
        replace: dict[str, str] | None = None,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Copy pages from another report into this one (copy and paste between reports).

        replace={source_page_id: page_id_here} puts a source page's content on an
        existing page instead of a new one: the page keeps its ID (links, bookmarks),
        name and navigation place; one journalled page write, undone by operation_restore.
        Use it to bring a redesign made on a copy back to the original report.

        Open both reports with report_open first (the target last, or pass report_id).
        The source is only read. Brings what the pages need: attaches their reusable
        datasources, copies the saved filters they reference, creates the pages with
        conditional formatting (links between imported pages point to the copies),
        optionally as one navigation section, and re-uploads image files. Each step is
        journalled and read back; a failure returns status=partial with the steps done.
        Refuses blends, report variables and datasources embedded in the source.
        Fingerprint from report_inspect of the target.
        """
        return await call(
            report_id,
            lambda b: PageImport(b.studio).run(
                workspace.resolve(parse_report_reference(source_report_id)).studio, page_ids,
                expected_structure_fingerprint, section_name=section_name, replace=replace,
                dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def report_theme_copy(
        source_report_id: str,
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Give this report the theme of another report (its custom theme, made active),
        e.g. after pages_import so the copied pages look exactly like the source.
        Report-wide: every page here takes it. Open both reports with report_open; the
        source is only read. Fingerprint from report_content_get. Journalled, restorable."""
        return await call(
            report_id,
            lambda b: ThemeCopy(b.studio).run(
                workspace.resolve(parse_report_reference(source_report_id)).studio,
                expected_fingerprint, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def pages_delete(
        page_ids: list[str],
        expected_structure_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete selected report pages, not the report asset. Never delete the last page.

        Saves complete page definitions and navigation for operation_restore. Restore
        recreates the original IDs/content then navigation; audit timestamps change.
        Preview first; references in other entities require separate dependency review.
        """
        return await call(
            report_id,
            lambda b: b.pages.delete(page_ids, expected_structure_fingerprint, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def page_duplicate(
        page_id: str,
        name: str,
        expected_structure_fingerprint: str,
        index: int | None = None,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Duplicate a loaded page, retaining fields/styles and sharing its existing sources.

        Open the source page in View first. Native duplicateConfig generates component
        IDs and control aliases; only its delta is applied to full saved JSON, preserving
        opaque properties. Copies page visibility/icon. Returns a component ID map.
        Conditions with owners absent from the source page are omitted from the copy
        as in native duplication; omitted_condition_owner_ids reports them explicitly.
        This creates a page inside the bound report, never a report/datasource asset.
        """
        return await call(
            report_id,
            lambda b: b.pages.duplicate(
                page_id, name, expected_structure_fingerprint, index, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def navigation_patch(
        patches: list[Patch],
        expected_structure_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Patch report navigation: page names, icons, visibility, order and navigation settings.

        RFC 6902 paths are relative to report_inspect.navigation. Preserve unknown
        properties; every existing page must remain represented exactly once.
        Versioned request and complete read-back; operation_restore reverses changes.
        """
        return await call(
            report_id,
            lambda b: b.pages.navigation(patches, expected_structure_fingerprint, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def report_refresh(
        page_id: str | None = None, report_id: str | None = None
    ) -> dict[str, Any]:
        """Reload the report draft in the MCP tab, optionally on another page.

        Tools already reload after writes and when the model is stale; use this to open a
        specific page (e.g. before page_duplicate) or after edits made outside the MCP.
        Never discards unsaved editor changes: an open editor is autosaved first.
        """
        return await call(report_id, lambda b: b.studio.refresh(page_id))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def page_get(page_id: str, report_id: str | None = None) -> dict[str, Any]:
        """Read a complete page, including components, geometry, styles and field bindings."""

        async def read(bundle):
            page = find_page(await bundle.studio.report(), page_id)
            return {"page": page, "fingerprint": fingerprint(page["page"])}

        return await call(report_id, read)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def datasources_list(
        report_id: str | None = None, all_accessible: StrictBool = False
    ) -> list[dict[str, Any]]:
        """List the sources attached to a report, or every datasource the account can open.

        With all_accessible=true, or with no report open, lists the account's reusable
        datasources (id, name, owner, modified) without any report. Otherwise lists the
        sources attached to the report; isEmbedded distinguishes shared sources.
        """
        if all_accessible or (report_id is None and workspace.active is None):
            return await guarded(lambda: workspace.datasources().studio.sources())
        return await call(report_id, lambda b: b.studio.sources())

    @mcp.tool(annotations={"readOnlyHint": True})
    async def datasource_get(datasource_id: str, report_id: str | None = None) -> dict[str, Any]:
        """Read a source's draft and published definitions, fields, formulas, types, credentials.

        datasource_id is a UUID or a datasource URL. No report is needed; see the
        datasource-scope rule in the server instructions.
        """
        return await source_call(
            datasource_id, report_id, lambda b, source_id: b.studio.datasource(source_id)
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def operation_restore(operation_id: str, dry_run: bool = True) -> dict[str, Any]:
        """Restore a journalled edit or viewer operation if no later edit conflicts.

        The journal record names its report; that report is resolved automatically and
        its tab must be open in the MCP Chrome profile. Preview by default. Restoration
        is itself journaled and verified. Unknown outcomes need inspection first.
        Runtime-control recovery additionally requires the original browser document;
        reload/navigation replacement expires it rather than applying old state to a new session.
        """
        record = journal_record(config.state_dir, operation_id)
        if record["report_id"] is None:
            return await guarded(
                lambda: workspace.datasources().studio.restore_operation(
                    operation_id, dry_run=dry_run
                )
            )
        return await call(
            record["report_id"],
            lambda b: b.studio.restore_operation(operation_id, dry_run=dry_run),
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def operation_get(operation_id: str) -> dict[str, Any]:
        """Read durable mutation status without dumping the saved report content."""
        record = journal_record(config.state_dir, operation_id)
        return {
            key: record[key]
            for key in (
                "operation_id",
                "report_id",
                "page_id",
                "datasource_id",
                "kind",
                "started_at",
                "state",
                "mismatches",
                "dispatch_error",
                "edit_scope",
                "steps",
                "create_page_ids",
                "delete_page_ids",
            )
            if key in record
        }

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_patch(
        page_id: str,
        changes: list[ComponentChange],
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Patch existing components on ONE page in a single versioned write.

        Each change identifies a component and RFC 6902 add/replace/remove/test
        operations relative to that component. Preserve all unknown fields.
        Use page_get's fingerprint. Geometry is in attributeConfig.componentAttribute;
        styles and bindings are in propertyConfig.componentProperty and conceptDefs.
        dry_run previews changes without a remote write. Returns a durable operation
        ID and read-back status. Does not change a component's ID or datasource.
        """
        return await call(
            report_id,
            lambda b: b.studio.patch_components(
                page_id, changes, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_switch_type(
        page_id: str,
        changes: list[TypeChange],
        expected_fingerprint: str,
        dry_run: bool = True,
        allow_loss: bool = False,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Convert chart types/presets through isolated native editor transformations.

        Preview exposes all changed/removed settings and before/after bindings.
        Optional dimensions/metrics select source fields or validated expressions before validation;
        omitted roles retain native conversion, empty lists clear a supported role.
        Metrics require explicit aggregation; aggregate expressions use AUTO (7).
        concept_name retains an existing local field ID. Special roles remain separate work.
        Non-preview writes require allow_loss when settings or fields disappear/change.
        Same datasource, static ungrouped non-composite charts; conditional formatting
        remapping and other contexts remain separate work. Requires report_refresh,
        page_get fingerprint and matching datasource draft/published. One journaled
        page write/read-back/undo. No claim of equivalent rendering or numeric results.
        """
        return await call(
            report_id,
            lambda b: b.switching.patch(
                page_id, changes, expected_fingerprint, dry_run=dry_run, allow_loss=allow_loss
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def page_insert_space(
        page_id: str,
        at_top: int,
        height: int,
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Make room for a new section: every component starting at or below at_top
        (pixels) moves down by height, groups as a whole, and the page canvas grows by
        height. A negative height removes empty space (refused if anything would overlap).
        One page write with preview, read-back and operation_restore.
        Static canvas only; page_get fingerprint."""
        return await call(
            report_id,
            lambda b: b.components.insert_space(
                page_id, at_top, height, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def groups_move(
        page_id: str,
        moves: list[GroupMove],
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Translate 1–100 ordinary groups, preserving their members and settings.

        Integer pixel offset_x/offset_y applied equally to every member. Preserves
        sizes, rotations, relative positions, layer order, group filters/control scope.
        Static canvas, direct leaf members only; nested/overlapping groups and
        composites rejected. A zero translation returns unchanged without writing.
        report_refresh first, page_get fingerprint, preview, one page write and undo.
        """
        return await call(
            report_id,
            lambda b: b.components.groups_move(
                page_id, moves, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def groups_create(
        page_id: str,
        groups: list[NewGroup],
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Create 1–100 ordinary groups from disjoint ungrouped top-level components.

        Each group needs 2–100 members. Static canvas only; no regroup/nesting.
        Defaults come from the native editor. Grouping changes control/filter scope.
        Preview IDs are provisional; saved component payloads/order stay untouched.
        report_refresh first, page_get fingerprint, one journaled page write and undo.
        """
        return await call(
            report_id,
            lambda b: b.components.groups_create(
                page_id, groups, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def groups_ungroup(
        page_id: str,
        group_ids: list[str],
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Remove ordinary group containers, retaining every component and its order.

        Removes group-local filters and returns members to page control/filter scope;
        inspect removed_groups in preview. No nested or responsive groups. Uses
        report_refresh, page_get fingerprint, one journaled page write/read-back/undo.
        """
        return await call(
            report_id,
            lambda b: b.components.groups_ungroup(
                page_id, group_ids, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_layers(
        page_id: str,
        component_ids: list[str],
        action: LayerAction,
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Change stacking of 1–100 top-level components on one ungrouped static page.

        Actions: bring_to_front, send_to_back, bring_forward, send_backward.
        Preserves relative order inside selected and unselected subsets, independent
        of input ID order. At the requested boundary returns unchanged without a
        write. Component payloads, child order, geometry and conditions are preserved.
        Preview lists full before/after order back-to-front. report_refresh first,
        page_get fingerprint, versioned write/read-back/journal/operation_restore.
        Grouped and responsive pages and report-level layers need separate support.
        """
        return await call(
            report_id,
            lambda b: b.components.layers(
                page_id, component_ids, action, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_arrange(
        page_id: str,
        component_ids: list[str],
        action: Arrangement,
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Align or distribute unrotated, ungrouped leaf components on a static page.

        Align 2–100 objects against their selection bounds, not canvas bounds.
        Distribute 3–100 objects with equal edge gaps, holding first/last positions
        fixed. Negative gaps (overlap) are allowed. Stable position ties preserve
        input ID order. Computed coordinates truncate toward zero to integer pixels
        as updatePage does; preview includes requested and persisted coordinates.
        Sizes, opposite-axis positions, styles and layers remain
        unchanged. report_refresh first; page_get fingerprint; one versioned page
        write with preview/read-back/journal/undo. Groups/responsive are separate.
        """
        return await call(
            report_id,
            lambda b: b.components.arrange(
                page_id, component_ids, action, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_geometry(
        page_id: str,
        changes: list[ComponentGeometry],
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Set absolute pixel positions/sizes for 1–100 components in one page write.

        Only ungrouped top-level leaf components on a static canvas. Load the page
        with report_refresh first; use page_get's fingerprint. Unspecified geometry
        and all unknown fields are retained. Values truncate toward zero to whole
        pixels; preview shows requested and after values. Dimensions must remain
        at least one pixel. Does not scale children, change layers, or change rotation.
        Supports preview, durable journal, independent read-back and operation_restore.
        """
        return await call(
            report_id,
            lambda b: b.components.geometry(
                page_id, changes, expected_fingerprint, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_duplicate(
        page_id: str,
        component_ids: list[str],
        expected_fingerprint: str,
        offset_x: float = 20,
        offset_y: float = 20,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Copy ungrouped top-level components on the same static-canvas page in one write.

        Load the page using report_refresh first. Preserves fields/styles, regenerates
        IDs/control aliases and copies owned conditional formatting. New objects are
        appended to the component list, offset in pixels. Preview IDs are provisional.
        Grouped/responsive/component-child operations require separate support.
        """
        return await call(
            report_id,
            lambda b: b.components.duplicate(
                page_id, component_ids, expected_fingerprint, offset_x, offset_y, dry_run=dry_run
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def components_delete(
        page_id: str,
        component_ids: list[str],
        expected_fingerprint: str,
        dry_run: bool = True,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete ungrouped top-level components and their owned formatting in one page write.

        Does not delete datasource assets, fields or report resources. Group membership
        is guarded. operation_restore can recreate deleted components from the journal.
        """
        return await call(
            report_id,
            lambda b: b.components.delete(
                page_id, component_ids, expected_fingerprint, dry_run=dry_run
            ),
        )

    return mcp


def main() -> None:
    create_server(BrowserConfig.from_env()).run(transport="stdio")


if __name__ == "__main__":
    main()
