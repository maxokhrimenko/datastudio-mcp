import test from 'node:test';
import assert from 'node:assert/strict';
import {load} from './native.mjs';
const validate=load('page_filters.py','_VALIDATE');

function run(mode='valid'){
  const definition={version:3,filterDefinition:{filterExpression:{}}};
  const schema={isDummy:()=>false,consumer:false};
  function validator(value,schema){
    const marker='.filterDefinition .isDummy .version||3';void marker;
    assert.deepEqual(value,definition);value.changed=true;
    return schema.consumer?false:mode!=='invalid';
  }
  const c={getId:()=> 'c',getDisplayType:()=> 'chart',isReady$:{value:mode!=='unready'},
    getDisplayTypeObj:()=>({getNeedData:()=>true}),getSchema:()=>({...schema,consumer:true})};
  const p={isReady$:{value:true},getVersion:()=> '1',getComponents:()=>[c],
    getAllDataSourceKeys:()=>({filters:{stateKey:'filters',dominant:3}}),
    getDataSourceKeyValue:()=>mode==='stale'?['other']:[]};
  const r={getId:()=> 'r',getPage:()=>p,getResourceById:()=>definition,
    isChanged:mode==='dirty'?{data:true}:{}};
  if(mode==='report')Object.assign(r,p,{
    getAllDataSourceKeys:()=>({filters:{stateKey:'filters',dominant:4}})});
  const group={...p,getParent:()=>p};
  delete group.getVersion;
  p.getGroup=id=>id==='g'?group:null;
  const i={has:()=>true,get:name=>name==='reportContextService'?{getReport:()=>r}:{getSchema:()=>schema}};
  const oldWindow=globalThis.window,oldDocument=globalThis.document;
  globalThis.window={angular:{element:()=>({injector:()=>i})},default_datastudio:{validator}};
  globalThis.document={body:{}};
  try{return validate({reportId:'r',pageId:mode==='report'?null:'p',groupId:mode==='group'?'g':null,version:'1',beforeIds:[],beforeInherit:mode!=='report',
    filters:[{id:'f',sourceId:'ds',definition}]})}
  finally{globalThis.window=oldWindow;globalThis.document=oldDocument}
}
test('invalid consumer compatibility is advisory, invalid filter source is terminal',()=>{
  assert.deepEqual(run().consumers,[{component_id:'c',type:'chart',status:'available',
    compatibility:[{filter_id:'f',valid:false}]}]);
  assert.equal(run('invalid').error,'INVALID_FILTER');
  assert.equal(run('unready').consumers[0].status,'unavailable');
});
test('dirty or stale page cannot be used to prepare a binding change',()=>{
  assert.equal(run('dirty').error,'EDITOR_DIRTY');
  assert.equal(run('stale').error,'EDITOR_STALE');
});
test('report root uses non-inheriting scope and reports its diagnostic coverage',()=>{
  assert.equal(run('report').owner_scope,'report');
  assert.equal(run('report').consumers.length,1);
});
test('group uses its own filter settings but the enclosing page version',()=>{
  assert.equal(run('group').owner_scope,'group');
  assert.equal(run('group').consumers.length,1);
});
