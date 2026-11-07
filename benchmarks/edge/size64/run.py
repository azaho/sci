"""One measured verification per fresh deployment; no measured-request retries."""
import argparse,hashlib,importlib.util,json,os,time
from pathlib import Path
import requests
from adapter import source
spec=importlib.util.spec_from_file_location('bench',Path(__file__).parent.parent/'run.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Single(m.Bench):
 def prepare_one(self,fixtures,case):
  keys=self.call('GET',self.management+'/api-keys?reveal=true',management=True,key_lookup=True).json();self.key=next(k['api_key'] for k in keys if k.get('name')=='service_role')
  if os.environ.get('GITHUB_ACTIONS'):print(f'::add-mask::{self.key}',flush=True)
  parts=[f'{i}.bin' for i in range(8)]
  config={'auth_sha256':hashlib.sha256(self.invocation_token.encode()).hexdigest(),'source_prefix':self.base+f'/storage/v1/object/sign/{self.name}/','id':case['id'],'parts':parts,'zip_bytes':case['zip_bytes']}
  handler=Path(__file__).with_name('index.ts').read_text().replace("/* CONFIG */ {auth_sha256:'', source_prefix:'', id:'',parts:[] as string[], zip_bytes:67108864}",json.dumps(config))
  sources=[('file',('index.ts',handler,'application/typescript'))]
  for name in ['unicode18.ts','metadata.ts','timestamp.ts','verify.ts','vendor/pkijs.js']:sources.append(('file',(name,source(name),'application/typescript' if name.endswith('.ts') else 'application/javascript')))
  self.function_created=True
  try:deployment=self.call('POST',self.management+f'/functions/deploy?slug={self.name}',management=True,files=[('metadata',(None,json.dumps({'name':self.name,'entrypoint_path':'index.ts','verify_jwt':True}),'application/json')),*sources]).json()
  except m.ApiError as e:
   if e.status in (401,403):self.function_created=False
   raise
  self.function_id=deployment['id'];self.output.joinpath('deployment.json').write_text(json.dumps({'name':self.name,'id':self.function_id,'started':self.started.isoformat(),'git_sha':os.environ['GITHUB_SHA'],'case':case['name']},indent=2)+'\n')
  self.headers={'Authorization':f'Bearer {self.key}','apikey':self.key,'x-sci-benchmark-token':self.invocation_token,'x-region':'us-east-1'}
  for _ in range(5):
   r=requests.get(self.base+f'/functions/v1/{self.name}',headers=self.headers,timeout=30)
   if r.status_code==200 and r.json().get('ready'):break
   time.sleep(2)
  else:raise RuntimeError(f'Preflight failed: HTTP {r.status_code}')
  self.call('POST',self.base+'/storage/v1/bucket',json={'id':self.name,'name':self.name,'public':False,'file_size_limit':8_388_608});self.bucket_created=True
  self.urls=[]
  with (fixtures/f"{case['name']}.zip").open('rb') as f:
   for name in parts:
    self.objects.append(name);data=f.read(8_388_608);assert len(data)==8_388_608
    self.call('POST',self.base+f'/storage/v1/object/{self.name}/{name}',headers={'Content-Type':'application/octet-stream'},data=data)
    signed=self.call('POST',self.base+f'/storage/v1/object/sign/{self.name}/{name}',json={'expiresIn':1800}).json()['signedURL'];self.urls.append(self.base+'/storage/v1'+signed)
   assert not f.read(1)
 def measure(self,case,trial):
  request_id=f"{case['name']}-{trial}";start=time.monotonic()
  try:
   r=requests.post(self.base+f'/functions/v1/{self.name}',headers=self.headers|{'x-sci-request-id':request_id},json={'id':case['id'],'urls':self.urls},timeout=145)
   try:data=r.json()
   except ValueError:data={'error':'non_json_response'}
   if not r.ok:data={'error':data.get('code',data.get('error','platform_error'))}
   data['http_status']=r.status_code
  except requests.RequestException as e:data={'error':type(e).__name__,'http_status':None}
  data.update(case=case['name'],trial=trial,request_id=request_id,client_wall_ms=(time.monotonic()-start)*1000,fixture=case)
  data['passed']=data.get('http_status')==200 and data.get('accepted') is True
  self.output.joinpath('results.json').write_text(json.dumps([data],indent=2)+'\n')
  print(json.dumps({k:data.get(k) for k in ['case','trial','passed','http_status','error','verify_wall_ms']}),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('fixtures',type=Path);p.add_argument('output',type=Path);p.add_argument('--trial',type=int,choices=[0,1]);a=p.parse_args()
 cases=json.loads((a.fixtures/'cases.json').read_text())
 for trial in ([a.trial] if a.trial is not None else range(2)):
  for i,case in enumerate(cases):
   bench=Single(a.output/f'wave-{trial*len(cases)+i}')
   try:bench.prepare_one(a.fixtures,case);bench.measure(case,trial)
   finally:bench.cleanup()
