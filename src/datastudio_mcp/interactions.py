"""Read native viewer interaction state independently of saved configuration."""

from typing import Any

from .browser import StudioError
from .bundle import native_script
from .document import find_page, fingerprint
from .service import Studio


class Interactions:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def inspect(self, page_id: str, component_id: str) -> dict[str, Any]:
        saved = find_page(await self.studio.report(), page_id)
        page = await self.studio.session.page()
        result = await page.evaluate(
            native_script(_INSPECT),
            {
                "reportId": self.studio.session.config.report_id,
                "pageId": page_id,
                "componentId": component_id,
                "version": str(saved["version"]),
            },
        )
        if result.get("error"):
            raise StudioError(result["error"], "Native interaction state is unavailable")
        return {
            **result,
            "page_fingerprint": fingerprint(saved["page"]),
            "interaction_fingerprint": fingerprint(result),
            "scope": "Current browser session; not saved defaults or proof of rendered data",
        }


_INSPECT = """({reportId,pageId,componentId,version}) => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService')||!i.has('stateActionService'))
        return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport();
    if(!report||report.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    const owner=report.getPage(pageId);
    if(!owner)return {error:'NOT_FOUND'};
    if(String(owner.getVersion())!==version)return {error:'EDITOR_STALE'};
    const component=owner.getComponent(componentId);
    if(!component)return {error:'NOT_FOUND'};
    if(component.isReady$?.value!==true)return {error:'EDITOR_NOT_READY'};
    const ancestors=new Set([report,owner]);
    for(let node=component;node&&!ancestors.has(node);node=node.getParent?.())ancestors.add(node);
    if([...ancestors].some(node=>Object.values(node.isChanged||{}).some(Boolean)))
        return {error:'EDITOR_DIRTY'};
    const clean=value=>value===undefined?null:JSON.parse(JSON.stringify(value));
    const state=component.getState(),type=component.getDisplayTypeObj();
    const behavior=component.getBehavior('onSelect')||[];
    const controllers=[...document.querySelectorAll('canvas-control-editor')]
        .map(node=>window.angular.element(node).controller('canvasControlEditor'))
        .filter(controller=>controller?.componentId===componentId);
    let control={status:'not_applicable'};
    let parameter={status:'not_applicable'};
    if(component.getDisplayType()==='dimension-filter'){
        control={status:'unavailable'};
        const controller=controllers.length===1?controllers[0]:null;
        const dimension=controller?.dimension||component.getConcepts?.('dimensions')?.[0];
        if(dimension){
            let proto=null;
            try{proto=R.conceptOf(dimension)}catch{}
            const action=behavior.find(item=>item?.isControl&&
                item.action==='lego-dimension-filter');
            const selected=state.getValue('onSelect');
            const concept=selected?.dConcept||action?.init?.dConcept||proto;
            control={status:'available',enabled:controller?controller.isEnabled===true:true,
                is_parameter:false,is_analysis:!!controller?.isAnalysis,
                concept:clean(concept)};
        }else{
            const action=behavior.find(item=>item?.isControl&&
                item.action==='lego-dimension-filter');
            const selection=state.getValue('onSelect')||action?.init;
            const overrides=selection?.connectorConfigParameter?.parameterOverrides;
            const controllerKey=controller?.parameter?.sourceKey?.parameterKey||
                controller?.parameter?.sourceKey?.id;
            const key=overrides?.length===1?overrides[0]?.key:controllerKey;
            if(typeof key==='string')parameter={status:'available',key,
                enabled:controller?controller.isEnabled===true:true,
                values:clean(overrides?.[0]?.value?.parameterValues||null)};
        }
    }
    let dateRange={status:'not_applicable'};
    if(component.getDisplayType()==='simple-daterangepicker'){
        const action=behavior.find(item=>item?.isControl&&
            item.action==='lego-change-date-range');
        if(action)dateRange={status:'available',value:clean(
            (state.getValue('onSelect')||action.init||{}).dateRange||null)};
        else dateRange={status:'unavailable',value:null};
    }
    let checkbox={status:'not_applicable'};
    if(component.getDisplayType()==='dimension-filter'&&state.getValue('controlMode')===3){
        checkbox={status:'unavailable'};
        if(controllers.length===1){
            const controller=controllers[0],settings=controller.checkboxSettings;
            if(settings&&(settings.value===undefined||typeof settings.value==='boolean')){
                checkbox={status:'available',value:clean(settings.value),
                    is_parameter:!!controller.parameter,is_analysis:!!controller.isAnalysis,
                    enabled:controller.isEnabled===true};
            }
        }
    }
    let effective={status:'not_applicable',filters:null};
    // The bundle no longer exports its effective-filter resolver.
    if(type.getNeedData())effective={status:'unavailable',filters:null,
        reason:'not_exposed_by_bundle'};
    return {
        document_id:String(performance.timeOrigin),
        component_id:componentId,page_id:pageId,type:component.getDisplayType(),
        datasource_id:component.getDatasourceOrViewId?.()||null,
        preset:component.getPresetConfigName(),editor_page_version:String(owner.getVersion()),
        can_reset:i.get('stateActionService').canReset(componentId),
        on_select_behavior:clean(behavior),
        on_select_value:clean(state.getValue('onSelect')),
        control_mode:clean(state.getValue('controlMode')),
        inherited_cross_filters:clean(state.getValue('crossFilters')),
        slider_filters:clean(state.getValue('sliderFilters')),
        effective_filters:effective,
        control,
        parameter,
        date_range:dateRange,
        checkbox,
    };
}"""
