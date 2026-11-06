import { parseJson, verifyZip } from './verify.ts';
import { doiKey } from './metadata.ts';
const root=Deno.args[0];
const fixtures=JSON.parse(await Deno.readTextFile(`${root}/cases.json`));
const trust=JSON.parse(await Deno.readTextFile(`${root}/trust.json`));
let failed=0;
for(const c of fixtures) {
  let error:string|null=null; const started=performance.now();
  try {await verifyZip(await Deno.readFile(`${root}/${c.name}.zip`),c.id,()=>{},trust[c.trust],c.file_limit);}
  catch(e){error=e instanceof Error ? e.message : String(e);}
  const passed=error===c.expected_error;if(!passed)failed++;
  console.log(JSON.stringify({case:c.name,passed,error,expected:c.expected_error,local_ms:performance.now()-started}));
}
for(const s of ['{"a":1,"\\u0061":2}','{"x":"\\ud800"}','{"a":1} trailing']) {try{parseJson(new TextEncoder().encode(s));failed++;}catch{}}
if(doiKey('doi:10.123/a%2Fb')!==doiKey('doi:10.123/A%2fB'))failed++;
if(failed)throw new Error(`${failed} checks failed`);
