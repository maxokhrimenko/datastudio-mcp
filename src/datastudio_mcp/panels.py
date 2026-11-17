"""Every editor property panel, for the report, a page or a component, read and written natively.

The editor builds each panel from its property-panel service: tabs, sections and inputs,
common sections (background, border, header, data source, filters) included, each input
with its binding. Reading lists exactly what a human sees. Writing applies the inputs'
own bindings to the loaded draft model in View (which never autosaves), serializes it
with the native serializer, overlays only that delta on the saved JSON and sends one
journalled write; the model is then reloaded, so a failed write leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .browser import StudioError
from .document import find_page, fingerprint, walk_components
from .nativeops import NativeOps
from .report_resources import report_content

if TYPE_CHECKING:
    from .service import Studio


class Target(BaseModel):
    """What owns the panel: the report, a page, or a component (any depth) on a page."""

    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["report", "page", "component"]
    page_id: str | None = None
    component_id: str | None = None

    @model_validator(mode="after")
    def check(self) -> Target:
        if (self.kind == "report") != (self.page_id is None):
            raise ValueError("page and component targets need page_id; the report has none")
        if (self.kind == "component") != (self.component_id is not None):
            raise ValueError("component_id is required exactly for kind=component")
        return self


class SettingChange(BaseModel):
    """A panel input (key from settings_describe), or with display_key=true a display key of
    the model that no panel input edits: a text box's HTML "text", an embed's "contentUrl",
    a button's "text"/"linkUrl", a shape's "shapeType", an arrow's "terminals"."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    key: str = Field(min_length=1)
    value: JsonValue
    display_key: bool = False


class ComponentSettingChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    component_id: str = Field(min_length=1)
    changes: list[SettingChange]


def target_args(target: Target) -> dict[str, Any]:
    return {"kind": target.kind, "pageId": target.page_id, "componentId": target.component_id}


