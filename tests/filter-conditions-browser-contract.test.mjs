import test from 'node:test';
import assert from 'node:assert/strict';
import {fieldOf,load,schemaOf} from './native.mjs';
const validate=load('filter_conditions.py','_VALIDATE');

function run(mode='valid',catalogOnly=false){
  let calls=0;
  function validator(value,schema){
    const marker='.filterDefinition .isDummy .version||3';
    void marker;
    calls++;
    assert.equal(value.version,3);
    return mode!=='invalid';
  }
  const catalog=Object.fromEntries(['T','F','IN','BT','ANY','RM','IBL'].map(code=>[code,{label:code,dataTypes:[3]}]));
  const exports={catalog,validator};
  if(mode==='ambiguous')exports.other=Function(`return (${validator})`)();
  if(mode==='missing-catalog')delete exports.catalog;
  const report={getId:()=>mode==='wrong-report'?'other':'r'};
  const schema={isDummy:()=>mode==='dummy',...schemaOf(()=>fieldOf(true,mode==='stale'?3:2))};
  const i={has:()=>true,get:name=>name==='reportContextService'?{getReport:()=>report}:{getSchema:()=>schema}};
  const previousWindow=globalThis.window,previousDocument=globalThis.document;
  globalThis.window={angular:{element:()=>({injector:()=>i})},default_datastudio:exports};
  globalThis.document={body:{}};
  try{return {result:validate({reportId:'r',datasourceId:'ds',physicalTypes:{revenue:2},
    definition:{filterExpression:{}},catalogOnly}),calls}}
  finally{globalThis.window=previousWindow;globalThis.document=previousDocument}
}

test('catalog and validation are distinct and native false stays false',()=>{
  assert.equal(run('valid',true).result.conditions.length,7);
  assert.equal(run('valid',true).calls,0);
  assert.deepEqual(run().result,{valid:true,status:'valid'});
  assert.deepEqual(run('invalid').result,{valid:false,status:'invalid'});
});
test('wrong scope, catalog drift, ambiguous validator and stale schema fail before validation',()=>{
  for(const [mode,error] of [['wrong-report','OUT_OF_SCOPE'],['missing-catalog','FILTER_CONTRACT_CHANGED'],
    ['ambiguous','FILTER_CONTRACT_CHANGED'],['dummy','SCHEMA_NOT_READY'],['stale','SCHEMA_STALE']]){
    const actual=run(mode);
    assert.equal(actual.result.error,error);
    assert.equal(actual.calls,0);
  }
});
