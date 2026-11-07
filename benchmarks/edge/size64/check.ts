import {verifyZip} from './verify.ts';
const dir=Deno.args[0];
const cases=JSON.parse(await Deno.readTextFile(dir+'/cases.json'));
for(const c of cases){
 const bytes=await Deno.readFile(dir+'/'+c.name+'.zip');
 if(bytes.length!==67108864)throw new Error('fixture size');
 const r=await verifyZip(bytes,c.id);if(r.files!==1000)throw new Error('fixture count');
 console.log(c.name,JSON.stringify(r.times));
 const bad=bytes.slice();bad[12]=0x22;
 try{await verifyZip(bad,c.id);throw new Error('incorrect ZIP date accepted');}
 catch(e){if(!(e instanceof Error) || e.message!=='zip_local')throw e;}
}
