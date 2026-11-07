"""Matched bounded workloads, changing only the per-worker rejection guard."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import json
from pathlib import Path
import random

spec = importlib.util.spec_from_file_location('soak_bench', Path(__file__).parent.parent/'soak/run.py')
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

class Comparison(soak.Soak):
    def __init__(self, output, mode):
        super().__init__(output)
        self.mode = mode

    def transform_source(self, name, source):
        source = super().transform_source(name, source)
        if name != 'index.ts':
            return source
        substitutions = {
            'let fullActive=0; let fullCalls=0;':
                'let fullActive=0; let fullCalls=0; const activeStates=new Set<{peak:number}>();',
            'let source:Response|undefined;':
                "const activeState={peak:fullActive}; activeStates.add(activeState); for(const s of activeStates)s.peak=Math.max(s.peak,fullActive); result.active_at_start=fullActive; console.log(JSON.stringify({sci_request_start:{request_id:result.request_id,execution_id:result.execution_id,active:fullActive}})); let source:Response|undefined;",
            'fullActive--;sample();':
                'result.peak_active_for_request=activeState.peak; activeStates.delete(activeState); fullActive--;sample();',
        }
        if self.mode == 'unguarded':
            substitutions["if(fullActive)return Response.json({error:'busy',execution_id:Deno.env.get('SB_EXECUTION_ID'),region:Deno.env.get('SB_REGION')},{status:429});"] = '// Unguarded comparison: accept overlapping requests in this worker.'
        for old, new in substitutions.items():
            assert source.count(old) == 1, f'Instrumentation target changed: {old}'
            source = source.replace(old, new)
        return source

    def run_comparison(self, cases):
        cases = [c for c in cases if 'profile' in c]
        assert len(cases) == 20
        wave = 0 if self.mode == 'guarded' else 1
        self.output.joinpath('plan.json').write_text(json.dumps({
            'mode': self.mode, 'wave': wave, 'region': 'us-east-1', 'planned': 200,
            'phases': [{'concurrency': c, 'count': n} for c,n in [(1,20),(2,60),(4,60),(8,60)]],
            'retries': 0, 'schedule_seed': 20261107,
        }, indent=2)+'\n')
        for concurrency, count in [(1,20),(2,60),(4,60),(8,60)]:
            sequence = cases * (count // len(cases))
            random.Random(20261107+concurrency).shuffle(sequence)
            print(f'{self.mode}: concurrency={concurrency}, attempts={count}', flush=True)
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                pending = [pool.submit(self.invoke,c,f'concurrency-{concurrency}',concurrency,i,wave)
                           for i,c in enumerate(sequence)]
                for future in as_completed(pending):
                    result = future.result()
                    result['mode'] = self.mode
                    self.results.append(result)
                    with (self.output/'results.jsonl').open('a') as f:
                        f.write(json.dumps(result)+'\n')
                    if len(self.results)%10 == 0 or not result['passed']:
                        self.output.joinpath('results.json').write_text(json.dumps(self.results,indent=2)+'\n')
                        print(json.dumps({'mode':self.mode,'completed':len(self.results),
                            'last_outcome':result['outcome'],'failures':sum(not r['passed'] for r in self.results)}),flush=True)
        assert len(self.results) == 200
        self.output.joinpath('results.json').write_text(json.dumps(self.results,indent=2)+'\n')

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('fixtures',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--mode',required=True,choices=['guarded','unguarded']);a=p.parse_args()
    bench=Comparison(a.output,a.mode)
    try:
        cases=bench.prepare(a.fixtures)
        bench.run_comparison(cases)
    finally:
        bench.cleanup()