class Panels:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def _run(self, args: dict[str, Any]) -> dict[str, Any]:
        return await NativeOps(self.studio).evaluate(_PANEL, args)

    async def describe(
        self,
        target: Target,
        search: str = "",
        offset: int = 0,
        limit: int = 60,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 200:
            raise StudioError("INVALID_PAGINATION", "offset >= 0 and 1 <= limit <= 200")
        raw = await self.studio.report()
        version = self._version(raw, target)
        result = await self._run(
            {
                **target_args(target),
                "mode": "describe",
                "version": version,
                "search": search,
                "includeHidden": include_hidden,
            }
        )
        items = result.pop("items")
        result.update(
            items=items[offset : offset + limit],
            total=len(items),
            next_offset=offset + limit if offset + limit < len(items) else None,
            fingerprint=self._saved(raw, target)[1],
        )
        return result

    @staticmethod
    def _version(raw: dict[str, Any], target: Target) -> str:
        config = raw["reportConfig"]
        if target.kind == "report":
            return str(config["version"])
        return str(find_page(raw, target.page_id)["version"])

    @staticmethod
    def _saved(raw: dict[str, Any], target: Target) -> tuple[dict[str, Any], str]:
        """Saved content the write replaces, and the fingerprint guarding it."""
        if target.kind == "report":
            content = report_content(raw["reportConfig"])
            return content, fingerprint(content)
        frame = find_page(raw, target.page_id)
        return frame, fingerprint(frame["page"])

    async def set(
        self,
        target: Target,
        changes: list[SettingChange],
        expected_fingerprint: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not 1 <= len(changes) <= 100:
            raise StudioError("INVALID_BATCH", "Provide 1-100 property changes")
        return await NativeOps(self.studio).run(
            _PANEL,
            {**target_args(target), "mode": "set", "changes": [c.model_dump() for c in changes]},
            page_id=target.page_id,
            expected_fingerprint=expected_fingerprint,
            dry_run=dry_run,
            plan={"target": target.model_dump()},
        )


    async def describe_page(
        self, page_id: str, component_ids: list[str] | None, search: str = "",
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        """settings_describe for many components of one page in a single pass."""
        raw = await self.studio.report()
        frame = find_page(raw, page_id)
        known = [c["componentId"]
                 for c in walk_components(frame["page"].get("componentConfig", []))]
        ids = known if component_ids is None else component_ids
        missing = [i for i in ids if i not in known]
        if missing:
            raise StudioError("NOT_FOUND", f"Not on this page: {missing}")
        result = await NativeOps(self.studio).evaluate(_PANEL_PAGE, {
            "kind": "component", "pageId": page_id, "componentIds": ids, "mode": "describe",
            "version": str(frame["version"]), "search": search, "includeHidden": include_hidden})
        return {"page_id": page_id, "fingerprint": fingerprint(frame["page"]),
                "components": result["components"]}

    async def set_batch(
        self,
        page_id: str,
        components: list[ComponentSettingChanges],
        expected_fingerprint: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """settings_set for many components of one page: applied one after another on the
        live model and saved as ONE write (one read-back, one reload)."""
        ids = [c.component_id for c in components]
        if not 1 <= len(ids) <= 200 or len(set(ids)) != len(ids):
            raise StudioError("INVALID_BATCH", "Provide 1-200 distinct components")
        steps = []
        for item in components:
            if not 1 <= len(item.changes) <= 100:
                raise StudioError("INVALID_BATCH", "Provide 1-100 changes per component")
            target = Target(kind="component", page_id=page_id, component_id=item.component_id)
            steps.append({**target_args(target), "mode": "set",
                          "changes": [c.model_dump() for c in item.changes]})
        return await NativeOps(self.studio).run(
            _PANEL, steps, page_id=page_id, expected_fingerprint=expected_fingerprint,
            dry_run=dry_run, plan={"page_id": page_id, "components": ids})

    async def image(
        self, page_id: str, component_id: str, path: str, expected_fingerprint: str,
        *, dry_run: bool = True,
    ) -> dict[str, Any]:
        """Upload a local image into an image component, as its panel's file picker does."""
        file = await asyncio.to_thread(lambda: Path(path).expanduser())
        kind = mimetypes.guess_type(file.name)[0]
        try:
            data = await asyncio.to_thread(file.read_bytes)
        except OSError as exc:
            raise StudioError("INVALID_IMAGE", "Image file is not readable") from exc
        if kind not in IMAGE_TYPES:
            raise StudioError("INVALID_IMAGE", f"Supported types: {', '.join(IMAGE_TYPES)}")
        if len(data) > 25_000_000:
            raise StudioError("INVALID_IMAGE", "Images must be smaller than 25 MB")
        if dry_run:
            return {"status": "planned", "file": file.name, "type": kind, "bytes": len(data)}
        return await NativeOps(self.studio).run(
            _IMAGE,
            {"componentId": component_id, "name": file.name, "type": kind,
             "bytes": base64.b64encode(data).decode()},
            page_id=page_id,
            expected_fingerprint=expected_fingerprint,
            dry_run=False,
            plan={"component_id": component_id, "file": file.name},
        )


IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/svg+xml", "image/webp")

_IMAGE = r"""async args => {
    const X=OPS(args);
    const model=(X.owner.getComponentsRecursive?.()||X.owner.getComponents())
        .find(c=>c.getId()===args.componentId);
    if(model?.getDisplayType()!=='image-component')X.stop('NOT_FOUND','image component');
    const before=X.serialize();
    const input=(await R.panelService(X.i).getPropertyInputConfig(model)).file;
    if(typeof input?.bindingMethods?.writeToConfig!=='function')
        X.stop('PROPERTY_NOT_WRITABLE','file');
    const bytes=Uint8Array.from(atob(args.bytes),c=>c.charCodeAt(0));
    // Encoded exactly as the panel's file picker does before uploading.
    const data=R.exported('base64Encoder')(bytes,3);
    await input.bindingMethods.writeToConfig(model,{...input,key:'file',
        value:{data,name:args.name,type:args.type}});
    const file=X.clean(model.getDisplayKeyValue('file'));
    if(!file?.id)X.stop('UPLOAD_FAILED','the image service returned no id');
    return {before,after:X.serialize(),file};
}"""


_PANEL = r"""async ({reportId,kind,pageId,componentId,mode,version,search,changes,
        includeHidden,continuing}) => {
    const i=window.angular?.element(document.body).injector();
    if(!i?.has('reportContextService'))return {error:'EDITOR_NOT_READY'};
    const report=i.get('reportContextService').getReport();
    if(report?.getId()!==reportId)return {error:'OUT_OF_SCOPE'};
    if(report.isReady$?.value!==true)return {error:'EDITOR_NOT_READY'};
    const owner=pageId===null?null:report.getPage(pageId);
    if(pageId!==null&&!owner)return {error:'NOT_FOUND',details:'page'};
    if(owner&&owner.isReady$?.value!==true)return {error:'EDITOR_NOT_READY',details:'page'};
    const current=String((owner||report).getVersion());
    if(!/^[0-9]+$/.test(current))return {error:'EDITOR_STALE',details:'draft model not loaded'};
    if(current!==version)return {error:'EDITOR_STALE',details:'use report_refresh'};
    const model=kind==='report'?report:kind==='page'?owner:
        (owner.getComponentsRecursive?.()||owner.getComponents()).find(c=>c.getId()===componentId);
    if(!model)return {error:'NOT_FOUND',details:'component'};
    if(model.isReady$&&model.isReady$.value!==true&&typeof model.getReadyAsync==='function')
        await model.getReadyAsync();
    const dirty=m=>Object.values(m?.isChanged||{}).some(Boolean);
    if(!continuing&&[report,owner,model].some(dirty))return {error:'EDITOR_DIRTY'};
    const panel=R.panelService(i);
    const clean=v=>{try{return v===undefined?null:JSON.parse(JSON.stringify(v,(k,x)=>
        typeof x==='function'?undefined:x instanceof Map?Object.fromEntries(x):
        x instanceof Set?[...x]:x))}catch{return null}};
    const equal=(a,b)=>JSON.stringify(clean(a))===JSON.stringify(clean(b));
    // Native bindings may complete a value with defaults (a color gains opacity): every
    // requested member must hold, extra native members are allowed.
    const holds=(want,got)=>{
        // A picked field is its concept: the read-back may omit display details of it.
        if(want&&got&&typeof want==='object'&&typeof got==='object'&&'name' in want&&
                'namespace' in want&&'name' in got)
            return want.name===got.name&&want.namespace===(got.namespace??want.namespace);
        if(want&&typeof want==='object'&&!Array.isArray(want))
            // A color stored without opacity is opaque: {opacity:1} holds against it.
            return got&&typeof got==='object'&&Object.keys(want).every(k=>
                k==='opacity'&&want[k]===1&&got[k]===undefined||holds(want[k],got[k]));
        // An emptied picker reads back as nothing selected.
        if(Array.isArray(want)&&!want.length&&got==null)return true;
        if(Array.isArray(want))return Array.isArray(got)&&want.length===got.length&&
            want.every((v,i)=>holds(v,got[i]));
        return equal(want,got);
    };
    const read=(input,m)=>{try{return clean(input.bindingMethods?.readFromConfig?.(m))}
        catch{return null}};
    // The next sort slot is hidden until "Add sort"; the UI opens sortConceptN once
    // sortConcept(N-1) is set, so it is writable then (the adder only fills the panel).
    const openSlot=(inputs,key,m)=>{
        const slot=/^sortConcept(\d+)$/.exec(key);
        if(!slot||slot[1]==='1')return false;
        const prior=inputs['sortConcept'+(Number(slot[1])-1)];
        const value=prior?read(prior,m):null;
        return Array.isArray(value)&&value.length>0;
    };
    // The component's own bindings; the theme resource is shared by every themed input.
    const bound=(inputs,key)=>(inputs[key]?.bindingMethods?.bindingKeys||[])
        .filter(k=>k.type!=='resource').map(k=>k.type+'|'+k.key);
    // An input disabled by a sibling on the same binding (Date range picker while the
    // Auto/Custom choice reads Auto) opens when that sibling is written, as in the UI.
    const siblingOpens=(inputs,key)=>Object.keys(inputs).some(other=>other!==key&&
        Array.isArray(inputs[other]?.options)&&inputs[other]?.disabled!==true&&
        typeof inputs[other]?.bindingMethods?.writeToConfig==='function'&&
        bound(inputs,other).some(k=>bound(inputs,key).includes(k)));
    // Panel-only choices (scorecard Dimension/Metric) write nothing; the panel enables
    // other inputs from them through enabledBy {<dependency keys>, callback}, as the UI
    // does on click. ui holds the choices this request made.
    const ui={};
    const uiOnly=b=>/^function\(\)\{\s*\}$/.test(Function.prototype.toString.call(b.writeToConfig));
    const enabledNow=(inputs,key)=>{
        const rule=inputs[key]?.enabledBy;
        if(typeof rule?.callback!=='function')return false;
        const deps=Object.values(rule).find(v=>Array.isArray(v)&&v.every(x=>typeof x==='string'));
        // Only rules over inputs are replayed; others read editor context we do not have.
        if(!deps||!deps.every(d=>Object.hasOwn(ui,d)||inputs[d]))return false;
        try{return rule.callback(...deps.map(d=>Object.hasOwn(ui,d)?ui[d]:read(inputs[d],model)))===true}
        catch{return false}
    };
    // Describe: an input a panel-only choice can enable is writable together with it.
    const choiceOpens=(inputs,key)=>{
        const rule=inputs[key]?.enabledBy;
        const deps=rule&&Object.values(rule).find(v=>Array.isArray(v)&&v.every(x=>typeof x==='string'));
        return !!deps&&deps.some(d=>inputs[d]?.bindingMethods?.writeToConfig&&uiOnly(inputs[d].bindingMethods));
    };
    // Describe: the next free numbered slot (referenceLineType2) opens by its section's Add
    // button (referenceLineAdd), so it is writable together with that click. The button
    // finds that slot in the display key it reads.
    const textBox=(m,inputs)=>m.getDisplayType?.()==='simple-description'&&!Object.hasOwn(inputs,'text');
    const adderOpens=(inputs,key)=>{
        const n=Number(/(\d+)$/.exec(key)?.[1]);
        return n>0&&Object.entries(inputs).some(([k,v])=>{
            if(v?.type!=='actionButton'||v.disabled===true||!/Add$/.test(k)||
                !key.startsWith(k.slice(0,-3))||/Band/.test(key)!==/Band/.test(k))return false;
            const slots=/getDisplayKeyValue\("(\w+)"\)/.exec(Function.prototype.toString.call(v.action))?.[1];
            if(!slots)return false;
            let list;try{list=model.getDisplayKeyValue(slots)}catch{return false}
            let free=0;while(free<10&&list?.[free]!=null)free++;
            return n===free+1;
        });
    };
    // The theme picker's values are native objects whose id field is minified; they are
    // shown and taken as {id,isModernTheme} and matched to the picker's own options.
    const themeView=v=>{
        const k=v&&typeof v==='object'?Object.keys(v).find(k=>k!=='isModernTheme'&&typeof v[k]==='string'):null;
        return k?{id:v[k],isModernTheme:!!v.isModernTheme}:v;
    };
    const shown=(key,v)=>key==='theme'?themeView(v):v;
    if(mode==='describe'){
        let layout=null;try{layout=clean(panel.getPropertyLayoutConfig(model))}catch{}
        const inputs=await panel.getPropertyInputConfig(model);
        // Layout: tabs -> sections whose children are rows (cells) or nested sections.
        const where={};
        const walk=(tab,node,path)=>{
            const label=node.label||(node.name&&!/^__PH_|^style|Section\d*$/.test(node.name)?
                node.name:null);
            const here=label?[...path,label]:path;
            for(const cell of node.cells||[])where[cell.key]??={tab,section:here.join(' › ')||null};
            for(const child of [...(node.rows||[]),...(node.children||[])])walk(tab,child,here);
        };
        for(const [tab,body] of Object.entries(layout?.tabs||{}))
            for(const section of body.sections||[])walk(tab,section,[]);
        const needle=(search||'').toLowerCase();
        const items=Object.entries(inputs).filter(([key,input])=>input.type!=='label'&&
            (includeHidden||Object.hasOwn(where,key))&&
            [key,clean(input.label),clean(input.tooltip),where[key]?.section].join(' ')
                .toLowerCase().includes(needle)).map(([key,input])=>{
            const b=input.bindingMethods||{};
            return {key,label:clean(input.label||input.tooltip||input.name),control:input.type,
                tab:where[key]?.tab||null,section:where[key]?.section||null,value:shown(key,read(input,model)),
                options:Array.isArray(input.options)?input.options.map(o=>({name:clean(o.name),
                    value:clean(shown(key,o.value))})):null,
                min:input.min??null,max:input.max??null,step:input.step??null,
                bindings:clean(b.bindingKeys),
                writable:input.type==='actionButton'?typeof input.action==='function'&&
                    input.disabled!==true:typeof b.writeToConfig==='function'&&
                    (input.disabled!==true||openSlot(inputs,key,model)||siblingOpens(inputs,key)||
                        choiceOpens(inputs,key)||adderOpens(inputs,key)),
                // writable but not enabled: it opens only once another change in the same
                // request is written (a sibling choice, an Add button, the previous slot).
                enabled:input.disabled!==true,
                dynamic_options:typeof input.dynamicOptions==='function'};
        });
        // A text box's content is typed on the canvas, not in the panel: shown as `text`.
        if(textBox(model,inputs)&&'text'.includes(needle))
            items.unshift({key:'text',label:'Text (HTML)',control:'richtext',tab:null,section:null,
                value:clean(model.getDisplayKeyValue('text')),options:null,min:null,max:null,
                step:null,bindings:[{type:'display',key:'text'}],writable:true,enabled:true,
                dynamic_options:false});
        // Numbered slots (Dimension #3, Metric #2, Sort #4, colColor7, ...) exist up to the
        // editor's maximum; the panel shows only those in use plus the next free one.
        const count=role=>{
            try{return (model.getConcepts?.(role)||[]).flat().length}catch{return 0}};
        const roles={dimension:count('dimensions'),metric:count('metrics'),series:count('metrics'),
            column:count('metrics')};
        const slot=item=>{
            const m=/(?:^|› )([^›#]+?) #(\d+)/.exec(item.section||'');
            return m?{group:item.tab+'|'+m[1].trim(),n:Number(m[2])}:null;
        };
        const used={};
        for(const item of items){
            const s=slot(item);
            if(s&&item.value!==null&&/chiplist/i.test(item.control||''))
                used[s.group]=Math.max(used[s.group]||0,s.n);
        }
        const limit=group=>{
            const stem=group.split('|')[1].toLowerCase();
            const role=Object.keys(roles).find(r=>stem.startsWith(r));
            return role!==undefined?Math.max(roles[role],used[group]||0):(used[group]||0)+1;
        };
        const visible=includeHidden?items:items.filter(item=>{
            const s=slot(item);
            if(s)return s.n<=limit(s.group);
            const m=/(\d+)$/.exec(item.key);
            if(!m||!item.section)return true;
            const groups=Object.keys(used).filter(g=>g.startsWith(item.tab+'|'+item.section));
            return !groups.length||Number(m[1])<=Math.max(...groups.map(limit));
        });
        return {kind,type:model.getDisplayType?.()||kind,
            preset:model.getPresetConfigName?.()||null,
            tabs:Object.fromEntries(Object.entries(layout?.tabs||{}).map(([tab,body])=>
                [tab,(body.sections||[]).map(s=>s.label||s.name)])),
            items:visible,editor_version:current};
    }
    const registry=i.get('legoMainRegistry');
    const serialize=()=>{
        if(kind==='report')return clean(R.exported('reportConfigBuilder')(
            report.getConfig(),registry,undefined));
        return clean(R.exported('pageConfigBuilder')(owner.getConfig(),{displayName:'draft'},
            undefined,registry).page);
    };
    const before=serialize();
    // Errors the report already has (an invalid page filter, ...) are not this edit's.
    const errorsOf=()=>{if(kind!=='component')return {};model.validate?.();
        return clean(model.getConfigErrors?.()||{})||{}};
    const priorErrors=errorsOf();
    const properties=[];
    const themeOwner=kind==='report'?report:null;
    const deferred=[],written=new Set();
    // A request is order-free, as clicking in the UI: an input disabled until another
    // change of the request lands (delta colors until a comparison range is set) is
    // retried once after all the others.
    let queue=[...changes.entries()],postponed=[],finalPass=false;
    while(queue.length){
    for(let [index,{key,value,display_key}] of queue){
        if(key==='text'&&!display_key&&kind==='component'&&
                textBox(model,await panel.getPropertyInputConfig(model)))display_key=true;
        if(display_key){
            if(kind==='report'&&key==='themeConfig')return {error:'PROPERTY_REQUIRES_INPUT',
                details:'use the "theme" setting'};
            const known=new Set([...Object.keys(model.getConfig()?.config?.display?.keys||{}),
                ...Object.keys(clean(model.getDefaultDisplayKeys?.())||{})]);
            if(!known.has(key))return {error:'UNKNOWN_DISPLAY_KEY',details:key};
            const prior=clean(model.getDisplayKeyValue(key));
            model.setDisplayKeyValue(key,structuredClone(value));
            model.syncState?.();model.syncDisplayState?.();
            const actual=clean(model.getDisplayKeyValue(key));
            if(!holds(value,actual))return {error:'PROPERTY_VALUE_NOT_APPLIED',
                details:{key,requested:value,actual}};
            properties.push({key,display_key:true,before:prior,after:actual});
            continue;
        }
        const inputs=await panel.getPropertyInputConfig(model);
        const input=Object.hasOwn(inputs,key)?inputs[key]:null,b=input?.bindingMethods;
        // A panel button (Add reference line, Add band): true clicks it. It fills the next
        // free slot with the panel's defaults, which opens that slot's inputs.
        if(input?.type==='actionButton'&&typeof input.action==='function'){
            if(value!==true)return {error:'INVALID_PROPERTY_VALUE',details:key};
            if(input.disabled===true){
                if(!finalPass){postponed.push([index,changes[index]]);continue;}
                return {error:'PROPERTY_DISABLED',details:key};
            }
            // The action only sets panel values; the panel then writes each changed one,
            // as it does for any edited input.
            const values=Object.fromEntries(Object.entries(inputs).map(([k,v])=>[k,v?.value]));
            try{
                const pending=input.action();
                if(pending&&typeof pending.then==='function')await pending;
                for(const [k,v] of Object.entries(inputs)){
                    if(k===key||v?.value===values[k]||!v?.bindingMethods?.writeToConfig)continue;
                    const event={...v,key:k,value:v.value,oldValue:values[k]};
                    const done=v.bindingMethods.writeToConfig(model,event);
                    if(done&&typeof done.then==='function')await done;
                    v.bindingMethods.onConfigChange?.affectOtherConfig?.(model,event);
                }
                model.syncState?.();model.syncDisplayState?.();
            }catch(error){
                return {error:'PROPERTY_WRITE_FAILED',details:{key,message:String(error?.message||error)}};
            }
            properties.push({key,action:true});
            continue;
        }
        if(!b?.writeToConfig||!b?.readFromConfig)return {error:'PROPERTY_NOT_WRITABLE',details:key};
        if(input.disabled===true&&!openSlot(inputs,key,model)&&!enabledNow(inputs,key)&&
                !bound(inputs,key).some(k=>written.has(k))){
            if(!finalPass){postponed.push([index,changes[index]]);continue;}
            return {error:'PROPERTY_DISABLED',details:key};
        }
        for(const k of bound(inputs,key))written.add(k);
        if(uiOnly(b))ui[key]=value;
        if(input.type==='toggleswitch'&&typeof value!=='boolean')
            return {error:'INVALID_PROPERTY_VALUE',details:key};
        if(key==='theme'&&value&&typeof value==='object'&&'id' in value){
            const option=(input.options||[]).find(o=>themeView(o.value)?.id===value.id);
            if(!option)return {error:'INVALID_PROPERTY_OPTION',details:key};
            value=option.value;
        }
        if(Array.isArray(input.options)&&input.options.length&&
            !input.options.some(o=>equal(clean(o.value),clean(value))))
            return {error:'INVALID_PROPERTY_OPTION',details:key};
        // Theme inputs edit the report's custom theme; a built-in theme is first copied
        // into one, as the editor's Customize does.
        if(themeOwner&&b.bindingKeys?.some(k=>k.type==='resource'&&k.key==='theme')&&key!=='theme'){
            const themes=report.getResourcesByKey('theme')||[];
            const active=report.getDisplayKeyValue('themeConfig');
            if(!themes.some(theme=>theme.id===active)){
                const content=R.exported('themeResolver')(panel.reportType.themeService,report);
                const id=R.exported('themeStore')(panel.reportType.themeService,report,
                    structuredClone(content));
                report.setDisplayKeyValue('themeConfig',id);
                report.syncDisplayState?.();
            }
        }
        // A field picker takes field names, as picking a field in the panel does: a
        // datasource field (or concept) name resolves to the component's own concept.
        // One field may be given bare, as the single-select picker shows one chip.
        if(/^legochiplist/.test(input.type||'')&&typeof value==='string')value=[value];
        if(/^legochiplist/.test(input.type||'')&&Array.isArray(value)&&
                value.some(v=>typeof v==='string')){
            const defs=Object.values(model.getConfig?.()?.conceptDefs||{});
            const resolved=[];
            for(const v of value){
                if(typeof v!=='string'){resolved.push(v);continue;}
                // A field can back several concepts (chart axis, date range): prefer the one
                // the component shows as a dimension or metric.
                const shown=new Set(['dimensions','metrics'].flatMap(role=>
                    Object.values(model.getDataSourceKeyValue?.(role)||{}).flat()));
                // A field ID (_payment_type_) or the name the user sees in the panel
                // (Payment type, any case, when only one field has it).
                let id=v;
                let known=false;
                try{known=!!R.schemaField(model.getSchema(),v)?.isValid?.()}catch{}
                if(!known)try{
                    // The schema also lists the component's own concepts under their
                    // field's name: a concept counts as the field it reads.
                    const ids=new Set((model.getSchema().getConcepts()||[]).filter(f=>
                        f?.isValid?.()&&String(f.getDisplayName?.()).toLowerCase()===
                            v.toLowerCase()).map(f=>{
                        const def=defs.find(d=>d?.name===f.getName());
                        return def?.queryTimeTransformation?.dataTransformation
                            ?.sourceFieldName||f.getName();
                    }));
                    if(ids.size===1)id=[...ids][0];
                }catch{}
                const same=defs.filter(d=>[v,id].includes(
                    d?.queryTimeTransformation?.dataTransformation?.sourceFieldName));
                let def=defs.find(d=>d?.name===v)||same.find(d=>shown.has(d.name))||same[0];
                if(!def){
                    // A field of the datasource the component does not use yet: picking it
                    // in the panel adds it to the component as a new concept.
                    let valid=false;
                    try{valid=R.schemaField(model.getSchema(),id).isValid()}catch{}
                    if(!valid)return {error:'FIELD_NOT_ON_COMPONENT',details:{key,field:v,
                        hint:'not a field of the component datasource'}};
                    const namespace=defs[0]?.namespace||'t0';
                    const name='qt_'+Math.random().toString(36).slice(2,12);
                    def={name,namespace,queryTimeTransformation:{dataTransformation:{sourceFieldName:id}}};
                    model.getConfig().conceptDefs[name]=def;
                }
                resolved.push({id:`${def.namespace}.${def.name}`,name:def.name,
                    namespace:def.namespace,
                    ...(def.queryTimeTransformation?{queryTimeTransformation:
                        structuredClone(def.queryTimeTransformation)}:{})});
            }
            value=resolved;
        }
        const prior=read(input,model);
        // A palette takes every slot: shorter lists change the leading colors only.
        if(input.type==='multicolorpicker'&&Array.isArray(value)&&Array.isArray(prior)&&
            value.length<prior.length)
            // A color given without themeRef is a custom color: it replaces the slot's
            // theme reference (which would otherwise render the theme's color).
            value=prior.map((slot,index)=>{
                if(index>=value.length)return slot;
                const given=value[index],{themeRef,...custom}=slot||{};
                return {...(given&&'color' in given&&!('themeRef' in given)?custom:slot),...given};
            });
        const event={...input,key,value,oldValue:prior};
        // A native throw names the change it came from, so a caller can drop just that one.
        try{
            const pending=b.writeToConfig(model,event);
            if(pending&&typeof pending.then==='function')await pending;
            b.onConfigChange?.affectOtherConfig?.(model,event);
            model.syncState?.();model.syncDisplayState?.();
        }catch(error){
            return {error:'PROPERTY_WRITE_FAILED',details:{key,message:String(error?.message||error)}};
        }
        let fresh=(await panel.getPropertyInputConfig(model))[key];
        let actual=fresh?read(fresh,model):read(input,model);
        // Some inputs (conditional formatting) write what their dialog holds, not the
        // value given; when the input is bound to one display key, set that key itself.
        const keys=b.bindingKeys||[];
        if(!holds(value,actual)&&keys.length===1&&keys[0].type==='display'){
            model.setDisplayKeyValue(keys[0].key,structuredClone(value));
            model.syncState?.();model.syncDisplayState?.();
            fresh=(await panel.getPropertyInputConfig(model))[key];
            actual=fresh?read(fresh,model):read(input,model);
        }
        if(!holds(value,actual)){
            // A derived choice (Date range Auto/Custom, scorecard Dimension/Metric) reads
            // what later changes of the request write; it is checked once all are in.
            if(!changes.slice(index+1).some(later=>!later.display_key)&&!postponed.length)
                return {error:'PROPERTY_VALUE_NOT_APPLIED',details:{key,requested:value,actual}};
            deferred.push({key,value,input,at:properties.length,prior});
        }
        properties.push({key,before:shown(key,prior),after:shown(key,actual)});
    }
    if(finalPass||!postponed.length)break;
    queue=postponed;postponed=[];finalPass=true;
    }
    for(const {key,value,input,at,prior} of deferred){
        const fresh=(await panel.getPropertyInputConfig(model))[key];
        const actual=fresh?read(fresh,model):read(input,model);
        if(!holds(value,actual))return {error:'PROPERTY_VALUE_NOT_APPLIED',
            details:{key,requested:value,actual}};
        properties[at]={key,before:shown(key,prior),after:shown(key,actual)};
    }
    if(kind==='component'){
        const errors=errorsOf(),added={};
        for(const [scope,found] of Object.entries(errors||{}))
            for(const [k,v] of Object.entries(found||{}))
                if(!equal(priorErrors?.[scope]?.[k],v))(added[scope]??={})[k]=v;
        if(Object.keys(added).length)
            return {error:'INVALID_COMPONENT_CONFIGURATION',details:added};
    }
    return {before,after:serialize(),properties};
}"""


# Every component of a page described in one page call (one tab acquire, one evaluate).
_PANEL_PAGE = "async args => { const one = (" + _PANEL + """);
    const components = {};
    for (const id of args.componentIds) {
        const r = await one({...args, componentId: id});
        components[id] = r.error ? {error: r.error, details: r.details ?? null}
            : {type: r.type, items: r.items};
    }
    return {components};
}"""
