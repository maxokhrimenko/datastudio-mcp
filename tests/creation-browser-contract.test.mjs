import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {RESOLVER,fieldOf,schemaOf,script} from './native.mjs';

// _PREPARE runs under the OPS prelude of nativeops.py, exactly as ops_script wires it.
const prelude=readFileSync(new URL('../src/datastudio_mcp/nativeops.py',import.meta.url),'utf8')
  .match(/OPS_PRELUDE = r"""([\s\S]*?)"""/)[1];
const prepare=Function(`${RESOLVER}\n${prelude}\nreturn (${script('creation.py','_PREPARE')})`)();

const SPEC={type:'dimension-filter',preset:'default',datasource_id:'source',
  dimensions:[{name:'month'}],metrics:[],
  field_definitions:{dimensions:[{name:'qt_month',namespace:'t0'}],metrics:[]},
  left:10,top:20,width:100,height:50,rotation:0};

async function run({spec=SPEC,responsive=false,services=1,version='1',valid=true,errors={},
  group='control',needData=true,presets=['default'],dirty=false,breakdown=false,
  noSort=false,slot=false}={}){
  const created=[],calls=[],applied={};
  const type={getPresetConfigNames:()=>presets,getNeedData:()=>needData};
  const registry={getDisplayTypeObj:()=>type,getDisplayTypeGroup:()=>group};
  const owner={isReady$:{value:true},getVersion:()=>version,isResponsiveLayout:()=>responsive,
    isChanged:dirty?{x:true}:{},getConfig:()=>({component:created.map(c=>c.cfg)})};
  const report={getId:()=> 'report',getPage:()=>owner,isReady$:{value:true},
    getVersion:()=> '1',getConfig:()=>({})};
  // The creation service is found by what its create() does, not by its minified name.
  class Creator{create(o){void o.creationMethod;void o.isResponsiveLayout;return this.make(o)}
    make(o){
      calls.push(o);
      const cfg={componentId:`c${created.length}`,conceptDefs:{},keys:{}};
      const component={cfg,isReady$:{value:true},getId:()=>cfg.componentId,
        getConfig:()=>cfg,setAttributeValues:v=>{applied.attributes=v},
        // Slots the type has read {} until set; a bullet (noSort) has no dimensions slot.
        getDataSourceKeyValue:k=>k==='dimensions'?(breakdown?{breakdown:['qt_location']}:
          slot?{breakdown:['qt_preset_user'],...applied.keys?.dimensions}:noSort?applied.keys?.dimensions:
          applied.keys?.dimensions??{}):k==='sortData'?(noSort?[]:applied.keys?.sortData??[{sortColumn:'qt_preset',sortDir:1}]):
          k==='metrics'?applied.keys?.metrics??{primary:['qt_preset']}:applied.keys?.[k],
        setDataSourceKeyValue:(k,v)=>{(applied.keys??={})[k]=v},syncState:()=>{},
        getSchema:()=>({getConcepts:()=>[],...schemaOf(()=>fieldOf(valid))}),
        validate:()=>{},getConfigErrors:()=>errors};
      created.push(component);return component;
    }}
  const root={get:cls=>{assert.equal(cls,Creator);return new Creator()}};
  const injector={has:()=>true,get:name=>({reportContextService:{getReport:()=>report},
    legoMainRegistry:registry,$$angularLazyModuleRef:{injector:root}})[name]};
  // Structural markers intentionally match the production serializer discovery.
  function serialize(cfg){
    const marker='componentConfig:[],groupConfig:[],conceptDefs:[] hiddenFromViewer pageId:';
    void marker;
    return {page:{componentConfig:cfg.component}};
  }
  const synced=[];
  function sortBehaviorSync(c){/* setBehavior("onSort" "onPreSort" */synced.push(c.getId())}
  const ds={serialize,sortBehaviorSync};
  if(services>=1)ds.Creator=Creator;
  if(services>=2)ds.Other=class{create(o){void o.creationMethod;void o.isResponsiveLayout}};
  if(services===0)delete ds.Creator;
  const previousWindow=globalThis.window,previousDocument=globalThis.document;
  globalThis.window={angular:{element:()=>({injector:()=>injector})},default_datastudio:ds};
  globalThis.document={body:{}};
  const previousLocation=globalThis.location;
  globalThis.location={pathname:'/u/0/reporting/report/page/page'};
  try{
    const result=await prepare({reportId:'report',pageId:'page',version:'1',specs:[spec]})
      .then(value=>({value}),error=>({error:error.code,details:error.details}));
    return {...result,calls,applied,created,synced};
  }finally{globalThis.window=previousWindow;globalThis.document=previousDocument;
    globalThis.location=previousLocation}
}

