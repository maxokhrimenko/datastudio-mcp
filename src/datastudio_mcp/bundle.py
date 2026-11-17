"""Native bundle contracts resolved by behaviour, not by minified name.

Data Studio ships a Closure-compiled bundle whose member names change on every
rebuild (pb -> qb, Oc -> Pc, qxa -> ixa, ...) and whose service methods may be
devirtualized into free exported functions. Pinning those names broke the MCP
twice. Every native capability the MCP needs is therefore declared once here and
resolved at call time from the native source itself: an exported function by the
text it must contain, a member by what its body does, a field name by parsing the
native code that reads it. A capability resolves to exactly one candidate (copies
with identical source count as one) or throws naming its contract, so drift is
reported precisely instead of surfacing as an anonymous TypeError.

`native_script` wraps a page script so it can use `R`. `NativeContracts` checks
every capability against the open report without dispatching anything.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .browser import StudioError

if TYPE_CHECKING:
    from .service import Studio

RESOLVER = r"""const R=(()=>{
    const source=fn=>{try{return Function.prototype.toString.call(fn)}catch{return ''}};
    const escape=text=>text.replace(/[$]/g,'\\$');
    const fail=(contract,detail)=>{
        const error=new Error(`NATIVE_CONTRACT_CHANGED[${contract}]: ${detail}`);
        error.contract=contract;error.code='NATIVE_CONTRACT_CHANGED';throw error;
    };
    const matches=(text,needles)=>needles.every(needle=>
        typeof needle==='string'?text.includes(needle):needle.test(text));
    // Methods: Closure keeps aliases with identical bodies (zg/oe); one candidate.
    // Exports: one function object exported twice is one candidate; equal source
    // under different closures is not.
    const distinct=(items,key)=>[...new Map(items.map(item=>[key(item),item])).values()];
    const single=(contract,items,key)=>{
        const found=distinct(items,key);
        return found.length===1?found[0]:fail(contract,`${found.length} candidates`);
    };
    const capture=(contract,text,pattern)=>{
        const match=pattern.exec(text);
        return match?match[1]:fail(contract,`pattern ${pattern} not found`);
    };
    // Exported bundle functions, by text their source must contain.
    const EXPORTS={
        filterValidator:['.filterDefinition','.isDummy','.version||3'],
        formulaRequestBuilder:['_validateField','parameterOverrides:[]'],
        joinTypeSelector:['this.JoinType=','.unionsEnabled()'],
        blendModel:['Unknown TreeQueryBlock type, cannot create RuntimeBlend',
            'columnPruningEnabled'],
        blendValidator:['reason:134','reason:152','.conceptPairs'],
        blendOutputSchemaBuilder:['.getDatasourceIds()','.getMeta(','"_dv"','sourceFieldName'],
        // Full new-component config (display, datasource keys, conceptDefs, behaviour).
        componentConfigBuilder:['presetConfigName','supportsArbitraryConfigs','datasourceOrViewId',
            'displayConfigVersion'],
        pageConfigBuilder:['componentConfig:[],groupConfig:[],conceptDefs:[]','hiddenFromViewer','pageId:'],
        // Report model config -> updateReport reportRequest.
        reportConfigBuilder:['componentConfig:[],chartCondition:[],conceptDefs:[]','parameterResource',
            'colorMappingList'],
        // Resolved theme content of a model (built-in or the report's custom theme resource).
        themeResolver:['getResourceIdByDisplayKey("themeConfig")','getWrapperType'],
        // Byte-array base64 encoder with selectable alphabets (image upload uses variant 3).
        base64Encoder:['Math.floor(','.length/3)','[64]||""'],
        // Stores theme content as the report's custom theme resource, returning its ID.
        themeStore:['updateResourceById("theme"','addResource("theme"'],
        propertyRuleCombiner:['.callback.apply(','.subConfig'],
        // Registry registration of a display type (built-in or community visualization).
        registerDisplayType:['is already registered','.definePresets()'],
        // Cached getCommunityVizRegistrationInfo load of a viz for a report (manifest,
        // consent, config); later renders and the type object read that cache.
        vizRegistrationLoader:['.has(b+"."+c)','.get(b+"."+c)'],
        // Rebuilds a chart's effective sort (behavior onSort/onPreSort) from its sortData,
        // as the Sort panel does after every change; the chart sorts by the behavior.
        sortBehaviorSync:['setBehavior("onSort"','"onPreSort"'],
    };
    // Methods of live objects, by what their body does.
    const MEMBERS={
        'schema.field':['.concepts[a]||'],
        'field.concept':['.queryTimeTransformation.isValid()'],
        'stateAction.reset':['syncBehaviorsState(!1,!0)'],
        'stateAction.apply':['.getBehavior(b)','lego-change-date-range'],
    };
    const candidates=name=>[...new Set(Object.values(window.default_datastudio||{}).filter(fn=>
        typeof fn==='function'&&matches(source(fn),EXPORTS[name])))];
    const exported=name=>single(name,candidates(name),fn=>fn);
    const memberBy=(contract,object,needles)=>{
        const names=[],seen=new Set();
        for(let proto=object;proto&&proto!==Object.prototype;proto=Object.getPrototypeOf(proto))
            for(const name of Object.getOwnPropertyNames(proto)){
                if(name==='constructor'||seen.has(name))continue;
                seen.add(name);
                const value=Object.getOwnPropertyDescriptor(proto,name).value;
                if(typeof value==='function'&&matches(source(value),needles))names.push(name);
            }
        return single(contract,names,name=>source(object[name]));
    };
    const member=(contract,object)=>memberBy(contract,object,MEMBERS[contract]);
    const invoke=(contract,object,...args)=>object[member(contract,object)](...args);
    const ownKey=(contract,object,test)=>single(contract,Object.keys(object||{}).filter(key=>{
        try{return test(object[key])}catch{return false}}),key=>key);
    const treeKey=model=>ownKey('blend.tree',model,value=>
        Array.isArray(value?.queries)&&Array.isArray(value?.joins));
    // Name of the state method returning namespaced saved layers: the same lookup
    // as getValue(key) over the other layer set, without unwrapping values.
    const layersMember=state=>{
        const shape=/=([\w$]+)\(this,a,([\w$]+)\(this,a\)\)/.exec(source(state.getValue))
            ||fail('state.layers','getValue shape changed');
        return memberBy('state.layers',state,[new RegExp(`\\{\\s*return ${escape(shape[1])
            }\\(this,a,(?!${escape(shape[2])}\\()[\\w$]+\\(this,a\\)\\)\\s*;?\\s*\\}$`)]);
    };
    const serializerMember=model=>{
        const tree=escape(treeKey(model));
        const view=source(model[memberBy('blend.serializer',model,
            [new RegExp(`return [\\w$.]+\\(this\\.${tree}\\)\\.blockDatasource\\s*;?\\s*\\}$`)])]);
        const serializer=capture('blend.serializer',view,
            new RegExp(`return ([\\w$.]+)\\(this\\.${tree}\\)\\.blockDatasource`));
        return memberBy('blend.serializer',model,[new RegExp(
            `\\{\\s*return ${escape(serializer)}\\(this\\.${tree}\\)\\s*;?\\s*\\}$`)]);
    };
    const unionsKey=selector=>capture('blend.unions',source(selector.constructor),
        /this\.([\w$]+)=[\w$.]+\.unionsEnabled\(\)/);
    // Editor suggestion trie: the only exported class with this method set.
    const formulaTrie=()=>single('formulaTrie',Object.values(window.default_datastudio||{}).filter(
        fn=>typeof fn==='function'&&['add','get','getValues','setValue','clear']
            .every(key=>typeof fn.prototype?.[key]==='function')),fn=>fn);
    // The formula parser is private to the calculated-field editor class: the one exported
    // class with getEditor() that keeps a suggestion trie. Its parse method (three module
    // calls on `this`: suggestions, parse, highlight) runs on a stand-in holding the
    // CodeMirror document (getEditor's field) and the caller's trie; the parsed tree is the
    // value it stores. Fields it only reads for suggestions are stubbed on demand.
    const formulaParse=(text,trie)=>{
        const fns=Object.values(window.default_datastudio||{}).filter(fn=>typeof fn==='function');
        const Trie=formulaTrie();
        const trieKey=single('formulaTrie',Object.keys(window.default_datastudio).filter(key=>
            window.default_datastudio[key]===Trie),key=>key);
        const created=new RegExp(`this\\.([\\w$]+)=new _\\.${escape(trieKey)}\\b`);
        const Editor=single('formula.editor',fns.filter(fn=>
            typeof fn.prototype?.getEditor==='function'&&created.test(source(fn))),fn=>fn);
        const doc=capture('formula.editor',source(Editor.prototype.getEditor),
            /^function\(\)\{return this\.([\w$]+)\}$/);
        const parse=memberBy('formula.parse',Editor.prototype,
            [/^function\(\)\{[\w$]+\(this\)(;[\w$]+\(this\)){2};?\}$/]);
        const empty={subscribe(observer){(observer?.next?.bind(observer)||observer)?.([]);
            return {unsubscribe(){}}},pipe(){return this}};
        const own={[doc]:{getDoc:()=>({isClean:()=>false,getValue:()=>text,
            changeGeneration:()=>1,getAllMarks:()=>[]})},
            [capture('formula.editor',source(Editor),created)]:trie,
            store:{select:()=>empty}};
        for(let attempt=0;attempt<8;attempt++){
            let tree=null,missing=null;
            const self=new Proxy(Object.create(Editor.prototype),{
                get:(target,key)=>key in own?own[key]
                    :typeof target[key]==='function'?target[key]:(missing=key,undefined),
                set:(target,key,value)=>{own[key]=value;
                    if(typeof value?.getChildren==='function'
                       &&typeof value?.getBackEndTextFormula==='function')tree=value;
                    return true}});
            try{self[parse]()}catch(error){
                if(tree)return tree;
                if(!(error instanceof TypeError)||missing===null||missing in own)
                    fail('formula.parse',String(error?.message||error));
                own[missing]={};continue;
            }
            return tree||fail('formula.parse','the editor stored no formula tree');
        }
        return fail('formula.parse','the editor needs fields the stand-in lacks');
    };
    // Method listing a formula leaf's highlighted tokens, calibrated on a keyword
    // leaf of a reference formula; every leaf class shares that method name.
    const formulaTokensKey=()=>{
        const stack=[formulaParse('CASE WHEN TRUE THEN 1 END',new (formulaTrie())())];
        while(stack.length){
            const node=stack.pop();
            const children=node?.getChildren?.()||[];
            if(!children.length&&node?.getBackEndTextFormula?.()==='END')
                return memberBy('formula.tokens',node,['position:this.getPosition()']);
            stack.push(...children);
        }
        return fail('formula.tokens','reference formula has no END leaf');
    };
    // The builder's first argument: the one service on the report/page models holding the
    // type registry and a data service that reads schemas, under the keys the builder uses.
    const componentProvider=roots=>{
        const text=source(exported('componentConfigBuilder'));
        const registry=capture('component.provider',text,/a\.([\w$]+)\.getDisplayTypeObj/);
        const data=capture('component.provider',text,/\(a\.([\w$]+),\{datasourceOrViewId:/);
        const values=roots.flatMap(root=>Object.values(root||{}));
        return single('component.provider',values.filter(value=>
            value&&typeof value==='object'&&
            typeof value[registry]?.getDisplayTypeObj==='function'&&
            value[data]&&typeof value[data]==='object'&&
            Object.values(value[data]).some(item=>typeof item?.getSchema==='function')),
            value=>value);
    };
    // The editor's property-panel service (Angular root service): the panel any model shows,
    // common sections included. Its class is the one export with this method set.
    const rootService=(contract,injector,test)=>{
        const Service=single(contract,Object.values(window.default_datastudio||{}).filter(fn=>
            typeof fn==='function'&&test(fn)),fn=>fn);
        const root=injector.has('$$angularLazyModuleRef')?
            injector.get('$$angularLazyModuleRef')?.injector:null;
        if(!root)fail(contract,'Angular root injector unavailable');
        return root.get(Service);
    };
    const panelService=injector=>rootService('panelService',injector,fn=>
        ['getPropertyLayoutConfig','getPropertyInputConfig','needData']
            .every(key=>typeof fn.prototype?.[key]==='function'));
    // The Insert menu's component-creation service: create({type, geometry, creationMethod,
    // datasourceId, presetConfigName, parent}) -> the new component model.
    const componentCreator=injector=>rootService('componentCreator',injector,fn=>
        ['creationMethod','isResponsiveLayout'].every(text=>source(fn.prototype?.create).includes(text)));
    // The registry's type dictionary: the one own object whose values are display types.
    const typeDictionary=registry=>registry[ownKey('typeDictionary',registry,value=>
        value&&typeof value==='object'&&!Array.isArray(value)&&
        Object.values(value).some(type=>typeof type?.getPresetConfigNames==='function'))];
    // Community visualization services (Angular root): the registration cache the
    // loader fills, and the factory building a viz's display type object from it.
    const vizRegistry=injector=>rootService('vizRegistry',injector,fn=>
        ['mutate','getStyleConfig'].every(key=>typeof fn.prototype?.[key]==='function'));
    const vizTypeFactory=injector=>rootService('vizTypeFactory',injector,fn=>
        source(fn).includes('this.themeService=a')&&
        /return new [\w$]+\(this\.themeService,/.test(source(fn.prototype?.get)));
    // The editor's default theme content (what a generated theme is laid over): the one
    // exported plain object carrying every theme color slot.
    const defaultTheme=()=>single('defaultTheme',Object.values(window.default_datastudio||{})
        .filter(value=>value&&typeof value==='object'&&!Array.isArray(value)&&
            ['themeTextboxVisitedHyperLinkFillColor','themeSeriesColor','themePaletteColors',
                'themeFillColor'].every(key=>Object.hasOwn(value,key))),value=>value);
    return {
        EXPORTS,MEMBERS,componentProvider,panelService,componentCreator,typeDictionary,
        defaultTheme,vizRegistry,vizTypeFactory,
        // Native default config of a new component: {config:{display,datasource:{keys}},
        // attribute, behavior, conceptDefs}. spec: {datasourceId,displayType,presetConfigName,geo}.
        componentConfig:(roots,spec)=>exported('componentConfigBuilder')(componentProvider(roots),spec),
        candidates,exported,member,layersMember,serializerMember,unionsKey,treeKey,
        formulaTrie,formulaTokensKey,formulaParse,
        schemaField:(schema,name)=>invoke('schema.field',schema,name),
        // Concept type code of a field model (0 text, 1 number, 3 boolean, 6 date):
        // the getter its own isBoolean() compares with 3.
        fieldTypeKey:field=>capture('field.type',source(field.isBoolean),/this\.([\w$]+)\(\)===3/),
        fieldType:field=>field[R.fieldTypeKey(field)](),
        // Concept data of a field model or of a plain concept object; null otherwise.
        conceptOf:value=>typeof value?.getQueryTimeTransformation==='function'
            ?invoke('field.concept',value)
            :value?.queryTimeTransformation?structuredClone(value):value?.proto||null,
        // First argument of the formula request builder: the holder whose cached
        // metadata it reads, answering "none" so revision 0 is requested.
        formulaMetadataKey:builder=>capture('formula.metadata',source(builder),
            /\ba\.([\w$]+)\.getMeta\(/),
        formulaBuilderContext:builder=>({[R.formulaMetadataKey(builder)]:{getMeta:()=>null}}),
        // Member of a service exposing getMeta(datasourceId).
        metadataKey:service=>ownKey('service.metadata',service,
            value=>typeof value?.getMeta==='function'),
        metadataHolder:service=>service[R.metadataKey(service)],
        resetControl:(service,component)=>invoke('stateAction.reset',service,component),
        applyAction:(service,component,key,value)=>
            invoke('stateAction.apply',service,component,key,value),
        stateLayers:(state,key)=>state[layersMember(state)](key),
        // Property-rule fields, read from the native combiner: which fields list the
        // callback arguments and which flag prepends the context object.
        ruleShape:()=>{
            const text=source(exported('propertyRuleCombiner'));
            const args=capture('rule.arguments',text,/[,=][\w$]+=[\w$]+\.([\w$]+)\.map\(function/);
            const list=capture('rule.arguments',text,
                new RegExp(`\\.${escape(args)}=\\[\\]\\.concat\\(([^;]*?)\\);`));
            return {arguments:[...list.matchAll(/subConfig\.([\w$]+)/g)].map(match=>match[1]),
                context:capture('rule.context',text,/subConfig\.([\w$]+)&&[\w$]+\.unshift\(/)};
        },
        blendTree:model=>model[treeKey(model)],
        serializeBlend:model=>model[serializerMember(model)](),
        unionsEnabled:selector=>selector[unionsKey(selector)],
    };
})();"""


def native_script(script: str) -> str:
    """Give a page function access to `R`; `this` and the single argument pass through."""
    return f"function(args){{{RESOLVER}\nreturn ({script}).call(this,args);}}"


CONTRACTS = """() => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService'))return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport();
    if(!report)return {error:'EDITOR_NOT_READY'};
    const results=[];
    const check=(contract,subject,resolve)=>{
        if(!subject)return results.push({contract,status:'not_testable',
            detail:'the open report has no live object of this kind'});
        try{results.push({contract,status:'ok',resolved:resolve()})}
        catch(error){results.push({contract,status:'changed',detail:String(error?.message||error)})}
    };
    for(const name of Object.keys(R.EXPORTS))
        check('export.'+name,true,()=>{R.exported(name);return 'unique'});
    const service=name=>i.has(name)?i.get(name):null;
    const actions=service('stateActionService');
    check('stateAction.reset',actions,()=>R.member('stateAction.reset',actions));
    check('stateAction.apply',actions,()=>R.member('stateAction.apply',actions));
    const blendSchema=service('blendSchemaService');
    check('service.metadata',blendSchema,()=>R.metadataKey(blendSchema));
    check('formula.metadata',true,()=>R.formulaMetadataKey(R.exported('formulaRequestBuilder')));
    check('rule.shape',true,()=>R.ruleShape());
    check('formula.parse',true,()=>{R.formulaParse('1',new (R.formulaTrie())());return 'unique'});
    check('formula.tokens',true,()=>R.formulaTokensKey());
    check('panelService',true,()=>{R.panelService(i);return 'unique'});
    check('componentCreator',true,()=>{R.componentCreator(i);return 'unique'});
    check('defaultTheme',true,()=>{R.defaultTheme();return 'unique'});
    check('vizRegistry',true,()=>{R.vizRegistry(i);return 'unique'});
    check('vizTypeFactory',true,()=>{R.vizTypeFactory(i);return 'unique'});
    check('typeDictionary',true,()=>
        `${Object.keys(R.typeDictionary(i.get('legoMainRegistry'))).length} types`);
    const firstPage=report.getPages()[0];
    check('component.provider',firstPage,
        ()=>{R.componentProvider([firstPage,report]);return 'unique'});
    const Selector=R.candidates('joinTypeSelector')[0];
    check('blend.unions',Selector,()=>R.unionsKey(new Selector(null)));
    const components=report.getPages().flatMap(page=>page.getComponents());
    const bound=components.find(c=>
        c.isReady$?.value===true&&c.getDisplayTypeObj?.().getNeedData?.());
    const schema=bound?.getSchema?.();
    check('state.layers',bound,()=>R.layersMember(bound.getState()));
    check('schema.field',schema&&!schema.isDummy(),()=>R.member('schema.field',schema));
    const field=schema&&!schema.isDummy()?(schema.getConcepts()||[]).find(f=>f?.isValid?.()):null;
    check('field.type',field,()=>R.fieldTypeKey(field));
    check('field.concept',field,()=>R.member('field.concept',field));
    const blend=Object.values(report.getResourcesByKey('dataViewResource')||{})[0];
    const Model=R.candidates('blendModel')[0];
    const model=blend&&Model?new Model(structuredClone(blend),''):null;
    check('blend.tree',model,()=>R.treeKey(model));
    check('blend.serializer',model,()=>R.serializerMember(model));
    return {report_id:report.getId(),contracts:results,
        scope:'Resolution of each native capability against the loaded bundle. Read-only: '
            +'names are resolved, nothing is dispatched, saved or reset.'};
}"""


class NativeContracts:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def check(self) -> dict[str, Any]:
        session = self.studio.session
        page = await session.page()
        try:
            result = await asyncio.wait_for(
                page.evaluate(native_script(CONTRACTS)), timeout=session.config.timeout_ms / 1000
            )
        except Exception as exc:
            raise StudioError("NATIVE_CONTRACT_FAILED", f"Contract probe failed: {exc}") from exc
        if result.get("error"):
            raise StudioError(result["error"], "Report editor model is not loaded")
        changed = [item["contract"] for item in result["contracts"] if item["status"] == "changed"]
        return {**result, "healthy": not changed, "changed": changed}
