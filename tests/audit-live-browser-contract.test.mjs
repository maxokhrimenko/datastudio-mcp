import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source=readFileSync(new URL('../src/datastudio_mcp/audit.py',import.meta.url),'utf8');
const live=Function('return ('+source.match(/_LIVE = """([\s\S]*?)"""/)[1]+')')();

function component(id,{needData=true,typeNeedsData=true,error=null,table=true,children=[]}={}){
  return {getId:()=>id,getDisplayType:()=>'table',getDatasourceOrViewId:()=>'ds',
    needData:()=>needData,getDisplayTypeObj:()=>({getNeedData:()=>typeNeedsData}),
    getComponents:()=>children,hasError:()=>!!error,getError:()=>error,
    hasDataTableModel:()=>table,getErrorCategory:()=>error?.errorCategory??0,
    hasDatasourceError:()=>false,hasAccessDeniedError:()=>false,
    hasConfigurationError:()=>error?.errorCategory===5,hasSystemError:()=>false,
    hasViewersCredsDatasource:()=>false};
}

async function run(pageComponents,reportComponents=[],includeReportLevel=true){
  const report={getId:()=>'r',getPage:()=>({getComponents:()=>pageComponents}),
    getComponents:()=>reportComponents};
  const i={has:()=>true,get:()=>({getReport:()=>report})};
  globalThis.window={angular:{element:()=>({injector:()=>i})}};
  globalThis.document={body:{}};
  try{return await live({reportId:'r',pageId:'p',includeReportLevel,timeout:0})}
  finally{delete globalThis.window;delete globalThis.document}
}

test('reads viewer errors without stacks and skips components that never load data',async()=>{
  const error={errorCategory:5,reason:269,title:'Invalid filter',description:'bad',
    originalStack:'Error: secret stack',learnMoreLink:'https://x'};
  const button=component('button',{needData:false,table:false});
  const nested=component('nested');
  const result=await run([component('bad',{error,table:false}),button,
    component('group',{children:[nested]})],[component('header')]);
  assert.deepEqual(result.components.map(c=>[c.component_id,c.scope]),
    [['header','report'],['bad','page'],['group','page'],['nested','page']]);
  const bad=result.components.find(c=>c.component_id==='bad');
  assert.equal(bad.has_error,true);
  assert.equal(bad.flags.configuration,true);
  assert.deepEqual(bad.error,{reason:269,title:'Invalid filter',description:'bad'});
});

test('unsettled components are reported as such and report scope is read once',async()=>{
  const result=await run([component('slow',{table:false})],[component('header')],false);
  assert.deepEqual(result.components.map(c=>[c.component_id,c.settled]),[['slow',false]]);
});
