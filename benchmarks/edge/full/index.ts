import { LIMITS, verifyZip } from './verify.ts';
// Replaced with synthetic fixture policy. Neither credentials nor fixture bytes are embedded.
const fullConfig = /* FULL_CONFIG */ { auth_sha256:'', source_prefix:'', github_url:'', trust:{} as any, cases:[] as any[] };
let fullActive=0;
Deno.serve(async request=>{
 const token=request.headers.get('x-sci-benchmark-token')||'';
 const tokenHash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token))),x=>x.toString(16).padStart(2,'0')).join('');
 if(token.length!==64 || tokenHash!==fullConfig.auth_sha256)return Response.json({error:'benchmark_auth'},{status:401});
 if(request.method!=='POST')return Response.json({error:'method'},{status:405});
 if(fullActive)return Response.json({error:'busy'},{status:429});
 fullActive++;
 const start=performance.now();let verifyStart:number|undefined;
 const memory:Record<string,number>={};
 const sample=()=>{for(const [k,v] of Object.entries(Deno.memoryUsage()))memory[k]=Math.max(memory[k]||0,v);};sample();
 const result:Record<string,unknown>={execution_id:Deno.env.get('SB_EXECUTION_ID'),region:Deno.env.get('SB_REGION'),runtime:Deno.version};
 let source:Response|undefined;
 try {
  const inputReader=request.body?.getReader();if(!inputReader)throw new Error('request');
  const input=new Uint8Array(4096);let inputSize=0;
  try{for(;;){const {value,done}=await inputReader.read();if(done)break;if(inputSize+value.length>input.length)throw new Error('request_limit');input.set(value,inputSize);inputSize+=value.length;}}
  finally{await inputReader.cancel();inputReader.releaseLock();}
  const body=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(input.subarray(0,inputSize)));
  if(Object.keys(body).sort().join(',')!=='id,url' || typeof body.url!=='string' || typeof body.id!=='string' || !/^sha256:[0-9a-f]{64}$/.test(body.id))throw new Error('request');
  const url=new URL(body.url);
  if(url.protocol!=='https:' || url.username || url.password || url.hash || url.port)throw new Error('source_policy');
  // Benchmark policy restricts retrieval to exact fixture paths on known HTTPS origins.
  // A public service must choose and implement its own destination/redirect policy.
  let fixture;
  if(url.origin+url.pathname===fullConfig.github_url && !url.search)fixture=fullConfig.cases.find(c=>c.name==='external-github');
  else if((url.origin+url.pathname).startsWith(fullConfig.source_prefix)) {
   const name=url.pathname.slice(new URL(fullConfig.source_prefix).pathname.length);
   fixture=fullConfig.cases.find(c=>`${c.name}.zip`===name);
  }
  if(!fixture)throw new Error('source_policy');
  source=await fetch(url,{headers:{'accept-encoding':'identity'},redirect:'manual',credentials:'omit',signal:AbortSignal.timeout(30000)});
  if(source.status!==200 || !source.body)throw new Error('source_status');
  if(source.headers.get('content-encoding') && source.headers.get('content-encoding')!=='identity')throw new Error('source_encoding');
  const lengthHeader=source.headers.get('content-length');
  if(lengthHeader!==null && !/^(0|[1-9][0-9]*)$/.test(lengthHeader))throw new Error('source_length');
  const declared=lengthHeader===null ? LIMITS.zip : Number(lengthHeader);
  if(!Number.isSafeInteger(declared) || declared>LIMITS.zip)throw new Error('download_limit');
  const bytes=new Uint8Array(declared);let size=0;const reader=source.body.getReader();
  try{for(;;){const {value,done}=await reader.read();if(done)break;if(size+value.length>bytes.length)throw new Error('download_limit');bytes.set(value,size);size+=value.length;sample();}}
  finally{await reader.cancel();reader.releaseLock();}
  if(lengthHeader!==null && size!==declared)throw new Error('source_length');
  result.download_ms=performance.now()-start;result.download_bytes=size;verifyStart=performance.now();
  result.verification=await verifyZip(bytes.subarray(0,size),body.id.slice(7),sample,fullConfig.trust[fixture.trust],fixture.file_limit);
  result.accepted=true;
 }catch(e){result.accepted=false;result.error=e instanceof Error ? e.message : 'error';}
 finally{if(source?.body && !source.body.locked)await source.body.cancel().catch(()=>{});fullActive--;sample();}
 result.verify_wall_ms=verifyStart===undefined ? null : performance.now()-verifyStart;
 result.total_wall_ms=performance.now()-start;result.memory_sampled_max=memory;
 // No input URLs or headers are logged: signed source URLs are temporary credentials.
 console.log(JSON.stringify({sci_benchmark:result}));return Response.json(result);
});
