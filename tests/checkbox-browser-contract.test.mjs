import test from 'node:test';
import assert from 'node:assert/strict';
import {compile,script} from './native.mjs';

const apply=compile(script('checkbox.py','_APPLY').replace('__INSPECT__','()=>globalThis.actual'));

function run(mode,value=false,reset=false){
  const snapshot={document_id:'doc',checkbox:{status:'available',value:null,
    enabled:true,is_parameter:false,is_analysis:false}};
  const old={window:globalThis.window,document:globalThis.document,
    location:globalThis.location,actual:globalThis.actual};
  const calls=[];
  const controller={componentId:'c',applyCheckboxValue:arg=>calls.push(['set',arg.value])};
  const services={reportContextService:{getReport:()=>({getPage:()=>({getComponent:()=>({id:'c'})})})},
    stateActionService:{ixa(c){/* syncBehaviorsState(!1,!0) */calls.push(['reset',c.id])}},$rootScope:{$evalAsync:()=>calls.push(['digest'])}};
  globalThis.window={angular:{element:()=>({injector:()=>({get:key=>services[key]}),controller:()=>controller})}};
  globalThis.document={querySelectorAll:()=>mode==='missing-controller'?[]:[{}]};
  globalThis.location={pathname:'/reporting/r/page/p'+(mode==='edit'?'/edit':'')};
  globalThis.actual=structuredClone(snapshot);
  if(mode==='changed')globalThis.actual.document_id='new-doc';
  if(mode==='parameter')globalThis.actual.checkbox.is_parameter=true;
  const expected=mode==='parameter'?structuredClone(globalThis.actual):snapshot;
  try{return {result:apply({reportId:'r',pageId:'p',componentId:'c',expected,value,reset}),calls};}
  finally{Object.assign(globalThis,old)}
}

test('dispatches true/false/clear through native handler and reset through native command',()=>{
  for(const value of [true,false,null]){
    const {result,calls}=run('normal',value);
    assert.equal(result.applied,true);
    assert.deepEqual(calls,[['set',value===null?undefined:value],['digest']]);
  }
  assert.deepEqual(run('normal',null,true).calls,[['reset','c'],['digest']]);
});

test('edit mode, replaced document, parameter and missing controller cause zero dispatch',()=>{
  for(const [mode,error] of [['edit','EDITOR_MODE_UNSAFE'],['changed','VERSION_CONFLICT'],
    ['parameter','CONTROL_UNSUPPORTED'],['missing-controller','NATIVE_CONTRACT_FAILED']]){
    const result=run(mode);
    assert.equal(result.result.error,error);
    assert.deepEqual(result.calls,[]);
  }
});
