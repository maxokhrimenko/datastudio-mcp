"""The editor's own catalog of everything configurable, for the tracked UI inventory.

Every component type and preset the Insert menu can create is created on the loaded
draft model in View (never saved), and its property panel is read through the editor's
panel service: tabs, sections and every input with label, control, options and range.
The report and page panels are read the same way.
The result is deterministic (no values, no IDs), so a stored snapshot diffed after a
Google release shows exactly which settings or types appeared, changed or vanished.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .browser import StudioError
from .nativeops import NativeOps

if TYPE_CHECKING:
    from .service import Studio

_CATALOG = r"""async args => {
    const X=OPS(args),{owner,report,registry,clean}=X;
    const panel=R.panelService(X.i);
    const creator=R.componentCreator(X.i);
    const label=v=>typeof v==='string'?v:clean(v);
    const describe=async model=>{
        let layout={};try{layout=clean(panel.getPropertyLayoutConfig(model))||{}}catch{}
        const inputs=await panel.getPropertyInputConfig(model);
        const where={};
        const walk=(tab,node,path)=>{
            const name=node.label||(node.name&&!/^__PH_|^style|Section\d*$/.test(node.name)?
                node.name:null);
            const here=name?[...path,name]:path;
            for(const cell of node.cells||[])where[cell.key]??={tab,section:here.join(' › ')};
            for(const child of [...(node.rows||[]),...(node.children||[])])walk(tab,child,here);
        };
        const tabs={};
        for(const [tab,body] of Object.entries(layout.tabs||{})){
            tabs[tab]=(body.sections||[]).map(s=>s.label||s.name);
            for(const section of body.sections||[])walk(tab,section,[]);
        }
        // Numbered slots (#1..#100) collapse to their first instance: the stem is the setting.
        const settings={};
        for(const [key,input] of Object.entries(inputs)){
            if(input.type==='label')continue;
            const stem=key.replace(/([A-Za-z_])\d+$/,'$1#');
            if(stem!==key&&settings[stem])continue;
            const at=where[key]||{};
            settings[stem]={label:label(input.label||input.tooltip||input.name)||null,
                control:input.type||null,tab:at.tab||null,
                section:at.section?at.section.replace(/ #\d+/g,' #'):null,
                options:Array.isArray(input.options)?input.options.map(o=>clean(o.value)):null,
                min:input.min??null,max:input.max??null,
                bindings:(input.bindingMethods?.bindingKeys||[]).map(b=>`${b.type}:${b.key}`),
                shown:Boolean(where[key])};
        }
        return {tabs,settings:Object.fromEntries(Object.entries(settings).sort())};
    };
    const source=report.getDatasourceIds?.()[0]||'';
    const types={};
    for(const [id,type] of Object.entries(R.typeDictionary(registry))){
        const group=registry.getDisplayTypeGroup(id);
        for(const preset of type.getPresetConfigNames()){
            const key=`${id}/${preset}`;
            const wanted=args.full||(args.only||[]).includes(key)||(args.only||[]).includes(id);
            const entry={name:type.getDisplayName(),group,needs_data:type.getNeedData(),
                preset_name:registry.getDisplayTypeObj(id,preset)?.getPresetDisplayName?.()||null,
                dimensions:[type.getMinNumOfDimensions(),type.getMaxNumOfDimensions()],
                metrics:[type.getMinNumOfMetrics(),type.getMaxNumOfMetrics()]};
            if(!wanted){types[key]=entry;continue}
            if(group==='composite'){types[key]={...entry,panel:null,note:'composite'};continue}
            if(type.getNeedData()&&!source){
                types[key]={...entry,panel:null,note:'no datasource'};continue}
            try{
                const c=await creator.create({type:id,presetConfigName:preset,
                    creationMethod:'Clickdraw',
                    datasourceId:type.getNeedData()?source:'',parent:owner,
                    geometry:{top:0,left:0,width:400,height:300}});
                if(c.isReady$?.value!==true)await c.getReadyAsync();
                types[key]={...entry,panel:await describe(c)};
            }catch(error){
                types[key]={...entry,panel:null,note:String(error?.message||error).slice(0,200)}}
        }
    }
    const panels=args.full||args.panels;
    return {report:panels?await describe(report):null,page:panels?await describe(owner):null,types};
}"""


class Inventory:
    def __init__(self, studio: Studio):
        self.studio = studio

    async def native_catalog(
        self, types: list[str] | None = None, panels: bool = False, full: bool = False
    ) -> dict[str, Any]:
        """Summary of every type/preset; full panels for `types` (type or type/preset IDs),
        the report and page panels when `panels`, everything when `full`."""
        raw = await self.studio.report()
        pages = raw["reportConfig"]["page"]
        if not pages:
            raise StudioError("NO_PAGE", "The report needs a page to instantiate components on")
        frame = pages[0]
        instantiate = full or bool(types)
        try:
            result = await NativeOps(self.studio).evaluate(
                _CATALOG,
                {"pageId": frame["pageId"], "version": str(frame["version"]),
                 "only": types or [], "full": full, "panels": panels},
            )
        finally:
            if instantiate:
                # Components were created on the viewer model only; discard them.
                await self.studio.session.acquire(reload=True)
        return {
            "scope": "Editor-native catalog. Deterministic; diff a stored snapshot "
            "(docs/inventory/native-catalog.json) to see editor changes.",
            "type_count": len(result["types"]),
            **shared_options(result),
        }


def shared_options(catalog: dict[str, Any]) -> dict[str, Any]:
    """Move long option lists (fonts, sizes, ...) to one table, referenced by name."""
    lists: dict[str, list] = {}
    names: dict[str, str] = {}

    def visit(panel: dict[str, Any] | None) -> None:
        for setting in (panel or {}).get("settings", {}).values():
            options = setting.get("options")
            if isinstance(options, list) and len(options) > 12:
                key = json.dumps(options, sort_keys=True)
                if key not in names:
                    names[key] = f"list{len(names) + 1}"
                    lists[names[key]] = options
                setting["options"] = names[key]

    visit(catalog.get("report"))
    visit(catalog.get("page"))
    for entry in catalog["types"].values():
        visit(entry.get("panel"))
    return {**catalog, "option_lists": lists}
