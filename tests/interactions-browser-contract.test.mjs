import test from 'node:test';
import assert from 'node:assert/strict';
import {load} from './native.mjs';

const inspect=load('interactions.py','_INSPECT');

function run(mode='normal',variant='filter'){
  const values={onSelect:variant==='parameter'?{connectorConfigParameter:{parameterOverrides:[
      {key:'country_param',value:{parameterValues:[{stringValue:'UA'}]}}]}}:
    variant==='date'?{dateRange:{startAnchor:'20260101',endAnchor:'20260131'}}:
      {filterDefinition:{filterExpression:{filterConditionType:'F'}}},
    crossFilters:[{other:{version:3}}],controlMode:3};
  const behavior=[{action:variant==='date'?'lego-change-date-range':'lego-dimension-filter',
    isControl:true}];
  const parent={isChanged:mode==='dirty-parent'?{config:true}:{},getParent:()=>owner};
  const component={isReady$:{value:mode!=='not-ready'},getParent:()=>parent,
    getState:()=>({getValue:key=>values[key]}),
    getDisplayType:()=>variant==='date'?'simple-daterangepicker':'dimension-filter',
    getPresetConfigName:()=> 'control-checkbox',getBehavior:()=>behavior,
    getDisplayTypeObj:()=>({getNeedData:()=>mode!=='no-data'}),getSchema:()=>({})};
  const owner={getVersion:()=>mode==='stale'?'2':'1',getComponent:()=>mode==='missing'?null:component};
  const report={getId:()=>mode==='wrong-report'?'other':'r',getPage:()=>owner};
  const services={reportContextService:{getReport:()=>report},
    stateActionService:{canReset:()=>true}};
  const controller={componentId:'c',checkboxSettings:{value:mode==='mixed'?undefined:false},
    isEnabled:true,isAnalysis:false};
  const i={has:key=>key in services,get:key=>services[key]};
  const oldWindow=globalThis.window,oldDocument=globalThis.document;
  globalThis.window={angular:{element:()=>({injector:()=>i,controller:()=>controller})}};
  globalThis.document={body:{},querySelectorAll:()=>mode==='no-controller'?[]:[{}]};
  const before=JSON.stringify({values,behavior});
  try{
    const result=inspect({reportId:'r',pageId:'p',componentId:'c',version:'1'});
    assert.equal(JSON.stringify({values,behavior}),before);
    return result;
  }finally{globalThis.window=oldWindow;globalThis.document=oldDocument}
}

test('reads current selection separately from behavior defaults without mutation',()=>{
  const result=run();
  assert.equal(result.can_reset,true);
  assert.equal(result.control_mode,3);
  assert.equal(result.on_select_value.filterDefinition.filterExpression.filterConditionType,'F');
  assert.equal(result.on_select_behavior[0].init,undefined);
  assert.deepEqual(result.inherited_cross_filters,[{other:{version:3}}]);
  assert.equal(result.slider_filters,null);
  assert.equal(result.document_id,String(performance.timeOrigin));
  assert.deepEqual(result.checkbox,{status:'available',value:false,
    is_parameter:false,is_analysis:false,enabled:true});
  assert.deepEqual(result.effective_filters,{status:'unavailable',filters:null,
    reason:'not_exposed_by_bundle'});
});

test('checkbox mixed and missing controller are distinct states',()=>{
  assert.equal(run('mixed').checkbox.value,null);
  assert.deepEqual(run('no-controller').checkbox,{status:'unavailable'});
});

test('resolves parameter key and date range without DOM mutation',()=>{
  assert.deepEqual(run('normal','parameter').parameter,{status:'available',
    key:'country_param',enabled:true,values:[{stringValue:'UA'}]});
  assert.deepEqual(run('normal','date').date_range,{status:'available',
    value:{startAnchor:'20260101',endAnchor:'20260131'}});
});

test('unexposed resolution and data-less objects are not empty-filter success',()=>{
  assert.deepEqual(run('no-data').effective_filters,{status:'not_applicable',filters:null});
});

test('rejects out-of-scope, stale, dirty ancestor, missing and unready models',()=>{
  for(const [mode,error]of [['wrong-report','OUT_OF_SCOPE'],['stale','EDITOR_STALE'],
    ['dirty-parent','EDITOR_DIRTY'],['missing','NOT_FOUND'],['not-ready','EDITOR_NOT_READY']]){
    assert.equal(run(mode).error,error);
  }
});
