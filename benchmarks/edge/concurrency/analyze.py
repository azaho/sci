"""Summarize every attempt; correlate resource failures with worker-start logs."""
import argparse
import collections
import csv
import json
import math
from pathlib import Path
import statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args();root=a.root
records=[];events=[];errors={}
for folder in sorted(root.glob('wave-*')):
    if (folder/'results.json').exists():records.extend(json.loads((folder/'results.json').read_text()))
    log=folder/'runtime-logs-final.json'
    if log.exists():
        data=json.loads(log.read_text());events.extend(data['result']);errors[folder.name]=data['collection_errors']
assert len({r['request_id'] for r in records})==len(records)
starts={};shutdowns={};boots=set()
for event in events:
    attr=event['log_attributes'];worker=attr.get('execution_id')
    if attr.get('event_type')=='Boot':boots.add(worker)
    if attr.get('event_type')=='Shutdown':shutdowns[worker]=attr
    try: payload=json.loads(event['event_message'])
    except (ValueError,TypeError):continue
    start=payload.get('sci_request_start') if isinstance(payload,dict) else None
    if start and start.get('request_id'):starts[start['request_id']]=start
for r in records:
    start=starts.get(r['request_id'],{})
    if r.get('execution_id') and start:assert r['execution_id']==start['execution_id']
    r['resolved_worker']=r.get('execution_id') or start.get('execution_id')
    r['logged_active_at_start']=start.get('active')
    r['worker_shutdown_reason']=shutdowns.get(r['resolved_worker'],{}).get('reason')

def distribution(values):
    xs=sorted(values)
    if not xs:return None
    def q(p):
        i=(len(xs)-1)*p;lo=math.floor(i);hi=math.ceil(i)
        return xs[lo]+(xs[hi]-xs[lo])*(i-lo)
    return {'n':len(xs),'min':min(xs),'p50':q(.5),'p95':q(.95),'p99':q(.99),'max':max(xs),'mean':statistics.mean(xs)}

def summarize(rs):
    accepted=[r for r in rs if r['passed']]
    workers={r['resolved_worker'] for r in rs if r['resolved_worker']}
    result={'attempts':len(rs),'accepted':len(accepted),
        'outcomes':dict(collections.Counter(r['outcome'] for r in rs)),
        'workers':len(workers),'workers_with_shutdown':len(workers & shutdowns.keys()),
        'worker_shutdown_reasons':dict(collections.Counter(shutdowns[w]['reason'] for w in workers if w in shutdowns)),
        'failed_request_shutdown_reasons':dict(collections.Counter(r['worker_shutdown_reason'] for r in rs if not r['passed'])),
        'logged_max_active':max((r.get('logged_active_at_start') or 0 for r in rs),default=0),
        'successful_peak_active':max((r.get('peak_active_for_request') or 0 for r in accepted),default=0),
        'sampled_memory_max_bytes':{k:max((r.get('memory_sampled_max',{}).get(k,0) for r in rs),default=0) for k in ['heapUsed','external']}}
    for field in ['verify_wall_ms','download_ms','total_wall_ms','client_wall_ms']:
        result[field]=distribution([r[field] for r in accepted if isinstance(r.get(field),(int,float))])
    return result

summary={'by_mode':{},'by_mode_concurrency':{},'by_mode_profile':{},'boot_workers':len(boots),
    'shutdown_workers':len(shutdowns),'shutdown_reasons':dict(collections.Counter(v['reason'] for v in shutdowns.values())),
    'collection_errors':errors,'attempts_with_start_event':len(starts),'failed_requests':[]}
for mode in ['guarded','unguarded']:
    rs=[r for r in records if r['mode']==mode]
    summary['by_mode'][mode]=summarize(rs)
    summary['by_mode_concurrency'][mode]={str(c):summarize([r for r in rs if r['concurrency']==c]) for c in [1,2,4,8]}
    summary['by_mode_profile'][mode]={p:summarize([r for r in rs if r['profile']==p]) for p in sorted({r['profile'] for r in rs})}
    summary['by_mode'][mode]['shutdown_cpu_ms']=distribution([float(shutdowns[w]['cpu_time_used']) for w in {r['resolved_worker'] for r in rs} if w in shutdowns])
    summary['by_mode'][mode]['shutdown_memory_bytes']=distribution([float(shutdowns[w].get('memory_used.total',0)) for w in {r['resolved_worker'] for r in rs} if w in shutdowns])
summary['failed_requests']=[{k:r.get(k) for k in ['request_id','mode','concurrency','case','outcome','http_status','error','resolved_worker','logged_active_at_start','worker_shutdown_reason']} for r in records if not r['passed']]
root.joinpath('summary.json').write_text(json.dumps(summary,indent=2)+'\n')
fields=['request_id','mode','concurrency','profile','case','outcome','http_status','error','verify_wall_ms','download_ms','client_wall_ms','resolved_worker','request_index','active_at_start','logged_active_at_start','peak_active_for_request','worker_shutdown_reason']
with (root/'requests.csv').open('w',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore',lineterminator='\n');writer.writeheader();writer.writerows(records)
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none'})
fig,axes=plt.subplots(1,2,figsize=(10,4.3),layout='constrained')
colors={'accepted':'#059669','busy':'#d97706','resource_limit':'#dc2626','other':'#6b7280'}
labels={'accepted':'Accepted','busy':'Busy (429)','resource_limit':'Resource limit (546)','other':'Other failure'}
for ax,mode in zip(axes,['guarded','unguarded']):
    bottom=[0]*4
    for outcome in colors:
        values=[]
        for c in [1,2,4,8]:
            d=summary['by_mode_concurrency'][mode][str(c)];o=d['outcomes']
            n=o.get(outcome,0) if outcome!='other' else sum(v for k,v in o.items() if k not in ['accepted','busy','resource_limit'])
            values.append(100*n/d['attempts'] if d['attempts'] else 0)
        ax.bar(range(4),values,bottom=bottom,color=colors[outcome],label=labels[outcome]);bottom=[x+y for x,y in zip(bottom,values)]
    ax.set_xticks(range(4),[f"{c}\n(n={summary['by_mode_concurrency'][mode][str(c)]['attempts']})" for c in [1,2,4,8]]);ax.set_ylim(0,104);ax.set_xlabel('Requests in flight');ax.set_ylabel('Percent of first attempts');ax.set_title(mode.capitalize());ax.grid(axis='y',alpha=.15)
fig.suptitle('SCI 1,000 files / 50 MB: guarded versus unguarded',fontsize=14)
handles,labels_=axes[0].get_legend_handles_labels();fig.legend(handles,labels_,loc='outside lower center',ncols=4,frameon=False,fontsize=9)
fig.savefig(root/'outcomes.png',dpi=180);fig.savefig(root/'outcomes.svg')
print(json.dumps({k:v for k,v in summary.items() if k not in ['by_mode_profile','failed_requests']},indent=2))
