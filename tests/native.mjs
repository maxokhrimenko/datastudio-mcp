// Loads page scripts the way native_script() runs them: with the real resolver R in scope.
import {readFileSync} from 'node:fs';

const read=file=>readFileSync(new URL(`../src/datastudio_mcp/${file}`,import.meta.url),'utf8');
export const RESOLVER=read('bundle.py').match(/RESOLVER = r"""([\s\S]*?)"""/)[1];
export const compile=script=>Function(`${RESOLVER}\nreturn (${script})`)();
export const script=(file,name)=>read(file).match(new RegExp(`${name} = r?"""([\\s\\S]*?)"""`))[1];
export const load=(file,name)=>compile(script(file,name));

// Fakes carry the behaviour R resolves by, under arbitrary minified names.
export const schemaOf=field=>({qb(a){/* this.qe.concepts[a]||this.ea[a] */return field(a)}});
export const fieldOf=(valid,type=0)=>({isValid:()=>valid,isBoolean(){return this.Xq()===3},Xq:()=>type});
export function propertyRuleCombiner(a,b){
  // Mirrors the native combiner's text: arguments = jb ++ ZK, Fd prepends context.
  var l={};l.Bgb=[].concat(_b((m=l.subConfig.jb)!=null?m:[]),_b((n=l.subConfig.ZK)!=null?n:[]));
  var A=0,B=z.Bgb.map(function(D){});z.subConfig.Fd&&B.unshift(A[0]);
  return z.subConfig.callback.apply(z.subConfig,B);
}
