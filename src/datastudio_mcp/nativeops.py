"""Run any editor operation natively, then save only what it changed.

The UI changes a report by calling its own services on the loaded model (create a
component, apply a panel input, customize a theme, ...). In View, the draft model never
autosaves, so the same services can run there safely: the page or report is serialized
with the native serializer before and after the operation, that delta is merged over the
saved JSON (unknown server fields kept, list elements matched by identity), written as
one journalled, read-back-verified write, and the viewer model is reloaded afterwards so
nothing applied in memory outlives the call.

Operation scripts are page functions run through native_script, so they may use R and
the helpers of OPS_PRELUDE; they return {before, after, ...plan} or {error, details}.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .browser import StudioError
from .bundle import native_script
from .document import (
    drop_new_nulls,
    normalize_colors,
    find_page,
    fingerprint,
    json_equal,
    overlay_delta,
)
from .report_resources import ReportResources, report_content

if TYPE_CHECKING:
    from .service import Studio

# Shared page-side context: report/page checks and the native serializers.
OPS_PRELUDE = r"""
const OPS=(args)=>{
    const i=window.angular?.element(document.body).injector();
    const stop=(code,details)=>{throw Object.assign(Error(code),{code,details})};
    // Only the View route is safe: the editor would autosave these in-memory edits.
    if(!/^(?:\/u\/[0-9]+)?\/reporting\/[^/]+(?:\/page\/[^/]+)?\/?$/.test(location.pathname))
        stop('EDITOR_MODE_UNSAFE');
    if(!i?.has('reportContextService'))stop('EDITOR_NOT_READY');
    const report=i.get('reportContextService').getReport();
    if(report?.getId()!==args.reportId)stop('OUT_OF_SCOPE');
    if(report.isReady$?.value!==true)stop('EDITOR_NOT_READY');
    const owner=args.pageId?report.getPage(args.pageId):null;
    if(args.pageId&&!owner)stop('NOT_FOUND','page');
    if(owner&&owner.isReady$?.value!==true)stop('EDITOR_NOT_READY','page');
    const current=String((owner||report).getVersion());
    if(!/^[0-9]+$/.test(current))stop('EDITOR_STALE','draft model not loaded');
    if(args.version!==undefined&&current!==args.version)stop('EDITOR_STALE','use report_refresh');
    const dirty=m=>Object.values(m?.isChanged||{}).some(Boolean);
    // A later step of one batch runs on the model the earlier steps changed.
    if(!args.continuing&&[report,owner].some(dirty))stop('EDITOR_DIRTY');
    const registry=i.get('legoMainRegistry');
    const clean=v=>{try{return v===undefined?null:JSON.parse(JSON.stringify(v,(k,x)=>
        typeof x==='function'?undefined:x instanceof Map?Object.fromEntries(x):
        x instanceof Set?[...x]:x))}catch{return null}};
    const serializeReport=()=>clean(R.exported('reportConfigBuilder')(report.getConfig(),
        registry,undefined));
    const serialize=()=>owner?clean(R.exported('pageConfigBuilder')(owner.getConfig(),
            {displayName:'draft'},undefined,registry).page)
        :serializeReport();
    return {i,report,owner,registry,clean,serialize,serializeReport,stop,dirty};
};
"""


_CONDITIONS = """async args => {
    const X=OPS(args);
    return {conditions: X.clean(R.exported('pageConfigBuilder')(X.owner.getConfig(),
        {displayName:'draft'},undefined,X.registry).chartCondition)};
}"""


def ops_script(body: str) -> str:
    """Wrap an operation body (`async args => {...}`) with error reporting and OPS.

    A page-scoped operation is written as a page; if it also changed report-level content
    (resources, theme), that part would be silently lost, so it is refused instead.
    """
    return native_script(
        "async function(args){"
        + OPS_PRELUDE
        + "try{const X0=args.pageId?OPS(args):null;const r0=X0&&JSON.stringify("
        "X0.serializeReport());const out=await ("
        + body
        + ")(args);if(r0&&out&&!out.error&&JSON.stringify(X0.serializeReport())!==r0)"
        "return {error:'CROSS_SCOPE_SIDE_EFFECT',details:'operation changed report-level "
        "content; use the report target'};return out}"
        "catch(error){return {error:error.code||'NATIVE_CONTRACT_FAILED',"
        "details:error.details??String(error?.message||error)}}}"
    )


class NativeOps:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
        session = self.studio.session
        page = await session.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(ops_script(script), {"reportId": session.config.report_id, **args}),
                timeout=session.config.timeout_ms / 1000 * 2,
            )
        except Exception as exc:
            raise StudioError("NATIVE_CONTRACT_FAILED", f"Native operation failed: {exc}") from exc
        if not isinstance(result, dict):
            raise StudioError("NATIVE_CONTRACT_FAILED", "Native operation returned no result")
        if result.get("error"):
            raise StudioError(result["error"], str(result.get("details", "")))
        return result

    async def _conditions(self, page_id: str, version: str) -> list[dict[str, Any]]:
        out = await self.evaluate(_CONDITIONS, {"pageId": page_id, "version": version,
                                                "continuing": True})
        return out["conditions"] or []

    async def run(
        self,
        script: str,
        args: dict[str, Any] | list[dict[str, Any]],
        *,
        page_id: str | None,
        expected_fingerprint: str,
        dry_run: bool,
        plan: dict[str, Any] | None = None,
        guard: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Run one native operation on a page (page_id) or the report (None) and save it.

        guard runs under the mutation lock right before and after the native operation
        (e.g. to recheck datasource fingerprints the operation's inputs were validated on).
        """
        async with self.studio.mutation_lock():
            raw = await self.studio.report()
            config = raw["reportConfig"]
            if page_id is None:
                saved = report_content(config)
                version = str(config["version"])
            else:
                frame = find_page(raw, page_id)
                saved = frame["page"]
                version = str(frame["version"])
            if fingerprint(saved) != expected_fingerprint:
                raise StudioError(
                    "VERSION_CONFLICT", "Target changed since it was read; read it again"
                )
            # From here the viewer model may hold uncommitted edits: stale until reloaded.
            self.studio.session.stale = True
            try:
                if guard is not None:
                    await guard()
                # Several steps (a list of args) run one after another on the live model
                # and are saved as one write: the first step's before, the last's after.
                steps = args if isinstance(args, list) else [args]
                # Conditional formatting lives beside the page body (chartCondition).
                conditions_before = (await self._conditions(page_id, version)
                                     if page_id is not None else None)
                result: dict[str, Any] = {}
                for index, step in enumerate(steps):
                    try:
                        out = await self.evaluate(script, {**step, "pageId": page_id,
                                                           "version": version,
                                                           "continuing": index > 0})
                    except StudioError as exc:
                        if len(steps) == 1:
                            raise
                        # Name the failing step, so a caller can drop it and retry.
                        raise StudioError(exc.code, f"step {index}: {exc}") from exc
                    result = {**out, "before": result.get("before", out.get("before"))}
                    if len(steps) > 1:
                        result.setdefault("steps", []).append(
                            {k: v for k, v in out.items() if k not in ("before", "after")})
                # EDITOR_STALE here (loaded model off its saved state) reaches server.call(),
                # which reloads and runs the whole tool once more; nothing was sent yet.
                merged = overlay_delta(result.pop("before"), result.pop("after"), saved)
                if guard is not None:
                    await guard()
                after = normalize_colors(drop_new_nulls(merged, saved))
                if page_id is None:
                    after.pop("chartCondition", None)
                summary = {**(plan or {}), **result}
                conditions = None
                if page_id is not None:
                    conditions_after = await self._conditions(page_id, version)
                    if not json_equal(conditions_after, conditions_before):
                        conditions = conditions_after
                        summary["chart_conditions"] = conditions
                if json_equal(after, saved) and conditions is None:
                    outcome = {**summary, "status": "unchanged"}
                elif dry_run:
                    outcome = {**summary, "status": "planned", "before": saved, "after": after}
                elif page_id is None:
                    # The operated report model must be gone before the write: reloaded
                    # after it, the editor saves the published revision's report-level
                    # content (theme, filters, canvas) over the draft (reproduced).
                    await self.studio.session.acquire(reload=True)
                    outcome = {**summary, **await ReportResources(self.studio).write(
                        config, after, settings=True)}
                else:
                    outcome = {**summary, **await self.studio._write_page(
                        frame, copy.deepcopy(after), conditions)}
            except BaseException:
                # The operation lives only in the viewer model: reload it, keep the error.
                with contextlib.suppress(Exception):
                    await self.studio.session.acquire(reload=True)
                raise
            try:
                await self.studio.session.acquire(reload=True)
            except Exception as exc:
                # A failed reload must not hide a write's result; the model stays stale
                # and the next tool call reloads it.
                outcome["reload_pending"] = getattr(exc, "code", type(exc).__name__)
            return outcome
