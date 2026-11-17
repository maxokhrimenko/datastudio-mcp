import test from 'node:test';
import assert from 'node:assert/strict';
import {load} from './native.mjs';
const panel=load('panels.py','_PANEL');

// A chart whose reference line #1 exists only after "Add reference line": the button sets
// the slot's panel value, the panel writes it, and then the slot's inputs are enabled.
function chart(){
  const store={};
  const model={getId:()=> 'c',isReady$:{value:true},isChanged:{},store,
    getDisplayKeyValue:k=>k==='referenceLineType'?[store.referenceLineType1]:undefined};
  const input=(key,extra)=>({key,...extra,bindingMethods:{bindingKeys:[{type:'display',key}],
    readFromConfig:m=>m.store[key]??null,writeToConfig:(m,e)=>{m.store[key]=e.value}}});
  class PanelService{
    getPropertyLayoutConfig(){return {tabs:{}}}
    needData(){}
    async getPropertyInputConfig(m){
      const c={
        referenceLineType1:input('referenceLineType1',{type:'dropdown',
          options:[{name:'Constant value',value:'constant'}],disabled:!m.store.referenceLineType1}),
        referenceLineLabel1:input('referenceLineLabel1',{type:'text',disabled:!m.store.referenceLineType1}),
        referenceLineLabel2:input('referenceLineLabel2',{type:'text',disabled:true}),
        referenceLineAdd:{key:'referenceLineAdd',type:'actionButton',disabled:false,
          action(){void m.getDisplayKeyValue("referenceLineType");c.referenceLineType1.value='constant'}},
      };
      return c;
    }
  }
  const page={isReady$:{value:true},getVersion:()=> '1',getComponentsRecursive:()=>[model],getConfig:()=>({})};
  const report={getId:()=> 'r',isReady$:{value:true},getPage:()=>page,getVersion:()=> '1',isChanged:{}};
  function pageConfigBuilder(){/* componentConfig:[],groupConfig:[],conceptDefs:[] hiddenFromViewer pageId: */
    return {page:{store:{...store}}}}
  const root={get:()=>new PanelService()};
  const i={has:()=>true,get:name=>name==='reportContextService'?{getReport:()=>report}:
    name==='$$angularLazyModuleRef'?{injector:root}:{}};
  return {model,i,dd:{PanelService,pageConfigBuilder}};
}
async function run(c,args){
  const oldWindow=globalThis.window,oldDocument=globalThis.document;
  globalThis.window={angular:{element:()=>({injector:()=>c.i})},default_datastudio:c.dd};
  globalThis.document={body:{}};
  try{return await panel({reportId:'r',kind:'component',pageId:'p',componentId:'c',version:'1',
    continuing:false,search:'',includeHidden:true,...args})}
  finally{globalThis.window=oldWindow;globalThis.document=oldDocument}
}

test('Add button is a writable click that opens its slot; the request is order-free',async()=>{
  const c=chart();
  const described=await run(c,{mode:'describe'});
  const item=key=>described.items.find(x=>x.key===key);
  assert.equal(item('referenceLineAdd').writable,true);
  assert.equal(item('referenceLineLabel1').writable,true);
  assert.equal(item('referenceLineLabel2').writable,false);  // only the next free slot
  assert.equal(item('referenceLineAdd').enabled,true);
  assert.equal(item('referenceLineLabel1').enabled,false);  // opens with Add in the same request
  const out=await run(c,{mode:'set',changes:[{key:'referenceLineLabel1',value:'Threshold'},
    {key:'referenceLineAdd',value:true}]});
  assert.equal(out.error,undefined);
  assert.deepEqual(c.model.store,{referenceLineType1:'constant',referenceLineLabel1:'Threshold'});
  assert.deepEqual(out.properties[0],{key:'referenceLineAdd',action:true});
});

test('a button takes only true',async()=>{
  const out=await run(chart(),{mode:'set',changes:[{key:'referenceLineAdd',value:'yes'}]});
  assert.equal(out.error,'INVALID_PROPERTY_VALUE');
});

test('a key the panel does not have is named unknown, with the keys that look like it',async()=>{
  const out=await run(chart(),{mode:'set',changes:[{key:'referenceLineLabel',value:'x'}]});
  assert.equal(out.error,'UNKNOWN_PROPERTY');
  assert.deepEqual(out.details,{key:'referenceLineLabel',
    similar:['referenceLineLabel1','referenceLineLabel2']});
});

