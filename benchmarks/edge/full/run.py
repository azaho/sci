"""Run hash + URL submissions. Inherits only credential handling, telemetry, cleanup."""
import importlib.util
from pathlib import Path
import argparse,hashlib,json,os,re,time
import requests
spec=importlib.util.spec_from_file_location('old_bench',Path(__file__).parent.parent/'run.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class FullBench(module.Bench):
 def prepare(self,fixtures):
  keys=self.call('GET',self.management+'/api-keys?reveal=true',management=True,key_lookup=True).json()
  self.key=next(k['api_key'] for k in keys if k.get('name')=='service_role')
  if os.environ.get('GITHUB_ACTIONS'):print(f'::add-mask::{self.key}',flush=True)
  cases=json.loads((fixtures/'cases.json').read_text());folder=Path(__file__).parent
  public=json.loads((folder/'public/case.json').read_text());cases.append(public)
  self.github_url=f'https://raw.githubusercontent.com/azaho/sci/{os.environ["GITHUB_SHA"]}/benchmarks/edge/full/public/baseline.zip'
  config={'auth_sha256':hashlib.sha256(self.invocation_token.encode()).hexdigest(),'source_prefix':self.base+f'/storage/v1/object/sign/{self.name}/','github_url':self.github_url,'trust':json.loads((fixtures/'trust.json').read_text()),'cases':cases}
  sources=[]
  for name in ['unicode18.ts','metadata.ts','timestamp.ts','verify.ts','index.ts']:
   s=(folder/name).read_text();s=re.sub(r'^import .*? from [\'\"]\./.*?;\n','',s,flags=re.M)
   if name=='index.ts':s=s.replace("/* FULL_CONFIG */ { auth_sha256:'', source_prefix:'', github_url:'', trust:{} as any, cases:[] as any[] }",json.dumps(config))
   sources.append(s)
  self.function_created=True
  try:
   deploy=self.call('POST',self.management+f'/functions/deploy?slug={self.name}',management=True,files={'metadata':(None,json.dumps({'name':self.name,'entrypoint_path':'index.ts','verify_jwt':True}),'application/json'),'file':('index.ts','\n'.join(sources),'application/typescript')}).json()
  except module.ApiError as e:
   if e.status in (401,403):self.function_created=False
   raise
  self.function_id=deploy['id']
  self.output.joinpath('deployment.json').write_text(json.dumps({'name':self.name,'id':self.function_id,'started':self.started.isoformat(),'git_sha':os.environ['GITHUB_SHA']},indent=2)+'\n')
  self.call('POST',self.base+'/storage/v1/bucket',json={'id':self.name,'name':self.name,'public':False,'file_size_limit':52_428_800})
  self.bucket_created=True;self.urls={}
  for case in cases:
   if case['name']=='external-github':self.urls[case['name']]=self.github_url;continue
   name=case['name']+'.zip';self.objects.append(name)
   with (fixtures/name).open('rb') as f:self.call('POST',self.base+f'/storage/v1/object/{self.name}/{name}',headers={'Content-Type':'application/zip'},data=f)
   signed=self.call('POST',self.base+f'/storage/v1/object/sign/{self.name}/{name}',json={'expiresIn':1800}).json()['signedURL']
   self.urls[case['name']]=self.base+'/storage/v1'+signed
  self.output.joinpath('cases.json').write_text(json.dumps(cases,indent=2)+'\n')
  return cases
 def run(self,cases,repeats):
  tests=[]
  for case in cases:
   for rep in range(repeats if case['expected_error'] is None else 1):tests.append((case,rep,'sha256:'+case['id'],self.urls[case['name']]))
  baseline=cases[0]
  for name,url,identifier,error in [('wrong-id',self.urls[baseline['name']],'sha256:'+'0'*64,'manifest_hash'),('private-url','https://127.0.0.1/','sha256:'+baseline['id'],'source_policy')]:
   c=dict(baseline,name=name,expected_error=error);tests.append((c,0,identifier,url))
  for case,rep,identifier,url in tests:
   started=time.monotonic()
   try:
    r=requests.post(self.base+f'/functions/v1/{self.name}',headers={'Authorization':f'Bearer {self.key}','apikey':self.key,'x-sci-benchmark-token':self.invocation_token},json={'id':identifier,'url':url},timeout=145)
    try:result=r.json()
    except ValueError:result={'error':'non_json_response'}
    if not r.ok:result={'error':result.get('code',result.get('error','platform_error'))}
    result['http_status']=r.status_code
   except requests.RequestException as e:result={'http_status':None,'error':type(e).__name__}
   result.update(case=case['name'],repeat=rep,expected_error=case['expected_error'],client_wall_ms=(time.monotonic()-started)*1000,file_count=case['files'],file_limit=case['file_limit'],payload_bytes=case['payload_bytes'])
   result['passed']=result.get('http_status')==200 and ((case['expected_error'] is None and result.get('accepted') is True) or (case['expected_error'] is not None and result.get('accepted') is False and result.get('error')==case['expected_error']))
   self.results.append(result);self.output.joinpath('results.json').write_text(json.dumps(self.results,indent=2)+'\n');print(json.dumps(result),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('fixtures',type=Path);p.add_argument('output',type=Path);p.add_argument('--repeats',type=int,default=5);a=p.parse_args();b=FullBench(a.output)
 try:
  c=b.prepare(a.fixtures);b.run(c,a.repeats);time.sleep(15);b.logs()
 finally:b.cleanup()
 if any(not r['passed'] for r in b.results):raise SystemExit('Unexpected result: inspect results.json')
