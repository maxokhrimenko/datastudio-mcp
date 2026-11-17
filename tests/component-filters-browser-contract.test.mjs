import test from 'node:test';
import assert from 'node:assert/strict';
import {load} from './native.mjs';
const validate=load('component_filters.py','_VALIDATE');

function run(mode='valid'){
  let calls=0;
  const filter={version:3,filterDefinition:{filterExpression:{}}};
  function validator(value,schema){
    const marker='.filterDefinition .isDummy .version||3';void marker;
    calls++;assert.deepEqual(value,filter);assert.ok(schema);
    value.modified=true; // Validator receives a detached copy.
    return mode!=='invalid';
  }
  const schema={isDummy:()=>mode==='dummy'};
  const c={isReady$:{value:true},isChanged:mode==='dirty'?{data:true}:{},
    getParent:()=>p,getDisplayTypeObj:()=>({getNeedData:()=>mode!=='decoration'}),
    getAllDataSourceKeys:()=>({filters:{stateKey:'filters',dominant:3}}),
    getDataSourceKeyValue:()=>mode==='local-stale'?['other']:[],
    getDatasourceOrViewId:()=>mode==='source-stale'?'other':'ds',getSchema:()=>schema};
  const p={isReady$:{value:true},getVersion:()=>mode==='stale'?'2':'1',getComponent:()=>c};
  const report={getId:()=>mode==='wrong-report'?'other':'r',getPage:()=>p,
    getResourceById:()=>mode==='resource-stale'?{}:filter};
  const i={has:()=>true,get:()=>({getReport:()=>report})};
  const previousWindow=globalThis.window,previousDocument=globalThis.document;
  globalThis.window={angular:{element:()=>({injector:()=>i})},default_datastudio:{validator}};
  globalThis.document={body:{}};
  try{return {result:validate({reportId:'r',pageId:'p',version:'1',changes:[{
    componentId:'c',datasourceId:'ds',beforeIds:[],beforeInherit:true,
    filters:[{id:'f',definition:filter}]}]}),calls,filter}}
  finally{globalThis.window=previousWindow;globalThis.document=previousDocument}
}

test('native schema validation uses copies and returns exact validated identities',()=>{
  const actual=run();
  assert.deepEqual(actual.result.components,[{component_id:'c',filter_ids:['f'],datasource_id:'ds'}]);
  assert.equal(actual.calls,1);assert.equal(actual.filter.modified,undefined);
  assert.equal(run('invalid').result.error,'FILTER_INCOMPATIBLE');
});
test('scope, dirty ancestors, local/resource drift and unsupported consumers fail closed',()=>{
  for(const [mode,error] of [['wrong-report','OUT_OF_SCOPE'],['stale','EDITOR_STALE'],
    ['dirty','EDITOR_DIRTY'],['local-stale','EDITOR_STALE'],['source-stale','EDITOR_STALE'],
    ['resource-stale','EDITOR_STALE'],['dummy','SCHEMA_NOT_READY'],['decoration','FILTER_NOT_APPLICABLE']]){
    const actual=run(mode);assert.equal(actual.result.error,error,mode);assert.equal(actual.calls,0,mode);
  }
});
