import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// The page script exactly as Python sends it: REQUEST_SCRIPT + the _CREATE body, with
// Python's escapes in those non-raw strings undone.
const read=file=>readFileSync(new URL(`../src/datastudio_mcp/${file}`,import.meta.url),'utf8');
const unescape=text=>text.replace(/\\\\/g,'\\');
const prelude=read('browser.py').match(/REQUEST_SCRIPT = """([\s\S]*?)"""/)[1];
const body=read('bigquery.py').match(/_CREATE = REQUEST_SCRIPT \+ """([\s\S]*?)"""/)[1];
const create=Function(`return (${unescape(prelude+body)})`)();

const COLUMNS=[{field:{name:'biz'},mutableVersion:{name:'_biz_'}},
  {field:{name:'_biz_'},outputName:'biz',immutableVersion:{name:'biz'}}];

async function run(args,preload){
  const sent=[];
  const old={document:globalThis.document,location:globalThis.location,
    performance:globalThis.performance,fetch:globalThis.fetch,window:globalThis.window};
  globalThis.document={cookie:'RAP_XSRF_TOKEN=x'};
  globalThis.location={pathname:'/reporting/r',href:'https://datastudio.google.com/reporting/r'};
  globalThis.performance={getEntriesByType:()=>[{name:'https://datastudio.google.com/x?appVersion=1'}]};
  globalThis.window={preload};
  globalThis.fetch=async(url,init)=>{
    const endpoint=url.split('?')[0].split('/').pop(), payload=JSON.parse(init.body);
    sent.push([endpoint,payload]);
    const json=endpoint==='getFirstPartyAccessToken'?{accessToken:'t'}
      :endpoint==='getColumns'?{columns:COLUMNS}:{shareable:{id:'new-id',name:'n'}};
    return {status:200,text:async()=>JSON.stringify(json)};
  };
  try{return {result:await create({connector:{alias:'t0'},name:'n',timeout:1000,...args}),sent};}
  finally{Object.assign(globalThis,old)}
}

const created=sent=>sent.find(([endpoint])=>endpoint==='createBlockDatasource')?.[1];

test('a reusable source is created in the account Data Studio project',async()=>{
  const {result,sent}=await run({dryRun:false},
    JSON.stringify({myDataStudioConfig:{projectNumber:'111'}}));
  assert.equal(result.id,'new-id');
  assert.deepEqual(created(sent).uiCreateOptions,{projectNumber:'111',region:'global'});
  assert.equal(created(sent).reportId,undefined);
});

test('a report source keeps the report project',async()=>{
  const {sent}=await run({dryRun:false,reportId:'r',projectNumber:'222'},
    {myDataStudioConfig:{projectNumber:'111'}});
  assert.equal(created(sent).uiCreateOptions.projectNumber,'222');
});

test('without any project nothing is sent',async()=>{
  const {result,sent}=await run({dryRun:false},undefined);
  assert.equal(result.error,'PROJECT_NUMBER_MISSING');
  assert.deepEqual(sent,[]);
});