// A palette: a custom color replaces the slot's theme reference; the other slots stay.
function palette(){
  const store={multiColor:[{color:'#111111',themeRef:{index:4}},{color:'#222222',themeRef:{index:5}}]};
  const model={getId:()=> 'c',isReady$:{value:true},isChanged:{},store,getDisplayKeyValue:()=>undefined};
  class PanelService{
    getPropertyLayoutConfig(){return {tabs:{}}}
    needData(){}
    async getPropertyInputConfig(){
      return {multiColor:{key:'multiColor',type:'multicolorpicker',disabled:false,
        bindingMethods:{bindingKeys:[{type:'display',key:'multiColor'}],
          readFromConfig:m=>m.store.multiColor,writeToConfig:(m,e)=>{m.store.multiColor=e.value}}}};
    }
  }
  const page={isReady$:{value:true},getVersion:()=> '1',getComponentsRecursive:()=>[model],getConfig:()=>({})};
  const report={getId:()=> 'r',isReady$:{value:true},getPage:()=>page,getVersion:()=> '1',isChanged:{}};
  function pageConfigBuilder(){/* componentConfig:[],groupConfig:[],conceptDefs:[] hiddenFromViewer pageId: */
    return {page:{store:structuredClone(store)}}}
  const root={get:()=>new PanelService()};
  const i={has:()=>true,get:name=>name==='reportContextService'?{getReport:()=>report}:
    name==='$$angularLazyModuleRef'?{injector:root}:{}};
  return {model,i,dd:{PanelService,pageConfigBuilder}};
}

test('a custom palette color drops the theme reference of its slot only',async()=>{
  const c=palette();
  const out=await run(c,{mode:'set',changes:[{key:'multiColor',value:[{color:'#F59E52'}]}]});
  assert.equal(out.error,undefined);
  assert.deepEqual(c.model.store.multiColor,[{color:'#F59E52'},{color:'#222222',themeRef:{index:5}}]);
});

// A text box: its content is a setting (`text`), as typing on the canvas is.
function textbox(){
  const keys={text:'<span>old</span>'};
  const model={getId:()=> 'c',isReady$:{value:true},isChanged:{},store:keys,
    getDisplayType:()=> 'simple-description',getDisplayKeyValue:k=>keys[k],
    setDisplayKeyValue:(k,v)=>{keys[k]=v},getConfig:()=>({config:{display:{keys}}})};
  class PanelService{
    getPropertyLayoutConfig(){return {tabs:{}}}
    needData(){}
    async getPropertyInputConfig(){return {}}
  }
  const page={isReady$:{value:true},getVersion:()=> '1',getComponentsRecursive:()=>[model],getConfig:()=>({})};
  const report={getId:()=> 'r',isReady$:{value:true},getPage:()=>page,getVersion:()=> '1',isChanged:{}};
  function pageConfigBuilder(){/* componentConfig:[],groupConfig:[],conceptDefs:[] hiddenFromViewer pageId: */
    return {page:{store:{...keys}}}}
  const root={get:()=>new PanelService()};
  const i={has:()=>true,get:name=>name==='reportContextService'?{getReport:()=>report}:
    name==='$$angularLazyModuleRef'?{injector:root}:{}};
  return {model,i,dd:{PanelService,pageConfigBuilder}};
}

test('a text box lists and takes its content as `text`',async()=>{
  const c=textbox();
  const described=await run(c,{mode:'describe'});
  const item=described.items.find(x=>x.key==='text');
  assert.equal(item.value,'<span>old</span>');
  assert.equal(item.writable,true);
  const out=await run(c,{mode:'set',changes:[{key:'text',value:'<b>new</b>'}]});
  assert.equal(out.error,undefined);
  assert.equal(c.model.store.text,'<b>new</b>');
});

