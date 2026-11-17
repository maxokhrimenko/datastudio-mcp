import test from 'node:test';
import assert from 'node:assert/strict';
import {load,schemaOf} from './native.mjs';
const validate=load('blends.py','_VALIDATE_BLEND');

function run(mode='valid'){
  const definition={blockDatasource:{blocks:[{treeQueryBlockConfig:{query:{datasourceId:'ds'}}}]},opaque:7};
  if(mode==='right-nested')definition.blockDatasource.blocks[0].treeQueryBlockConfig={join:{
    left:{query:{}},right:{join:{left:{query:{}},right:{query:{}}}}}};
  const original=structuredClone(definition);
  function Model(raw){
    const marker='Unknown TreeQueryBlock type, cannot create RuntimeBlend columnPruningEnabled';void marker;
    this.Fc={queries:[{datasourceId:'ds',concepts:[{queryTimeTransformation:{dataTransformation:{sourceFieldName:'field'}}}]}],joins:[],raw};
    raw.opaque=8;
  }
  // Native shape: a view method reveals the serializer, an alias method returns it.
  const serialize=tree=>tree.raw;void serialize;
  Model.prototype.zfa=function(){return serialize(this.Fc).blockDatasource};
  Model.prototype.zg=function(){return serialize(this.Fc)};
  Model.prototype.oe=function(){return serialize(this.Fc)};
  function validator(){
    const marker='reason:134 reason:152 .conceptPairs';void marker;
    return mode==='invalid'?[{reason:73,index:0,part:0}]:[];
  }
  const report={getId:()=>mode==='wrong-report'?'other':'r',getVersion:()=>mode==='stale-version'?'2':'1',
    isChanged:mode==='dirty'?{data:true}:{},getResourceById:()=>mode==='stale-resource'?{}:definition};
  function builder(service,model){
    const marker='.getDatasourceIds() .getMeta( "_dv" sourceFieldName';void marker;
    model.zg().blockDatasource.datasourceBlock={fields:[{field:{name:'new_dv0'}}]};
  }
  const schema={isDummy:()=>mode==='dummy-schema',...schemaOf(()=>mode==='missing-field'?null:{})};
  const metadata={Pc:{getMeta:()=>({getSchema:()=>schema})}};
  const i={has:()=>true,get:name=>name==='blendSchemaService'?metadata:{getReport:()=>report}};
  const oldWindow=globalThis.window,oldDocument=globalThis.document;
  globalThis.window={angular:{element:()=>({injector:()=>i})},default_datastudio:mode==='missing-native'?{}:{Model,validator,builder}};
  globalThis.document={body:{}};
  try{const result=validate({reportId:'r',version:'1',blendId:'b',definition,
    rebuildOutputSchema:['rebuild','dummy-schema','missing-field'].includes(mode)});
    assert.deepEqual(definition,original);return result;}
  finally{globalThis.window=oldWindow;globalThis.document=oldDocument;}
}
test('detached validation returns issues and normalization without changing saved data',()=>{
  assert.equal(run().structurally_valid,true);
  assert.deepEqual(run().normalization_paths,['/opaque']);
  assert.equal(run('invalid').structurally_valid,false);
  assert.deepEqual(run('invalid').issues,[{reason:73,index:0,part:0}]);
});
test('native identity, freshness, dirtiness, contracts and shape fail closed',()=>{
  for(const [mode,error] of [['wrong-report','OUT_OF_SCOPE'],['stale-version','EDITOR_STALE'],
    ['stale-resource','EDITOR_STALE'],['dirty','EDITOR_DIRTY'],['missing-native','BLEND_CONTRACT_CHANGED'],
    ['right-nested','UNSUPPORTED_BLEND_SHAPE']])assert.equal(run(mode).error,error);
});
test('output schema rebuild uses the detached model and rejects unavailable source fields',()=>{
  const result=run('rebuild');
  assert.equal(result.output_schema_rebuilt,true);
  assert.deepEqual(result.rebuilt_output_fields,[{field:{name:'new_dv0'}}]);
  assert.equal(run('dummy-schema').error,'SCHEMA_NOT_READY');
  assert.equal(run('missing-field').error,'FIELD_NOT_FOUND');
});