test('creates through the editor creation service with drag-draw defaults and requested geometry',async()=>{
  const {value,calls,applied}=await run();
  assert.deepEqual(value.created_component_ids,['c0']);
  assert.deepEqual(value.before.componentConfig,[]);
  assert.equal(value.after.componentConfig.length,1);
  assert.deepEqual(calls[0],{type:'dimension-filter',presetConfigName:'default',
    creationMethod:'Clickdraw',datasourceId:'source',parent:calls[0].parent,
    geometry:{top:20,left:10,width:100,height:50}});
  assert.deepEqual(applied.attributes,{top:20,left:10,width:100,height:50,rotation:0});
});

test('explicit fields install definitions, roles and default sort; empty role stays explicit',async()=>{
  const {value,applied,created,synced}=await run();
  assert.deepEqual(created[0].cfg.conceptDefs.qt_month,{name:'qt_month',namespace:'t0'});
  assert.deepEqual(applied.keys.dimensions,{primary:['qt_month']});
  assert.deepEqual(applied.keys.metrics,{primary:[]});
  assert.deepEqual(applied.keys.sortData,[{sortColumn:'qt_month',sortDir:1}]);
  assert.deepEqual(synced,['c0']);
  assert.equal(value.error,undefined);
});

test('a date axis sorts by the date, oldest first; otherwise by the metric, largest first',async()=>{
  const fields={dimensions:[{name:'qt_month',namespace:'t0'}],metrics:[{name:'qt_gross',namespace:'t0'}]};
  const time=await run({spec:{...SPEC,time_axis:true,field_definitions:fields}});
  assert.deepEqual(time.applied.keys.sortData,[{sortColumn:'qt_month',sortDir:0}]);
  const size=await run({spec:{...SPEC,time_axis:false,field_definitions:fields}});
  assert.deepEqual(size.applied.keys.sortData,[{sortColumn:'qt_gross',sortDir:1}]);
});

test('guards stop before or after creation with a stable code',async()=>{
  const cases=[
    [{responsive:true},'RESPONSIVE_LAYOUT_REQUIRES_SEPARATE_OPERATION'],
    [{presets:['other']},'UNKNOWN_TYPE_OR_PRESET'],
    [{group:'composite'},'COMPOSITE_CREATION_REQUIRES_CHILDREN'],
    [{spec:{...SPEC,datasource_id:null,dimensions:[],metrics:[]}},'MISSING_DATASOURCE'],
    [{valid:false},'UNKNOWN_FIELD'],
    [{errors:{display:{x:'bad'}}},'INVALID_COMPONENT_CONFIGURATION'],
    [{version:'NaN'},'EDITOR_STALE'],
    [{dirty:true},'EDITOR_DIRTY'],
    [{breakdown:true,spec:{...SPEC,metrics:[{name:'a',aggregation:7},{name:'b',aggregation:7}],
      field_definitions:{dimensions:[{name:'qt_month',namespace:'t0'}],
        metrics:[{name:'qt_a',namespace:'t0'},{name:'qt_b',namespace:'t0'}]}}},'INVALID_BINDINGS'],
  ];
  for(const [options,code] of cases){
    const {error}=await run(options);
    assert.equal(error,code,JSON.stringify(options));
  }
});

test('creation service must resolve to exactly one editor class',async()=>{
  for(const services of [0,2]){
    const {error,calls}=await run({services});
    assert.equal(error,'NATIVE_CONTRACT_CHANGED');
    assert.equal(calls.length,0);
  }
});

test('a second dimension goes to the breakdown slot; a type without sort gets none',async()=>{
  const two={...SPEC,dimensions:[{name:'month'},{name:'kind'}],metrics:[{name:'a',aggregation:7}],
    field_definitions:{dimensions:[{name:'qt_month',namespace:'t0'},{name:'qt_kind',namespace:'t0'}],
      metrics:[{name:'qt_a',namespace:'t0'}]}};
  const {applied}=await run({spec:two,breakdown:false});
  assert.deepEqual(applied.keys.dimensions,{primary:['qt_month','qt_kind']});
  const stacked=await run({spec:two,slot:true});
  assert.deepEqual(stacked.applied.keys.dimensions,{breakdown:['qt_kind'],primary:['qt_month']});
  const one=await run({slot:true});
  assert.deepEqual(one.applied.keys.dimensions,{breakdown:['qt_preset_user'],primary:['qt_month']});
  const bullet=await run({noSort:true,spec:{...SPEC,dimensions:[],metrics:[{name:'a',aggregation:7}],
    field_definitions:{dimensions:[],metrics:[{name:'qt_a',namespace:'t0'}]}}});
  assert.equal(bullet.applied.keys.sortData,undefined);
  assert.deepEqual(bullet.synced,[]);
  assert.equal('dimensions' in bullet.applied.keys,false);
  assert.deepEqual(bullet.synced,[]);
});
