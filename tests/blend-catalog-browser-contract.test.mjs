import test from 'node:test';
import assert from 'node:assert/strict';
import {load} from './native.mjs';
const catalog=load('blends.py','_JOIN_TYPES');

function run(mode='valid'){
  const types={1:'INNER_JOIN',2:'FULL_OUTER_JOIN',3:'LEFT_OUTER_JOIN',4:'RIGHT_OUTER_JOIN',101:'CROSS_JOIN',5:'UNION'};
  for(const [code,name] of Object.entries(types))types[name]=Number(code);
  if(mode==='malformed')types.INNER_JOIN=undefined;
  const flags={unionsEnabled:()=>mode==='union'};
  function Selector(){this.JoinType=types;this.dic=flags.unionsEnabled();}
  const ns=mode==='absent'?{}:{Selector};
  // A second function with the same text is a real ambiguity; one function exported twice is not.
  if(mode==='ambiguous')ns.Duplicate=Function(`return (${Selector})`)();
  if(mode==='aliased')ns.Alias=Selector;
  const i={has:()=>true,get:()=>({getReport:()=>({getId:()=>mode==='wrong-report'?'other':'r'})})};
  const oldWindow=globalThis.window,oldDocument=globalThis.document;
  globalThis.window={angular:{element:()=>({injector:()=>i})},default_datastudio:ns};
  globalThis.document={body:{}};
  try{return catalog({reportId:'r'})}
  finally{globalThis.window=oldWindow;globalThis.document=oldDocument}
}
test('native type enum and Union feature flag are separate contracts',()=>{
  const result=run();
  assert.equal(result.types.length,6);
  assert.deepEqual(result.types.find(t=>t.name==='LEFT_OUTER_JOIN'),{
    name:'LEFT_OUTER_JOIN',code:3,available:true,node_kind:'join'});
  assert.deepEqual(result.types.find(t=>t.name==='UNION'),{
    name:'UNION',code:5,available:false,node_kind:'union'});
  assert.equal(run('union').types.find(t=>t.name==='UNION').available,true);
  assert.equal(run('aliased').types.length,6);
});
test('missing, ambiguous, malformed contracts and wrong report fail closed',()=>{
  for(const mode of ['absent','ambiguous','malformed'])assert.equal(run(mode).error,'BLEND_CONTRACT_CHANGED');
  assert.equal(run('wrong-report').error,'OUT_OF_SCOPE');
});
