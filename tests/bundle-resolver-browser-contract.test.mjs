import test from 'node:test';
import assert from 'node:assert/strict';
import {RESOLVER} from './native.mjs';

const R=Function(`${RESOLVER}\nreturn R`)();

// Two bundle builds expose the same behaviour under different minified names:
// the 2026-09 build the MCP originally pinned, and the 2026-10 build that broke it.
const BUILDS={
  '2026-09':{field:'pb',type:'Ke',reset:'qxa',apply:'Of',layers:'Usa',meta:'Oc',tree:'Gc',
    serializer:'xg',unions:'Qhc',args:['ib','YK'],context:'Ed'},
  '2026-10':{field:'qb',type:'Qe',reset:'ixa',apply:'mf',layers:'Msa',meta:'Pc',tree:'Fc',
    serializer:'zg',unions:'dic',args:['jb','ZK'],context:'Fd'},
};

function build(n,extra={}){
  const schema={[n.field]:Function('a',`/* this.qe.concepts[a]||this.ea[a] */return {name:a}`)};
  const field={isBoolean:Function(`return this.${n.type}()===3`),[n.type]:()=>6};
  const actions={
    [n.reset]:Function('a','/* a.syncBehaviorsState(!1,!0) */return ["reset",a]'),
    [n.apply]:Function('a,b,c,d','/* g=a.getBehavior(b); "lego-change-date-range" */return ["apply",a,b,c]'),
    canReset:()=>true,
  };
  const state={
    getValue:Function('a','a=lookup(this,a,current(this,a));return a'),
    [n.layers]:Function('a','return lookup(this,a,all(this,a))'),
  };
  const serialize=tree=>({blockDatasource:{tree}});void serialize;
  function Model(){this[n.tree]={queries:[{datasourceId:'ds'}],joins:[]}}
  Model.prototype.view=Function(`return serialize(this.${n.tree}).blockDatasource`);
  Model.prototype[n.serializer]=Function(`return serialize(this.${n.tree})`);
  Model.prototype.alias=Function(`return serialize(this.${n.tree})`);
  const Selector=Function('flags',`return function(a){this.JoinType={};this.${n.unions}=flags.unionsEnabled()}`)(
    {unionsEnabled:()=>true});
  const combiner=Function('a,b',`var l={};l.Bgb=[].concat(f((m=l.subConfig.${n.args[0]})!=null?m:[]),`
    +`f((n=l.subConfig.${n.args[1]})!=null?n:[]));var A=0,B=z.Bgb.map(function(D){});`
    +`z.subConfig.${n.context}&&B.unshift(A[0]);return z.subConfig.callback.apply(z.subConfig,B)`);
  return {schema,field,actions,state,model:new Model(),selector:new Selector(),
    service:{[n.meta]:{getMeta:id=>id}},exports:{combiner,Selector,...extra}};
}

for(const [name,names] of Object.entries(BUILDS)){
  test(`resolves every capability by behaviour in the ${name} build`,()=>{
    const live=build(names);
    globalThis.window={default_datastudio:live.exports};
    try{
      assert.deepEqual(R.schemaField(live.schema,'revenue'),{name:'revenue'});
      assert.equal(R.fieldType(live.field),6);
      assert.equal(R.member('stateAction.reset',live.actions),names.reset);
      assert.equal(R.member('stateAction.apply',live.actions),names.apply);
      assert.equal(R.layersMember(live.state),names.layers);
      assert.equal(R.metadataKey(live.service),names.meta);
      assert.deepEqual(R.blendTree(live.model).queries,[{datasourceId:'ds'}]);
      // Aliases with identical bodies are one candidate; either name is correct.
      assert.ok([names.serializer,'alias'].includes(R.serializerMember(live.model)));
      assert.equal(R.unionsKey(live.selector),names.unions);
      assert.deepEqual(R.ruleShape(),{arguments:names.args,context:names.context});
    }finally{delete globalThis.window}
  });
}

test('ambiguity and absence fail naming the contract instead of guessing',()=>{
  const names=BUILDS['2026-10'];
  const live=build(names);
  live.actions.other=Function('a','/* a.syncBehaviorsState(!1,!0); differs */return a');
  globalThis.window={default_datastudio:{}};
  try{
    assert.throws(()=>R.member('stateAction.reset',live.actions),
      /NATIVE_CONTRACT_CHANGED\[stateAction\.reset\]: 2 candidates/);
    assert.throws(()=>R.ruleShape(),/NATIVE_CONTRACT_CHANGED\[propertyRuleCombiner\]: 0 candidates/);
    assert.throws(()=>R.schemaField({},'x'),/NATIVE_CONTRACT_CHANGED\[schema\.field\]: 0 candidates/);
  }finally{delete globalThis.window}
});

test('one function exported twice is one candidate; equal text under two objects is not',()=>{
  const live=build(BUILDS['2026-10']);
  globalThis.window={default_datastudio:{...live.exports,again:live.exports.combiner}};
  try{assert.equal(R.candidates('propertyRuleCombiner').length,1)}finally{delete globalThis.window}
  globalThis.window={default_datastudio:{...live.exports,
    copy:Function(`return (${live.exports.combiner})`)()}};
  try{assert.throws(()=>R.ruleShape(),/2 candidates/)}finally{delete globalThis.window}
});