// A field picker takes a field name and binds the component's own concept for it.
function picker({emptyAsNull=false,stripDisplay=false}={}){
  const qt={dataTransformation:{sourceFieldName:'_month_start_'}};
  const store={sortConcept:[]};
  const model={getId:()=> 'c',isReady$:{value:true},isChanged:{},store,getDisplayKeyValue:()=>undefined,
    getConfig:()=>({conceptDefs:{qt_m:{name:'qt_m',namespace:'t0',queryTimeTransformation:qt}}})};
  class PanelService{
    getPropertyLayoutConfig(){return {tabs:{}}}
    needData(){}
    async getPropertyInputConfig(){
      return {sortConcept:{key:'sortConcept',type:'legochiplistsingleselect',disabled:false,
        bindingMethods:{bindingKeys:[{type:'datasource',key:'sortData'}],
          readFromConfig:m=>m.store.sortConcept,writeToConfig:(m,e)=>{
            m.store.sortConcept=emptyAsNull&&!e.value.length?null:stripDisplay?e.value.map(v=>({
              name:v.name,namespace:v.namespace,queryTimeTransformation:{dataTransformation:
              v.queryTimeTransformation.dataTransformation}})):e.value}}}};
    }
  }
  const page={isReady$:{value:true},getVersion:()=> '1',getComponentsRecursive:()=>[model],getConfig:()=>({})};
  const report={getId:()=> 'r',isReady$:{value:true},getPage:()=>page,getVersion:()=> '1',isChanged:{}};
  function pageConfigBuilder(){/* componentConfig:[],groupConfig:[],conceptDefs:[] hiddenFromViewer pageId: */
    return {page:{store:structuredClone(store)}}}
  const root={get:()=>new PanelService()};
  const i={has:()=>true,get:name=>name==='reportContextService'?{getReport:()=>report}:
    name==='$$angularLazyModuleRef'?{injector:root}:{}};
  return {model,i,dd:{PanelService,pageConfigBuilder}};
}

test('a field picker takes a field name; an unbound field is refused with a hint',async()=>{
  const c=picker();
  const out=await run(c,{mode:'set',changes:[{key:'sortConcept',value:['_month_start_']}]});
  assert.equal(out.error,undefined);
  assert.deepEqual(c.model.store.sortConcept,[{id:'t0.qt_m',name:'qt_m',namespace:'t0',
    queryTimeTransformation:{dataTransformation:{sourceFieldName:'_month_start_'}}}]);
  const bare=picker();
  assert.equal((await run(bare,{mode:'set',changes:[{key:'sortConcept',value:'_month_start_'}]})).error,
    undefined);
  assert.equal(bare.model.store.sortConcept[0].name,'qt_m');
  const missing=await run(picker(),{mode:'set',changes:[{key:'sortConcept',value:['_grade_']}]});
  assert.equal(missing.error,'FIELD_NOT_ON_COMPONENT');
});

test('a field picker takes the name the user sees and binds that field',async()=>{
  const c=picker();
  const month={isValid:()=>true,getName:()=> '_month_start_',getDisplayName:()=> 'Month start'};
  c.model.getSchema=()=>({qb(a){/* this.qe.concepts[a]||this.ea[a] */return {isValid:()=>false}},
    // The component's own concept is listed too, under the same name.
    getConcepts:()=>[month,{isValid:()=>true,getName:()=> 'qt_m',getDisplayName:()=> 'Month start'}]});
  const out=await run(c,{mode:'set',changes:[{key:'sortConcept',value:'month START'}]});
  assert.equal(out.error,undefined);
  assert.equal(c.model.store.sortConcept[0].name,'qt_m');
});

test('a field the component does not use yet is found by the name the user sees',async()=>{
  const c=picker();
  const month={isValid:()=>true,getName:()=> 'calc_month',getDisplayName:()=> 'Month'};
  c.model.getSchema=()=>({qb(a){/* this.qe.concepts[a]||this.ea[a] */
    return {isValid:()=>a==='calc_month'}},getConcepts:()=>[month]});
  const out=await run(c,{mode:'set',changes:[{key:'sortConcept',value:'Month'}]});
  assert.equal(out.error,undefined);
  assert.equal(c.model.store.sortConcept[0].queryTimeTransformation.dataTransformation.sourceFieldName,
    'calc_month');
});

test('emptying a field picker holds when it reads back as nothing selected',async()=>{
  const c=picker({emptyAsNull:true});
  const out=await run(c,{mode:'set',changes:[{key:'sortConcept',value:[]}]});
  assert.equal(out.error,undefined);
  assert.equal(c.model.store.sortConcept,null);
});

test('a picked field holds when its read-back omits the display format',async()=>{
  const c=picker({stripDisplay:true});
  const cfg=c.model.getConfig();
  cfg.conceptDefs.qt_m.queryTimeTransformation.displayTransformation={displayFormat:'MMM yy'};
  c.model.getConfig=()=>cfg;
  const out=await run(c,{mode:'set',changes:[{key:'sortConcept',value:'_month_start_'}]});
  assert.equal(out.error,undefined);
  assert.equal(c.model.store.sortConcept[0].queryTimeTransformation.displayTransformation,undefined);
});
