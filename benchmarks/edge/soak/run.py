"""Controlled, no-retry soak. Four independent deployments expose fresh/reused workers."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path
import random
import time
import requests

spec = importlib.util.spec_from_file_location('full_bench', Path(__file__).parent.parent/'full/run.py')
full = importlib.util.module_from_spec(spec)
spec.loader.exec_module(full)

def utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')

class Soak(full.FullBench):
    def transform_source(self, name, source):
        if name != 'index.ts':
            return source
        substitutions = {
            'let fullActive=0;': 'let fullActive=0; let fullCalls=0;',
            "if(fullActive)return Response.json({error:'busy'},{status:429});":
                "if(fullActive)return Response.json({error:'busy',execution_id:Deno.env.get('SB_EXECUTION_ID'),region:Deno.env.get('SB_REGION')},{status:429});",
            'const result:Record<string,unknown>={execution_id:':
                "const result:Record<string,unknown>={request_id:request.headers.get('x-sci-request-id'),request_index:++fullCalls,execution_id:",
            'console.log(JSON.stringify({sci_benchmark:result}));return Response.json(result);':
                'if(!result.accepted)console.log(JSON.stringify({sci_benchmark:result}));return Response.json(result);',
        }
        for old, new in substitutions.items():
            assert source.count(old) == 1, f'Instrumentation target changed: {old}'
            source = source.replace(old,new)
        return source

    def invoke(self, case, phase, concurrency, ordinal, wave):
        start=time.monotonic()
        started=utc()
        request_id=f'w{wave}-c{concurrency}-{ordinal:04d}'
        try:
            response=requests.post(self.base+f'/functions/v1/{self.name}',
                headers={'Authorization':f'Bearer {self.key}','apikey':self.key,
                         'x-sci-benchmark-token':self.invocation_token,
                         'x-sci-request-id':request_id,'x-region':'us-east-1'},
                json={'id':'sha256:'+case['id'],'url':self.urls[case['name']]},timeout=145)
            try: result=response.json()
            except ValueError: result={'error':'non_json_response'}
            if not response.ok:
                result={k:result[k] for k in ['error','execution_id','region'] if k in result} | {
                    'error':result.get('code',result.get('error','platform_error'))}
                # Above allowlist omits any arbitrary platform response body.
                try: result['error']=response.json().get('code',result['error'])
                except ValueError: pass
            result['http_status']=response.status_code
            result['response_region']=response.headers.get('x-sb-edge-region')
            result['gateway_request_id']=response.headers.get('sb-request-id')
        except requests.RequestException as error:
            result={'http_status':None,'error':type(error).__name__}
        result.update(request_id=request_id,case=case['name'],profile=case['profile'],wave=wave,
                      phase=phase,concurrency=concurrency,ordinal=ordinal,
                      started_at=started,completed_at=utc(),client_wall_ms=(time.monotonic()-start)*1000,
                      file_count=case['files'],payload_bytes=case['payload_bytes'],
                      expected_zip_bytes=case['zip_bytes'],timestamped=case['timestamped'])
        result['passed']=result.get('http_status')==200 and result.get('accepted') is True
        if result['passed']: result['outcome']='accepted'
        elif result.get('http_status')==429: result['outcome']='busy'
        elif result.get('http_status')==546: result['outcome']='resource_limit'
        elif result.get('http_status') is None: result['outcome']='transport_failure'
        elif result.get('error','').startswith('source_') or result.get('error') in ['TimeoutError','AbortError']:
            result['outcome']='source_failure'
        elif result.get('http_status')!=200: result['outcome']='platform_failure'
        else: result['outcome']='verification_failure'
        return result

    def run_wave(self, cases, wave):
        cases=[c for c in cases if 'profile' in c]
        assert len(cases)==20 and all(c['files']==1000 and c['payload_bytes']==50_000_000 for c in cases)
        self.output.joinpath('soak-cases.json').write_text(json.dumps(cases,indent=2)+'\n')
        plans=[]
        for concurrency, total, offset in [(1,500,0),(2,250,0),(4,250,10)]:
            sequence=[cases[(i+offset)%len(cases)] for i in range(total)]
            random.Random(20261106+concurrency).shuffle(sequence)
            sizes=([125]*4 if concurrency==1 else ([62,63,62,63] if concurrency==2 else [63,62,63,62]))
            lo=sum(sizes[:wave]); hi=lo+sizes[wave]
            plans.append((concurrency,sequence[lo:hi],lo))
        self.output.joinpath('plan.json').write_text(json.dumps({
            'wave':wave,'region':'us-east-1','planned':250,
            'phases':[{'concurrency':c,'count':len(items)} for c,items,_ in plans],
            'retries':0,'first_call_definition':'request_index == 1; not a guarantee of infrastructure cold start'},indent=2)+'\n')
        log=self.output/'results.jsonl'
        for concurrency, sequence, start_index in plans:
            phase=f'concurrency-{concurrency}'
            print(f'wave={wave} phase={phase} start requests={len(sequence)}',flush=True)
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                pending=[pool.submit(self.invoke,c,phase,concurrency,start_index+i,wave) for i,c in enumerate(sequence)]
                for future in as_completed(pending):
                    result=future.result();self.results.append(result)
                    with log.open('a') as f:f.write(json.dumps(result)+'\n')
                    if len(self.results)%25==0 or not result['passed']:
                        failures=sum(not r['passed'] for r in self.results)
                        print(json.dumps({'wave':wave,'completed':len(self.results),'failures':failures,
                                          'last_case':result['case'],'last_outcome':result['outcome']}),flush=True)
                    if len(self.results)%25==0:
                        self.output.joinpath('results.json').write_text(json.dumps(self.results,indent=2)+'\n')
            self.output.joinpath('results.json').write_text(json.dumps(self.results,indent=2)+'\n')
        assert len(self.results)==250

    def logs(self):
        if not getattr(self,'function_id',None):return
        # Minute windows avoid truncating the API's per-query row limit.
        end=datetime.now(timezone.utc)
        start=self.started-timedelta(minutes=1)
        events={}; errors=[]
        query=("select timestamp, id, source, event_message, log_attributes from logs "
               "where source = 'function_logs' "
               f"and position(toString(log_attributes), '{self.function_id}') > 0 "
               "order by timestamp asc limit 1000")
        while start<end:
            stop=min(start+timedelta(seconds=60),end)
            try:
                response=self.call('GET',self.management+'/analytics/endpoints/logs',management=True,
                    params={'sql':query,'iso_timestamp_start':start.isoformat(timespec='milliseconds').replace('+00:00','Z'),
                            'iso_timestamp_end':stop.isoformat(timespec='milliseconds').replace('+00:00','Z')}).json()
                rows=response.get('result')
                if not isinstance(rows,list):raise RuntimeError('Log query returned no result array')
                if len(rows)>=1000:errors.append(f'Possibly truncated window: {start.isoformat()}')
                for row in rows:events[row.get('id') or json.dumps(row,sort_keys=True)]=row
            except Exception as error:errors.append(str(error))
            start=stop
        self.output.joinpath('runtime-logs.json').write_text(json.dumps({'result':list(events.values()),'collection_errors':errors},indent=2)+'\n')
        print(f'Collected {len(events)} log events, {len(errors)} collection errors.',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('fixtures',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--wave',type=int,required=True,choices=range(4));a=p.parse_args()
    bench=Soak(a.output)
    try:
        cases=bench.prepare(a.fixtures)
        bench.run_wave(cases,a.wave)
        time.sleep(15)
        bench.logs()
    finally:bench.cleanup()
    # Request failures are measurements. A final workflow check evaluates the aggregate.
