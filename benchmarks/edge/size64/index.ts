import { verifyZip } from './verify.ts';
const config=/* CONFIG */ {auth_sha256:'', source_prefix:'', id:'',parts:[] as string[], zip_bytes:67108864};
let calls=0;
Deno.serve(async request=>{
 const token=request.headers.get('x-sci-benchmark-token')||'';
 const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token))),n=>n.toString(16).padStart(2,'0')).join('');
 if(token.length!==64 || digest!==config.auth_sha256)return Response.json({error:'auth'},{status:401});
 if(request.method==='GET')return Response.json({ready:true});
 if(request.method!=='POST')return Response.json({error:'method'},{status:405});
 const start=performance.now();
 const result:any={request_id:request.headers.get('x-sci-request-id'),execution_id:Deno.env.get('SB_EXECUTION_ID'),region:Deno.env.get('SB_REGION'),runtime:Deno.version,verification_call:++calls};
 console.log(JSON.stringify({sci_request_start:result}));
 const memory:Record<string,number>={};
 const sample=()=>{for(const [k,v] of Object.entries(Deno.memoryUsage()))memory[k]=Math.max(memory[k]||0,v);};
 try{
  const reader=request.body?.getReader();if(!reader)throw new Error('request');
  const bodyBytes=new Uint8Array(16384);let bodySize=0;
  try{for(;;){const {done,value}=await reader.read();if(done)break;if(bodySize+value.length>bodyBytes.length)throw new Error('request_limit');bodyBytes.set(value,bodySize);bodySize+=value.length;}}
  finally{await reader.cancel();reader.releaseLock();}
  const body=JSON.parse(new TextDecoder().decode(bodyBytes.subarray(0,bodySize)));
  if(body.id!==config.id || !Array.isArray(body.urls) || body.urls.length!==config.parts.length)throw new Error('request');
  const zip=new Uint8Array(config.zip_bytes);let offset=0;sample();
  for(let i=0;i<body.urls.length;i++){
   const url=new URL(body.urls[i]);
   if(url.protocol!=='https:' || url.username || url.password || url.hash || url.port || url.origin+url.pathname!==config.source_prefix+config.parts[i])throw new Error('source_policy');
   const response=await fetch(url,{redirect:'manual',credentials:'omit',headers:{'accept-encoding':'identity'},signal:AbortSignal.timeout(30000)});
   if(response.status!==200 || !response.body)throw new Error('source_status');
   const part=response.body.getReader();
   try{for(;;){const {done,value}=await part.read();if(done)break;if(offset+value.length>zip.length)throw new Error('download_limit');zip.set(value,offset);offset+=value.length;sample();}}
   finally{await part.cancel();part.releaseLock();}
  }
  if(offset!==zip.length)throw new Error('download_size');
  result.download_bytes=offset;result.download_ms=performance.now()-start;
  const verifyStart=performance.now();result.verification=await verifyZip(zip,config.id,sample);
  result.verify_wall_ms=performance.now()-verifyStart;result.accepted=true;
 }catch(e){result.accepted=false;result.error=e instanceof Error?e.message:'error';}
 result.total_wall_ms=performance.now()-start;result.memory_sampled_max=memory;
 console.log(JSON.stringify({sci_benchmark:result}));return Response.json(result);
});
