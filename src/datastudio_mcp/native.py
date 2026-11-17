"""Bounded native-editor contracts. No arbitrary evaluation is exposed to MCP."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .browser import StudioError
from .bundle import native_script
from .document import overlay_native_delta, walk_components

if TYPE_CHECKING:
    from .browser import ReportSession


def merge_duplicate(
    original: dict[str, Any], duplicate: dict[str, Any], saved: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    native_ids = [c["componentId"] for c in walk_components(original["page"]["componentConfig"])]
    saved_ids = [c["componentId"] for c in walk_components(saved["page"]["componentConfig"])]
    duplicate_ids = [
        c["componentId"] for c in walk_components(duplicate["page"]["componentConfig"])
    ]
    if native_ids != saved_ids or len(native_ids) != len(duplicate_ids):
        raise StudioError("EDITOR_STALE", "Native component inventory differs from saved page")
    draft = overlay_native_delta(original, duplicate, saved)
    if draft["pageId"] == saved["pageId"]:
        raise StudioError("CODEC_CONTRACT_CHANGED", "Duplicate did not receive a new page ID")
    # View's native serializer can omit chart conditions entirely. Preserve the
    # saved definitions, but rebind their owner IDs to the duplicated charts.
    id_map = dict(zip(native_ids, duplicate_ids, strict=True))
    omitted = []
    retained = []
    for condition in draft.get("chartCondition", []):
        chart_id = condition.get("chartId")
        if not isinstance(chart_id, str):
            raise StudioError("INVALID_CONDITION_OWNER", "Condition must have a string chartId")
        if chart_id in id_map:
            condition["chartId"] = id_map[chart_id]
        elif chart_id not in duplicate_ids:
            omitted.append(chart_id)
            continue
        retained.append(condition)
    draft["chartCondition"] = retained
    return draft, omitted


class Native:
    def __init__(self, session: ReportSession):
        self.session = session

    async def require_static_page(self, frame: dict[str, Any]) -> None:
        page = await self.session.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    """({reportId,pageId,version}) => {
                    const injector = window.angular?.element(document.body).injector();
                    if (!injector?.has('reportContextService'))
                        return {error:'EDITOR_NOT_READY'};
                    const report = injector.get('reportContextService').getReport();
                    if (!report || report.getId() !== reportId) return {error:'OUT_OF_SCOPE'};
                    const owner = report.getPage(pageId);
                    if (!owner || owner.isReady$?.value !== true)
                        return {error:'EDITOR_NOT_READY'};
                    if (String(owner.getVersion()) !== version) return {error:'EDITOR_STALE'};
                    if (owner.isResponsiveLayout())
                        return {error:'RESPONSIVE_LAYOUT_REQUIRES_SEPARATE_OPERATION'};
                    if ([report,owner,...owner.getComponents()].some(model =>
                        Object.values(model.isChanged||{}).some(Boolean)))
                        return {error:'EDITOR_DIRTY'};
                    return {static:true};
                    }""",
                    {
                        "reportId": self.session.config.report_id,
                        "pageId": frame["pageId"],
                        "version": str(frame["version"]),
                    },
                ),
                timeout=self.session.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError(
                "NATIVE_CONTRACT_FAILED", "Cannot inspect native page layout"
            ) from exc
        if result.get("error"):
            raise StudioError(
                result["error"], "Load the static page in View before changing geometry"
            )
        if result.get("static") is not True:
            raise StudioError("NATIVE_CONTRACT_FAILED", "Native layout result is incomplete")

    async def group_drafts(
        self, frame: dict[str, Any], groups: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        await self.require_static_page(frame)
        page = await self.session.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    native_script(r"""({reportId,pageId,version,groups}) => {
                const i=window.angular?.element(document.body).injector();
                const report=i.get('reportContextService').getReport();
                if(report.getId()!==reportId)throw Error('OUT_OF_SCOPE');
                const owner=report.getPage(pageId);
                if(!owner?.isReady$?.value||String(owner.getVersion())!==version||
                    owner.isResponsiveLayout())throw Error('EDITOR_STALE');
                const serializers=R.candidates('pageConfigBuilder');
                if(serializers.length!==1)throw Error('CODEC_CHANGED');
                const before=JSON.stringify(owner.getConfig());
                const drafts=groups.map(spec=>{
                    const defaults=R.componentConfig([owner,report],
                        {datasourceId:'',displayType:'group'});
                    const keys=defaults.config.datasource.keys;
                    if(keys.controlStatePassThrough)keys.controlStatePassThrough.value=false;
                    return {id:spec.group_id,componentIds:spec.component_ids,childGroupIds:[],
                        config:{datasource:{keys},display:{type:'group',keys:{}}},
                        conceptDefs:defaults.conceptDefs};
                });
                const wire=serializers[0]({...owner.getConfig(),component:[],group:drafts},
                    {displayName:'draft'},undefined,i.get('legoMainRegistry'));
                if(JSON.stringify(owner.getConfig())!==before)throw Error('MODEL_CHANGED');
                return wire.page.groupConfig;
                }"""),
                    {
                        "reportId": self.session.config.report_id,
                        "pageId": frame["pageId"],
                        "version": str(frame["version"]),
                        "groups": groups,
                    },
                ),
                timeout=self.session.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError(
                "NATIVE_CONTRACT_FAILED", "Cannot prepare native group defaults"
            ) from exc
        if not isinstance(result, list) or len(result) != len(groups):
            raise StudioError("NATIVE_CONTRACT_FAILED", "Native group inventory differs")
        for spec, wire in zip(groups, result, strict=True):
            if (
                not isinstance(wire, dict)
                or wire.get("groupId") != spec["group_id"]
                or wire.get("componentId") != spec["component_ids"]
                or wire.get("childGroupId")
            ):
                raise StudioError("NATIVE_CONTRACT_FAILED", "Native group identity differs")
            # Newly serialized optional group messages can be null in Playwright
            # transport; updatePage omits these exact null fields on read-back.
            # Never apply this normalization to an existing saved group.
            properties = wire.get("propertyConfig", {}).get("groupProperty", {})
            for key in ("backgroundAndBorderProperty", "groupAlignment"):
                if key in properties and properties[key] is None:
                    del properties[key]
        return result

    async def duplicate_page(
        self,
        frame: dict[str, Any],
        metadata: dict[str, Any],
        *,
        require_static: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        page = await self.session.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    native_script("""({reportId,pageId,version,metadata,requireStatic}) => {
                const injector = window.angular?.element(document.body).injector();
                if (!injector || !injector.has('reportContextService'))
                    return {error:'EDITOR_NOT_READY'};
                const report = injector.get('reportContextService').getReport();
                if (!report || report.getId() !== reportId) return {error:'OUT_OF_SCOPE'};
                const owner = report.getPage(pageId);
                if (!owner || owner.isReady$?.value !== true) return {error:'EDITOR_NOT_READY'};
                if (String(owner.getVersion()) !== version) return {error:'EDITOR_STALE'};
                if (requireStatic && owner.isResponsiveLayout())
                    return {error:'RESPONSIVE_LAYOUT_REQUIRES_SEPARATE_OPERATION'};
                if ([report,owner,...owner.getComponents()].some(model =>
                    Object.values(model.isChanged||{}).some(Boolean)))
                    return {error:'EDITOR_DIRTY'};
                const registry = injector.get('legoMainRegistry');
                // Contract drift is a hard failure, never a guessed serializer.
                const serializers = R.candidates('pageConfigBuilder');
                if (serializers.length !== 1) return {error:'CODEC_CONTRACT_CHANGED'};
                const serialize = serializers[0];
                const original = serialize(owner.getConfig(), metadata, undefined, registry);
                const duplicate = serialize(owner.duplicateConfig(), metadata, undefined, registry);
                return {original,duplicate};
            }"""),
                    {
                        "reportId": self.session.config.report_id,
                        "pageId": frame["pageId"],
                        "version": str(frame["version"]),
                        "metadata": metadata,
                        "requireStatic": require_static,
                    },
                ),
                timeout=self.session.config.timeout_ms / 1000,
            )
        except Exception as exc:
            raise StudioError(
                "NATIVE_CONTRACT_FAILED", "Native duplication could not be prepared"
            ) from exc
        if result.get("error"):
            raise StudioError(
                result["error"], "Open/refresh the source page in View before duplication"
            )
        original, duplicate = result["original"], result["duplicate"]
        saved = {
            "pageId": frame["pageId"],
            "page": frame["page"],
            "chartCondition": frame.get("chartCondition", []),
            **metadata,
        }
        return merge_duplicate(original, duplicate, saved)
