import argparse,collections,csv,json,math,os,statistics
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args();root=a.root
records=[];events=[];deployments=[]
for folder in sorted(root.glob('wave-*')):
 if (folder/'results.json').exists():records.extend(json.loads((folder/'results.json').read_text()))
 log=folder/'runtime-logs-final.json'
 if not log.exists():log=folder/'runtime-logs.json'
 if log.exists():events.extend(json.loads(log.read_text()).get('result',[]))
 if (folder/'deployment.json').exists():deployments.append(json.loads((folder/'deployment.json').read_text()))
assert len({r['request_id'] for r in records})==len(records)
def quantile(xs,p):
 xs=sorted(xs);i=(len(xs)-1)*p;lo=math.floor(i);hi=math.ceil(i)
 return xs[lo]+(xs[hi]-xs[lo])*(i-lo)
def stats(group):
 good=[r for r in group if r['passed']]
 result={'attempts':len(group),'accepted':len(good),'outcomes':dict(collections.Counter(r['outcome'] for r in group))}
 for field in ['verify_wall_ms','download_ms','total_wall_ms','client_wall_ms']:
  values=[r[field] for r in good if isinstance(r.get(field),(int,float))]
  if values:result[field]={'n':len(values),'min':min(values),'p50':quantile(values,.5),'p95':quantile(values,.95),'p99':quantile(values,.99),'max':max(values),'mean':statistics.mean(values)}
 return result
summary={'overall':stats(records),'by_concurrency':{},'by_profile':{},'by_wave':{},'by_worker_use':{},'regions':dict(collections.Counter(r.get('region') or r.get('response_region') for r in records)),
         'distinct_workers':len({r['execution_id'] for r in records if r.get('execution_id')}),'distinct_cases':len({r['case'] for r in records}),
         'case_counts':dict(collections.Counter(r['case'] for r in records)),
         'failures':[r for r in records if not r['passed']]}
for key in ['concurrency','profile','wave']:
 for value in sorted({r[key] for r in records}):summary['by_'+key][str(value)]=stats([r for r in records if r[key]==value])
for name,pred in [('first',lambda r:r.get('request_index')==1),('reused',lambda r:isinstance(r.get('request_index'),int) and r['request_index']>1),('unknown',lambda r:r.get('request_index') is None)]:
 summary['by_worker_use'][name]=stats([r for r in records if pred(r)])
shutdown=[e for e in events if e['log_attributes'].get('event_type')=='Shutdown']
summary['shutdown_reasons']=dict(collections.Counter(e['log_attributes'].get('reason') for e in shutdown))
summary['boot_workers']=len({e['log_attributes']['execution_id'] for e in events if e['log_attributes'].get('event_type')=='Boot'})
summary['shutdown_workers']=len({e['log_attributes']['execution_id'] for e in shutdown})
summary['single_request_cpu_ms']=[]
for e in shutdown:
 at=e['log_attributes'];rs=[r for r in records if r.get('execution_id')==at.get('execution_id')]
 if len(rs)==1 and rs[0].get('request_index')==1 and rs[0]['passed'] and at.get('reason')=='EarlyDrop':
  summary['single_request_cpu_ms'].append({'request_id':rs[0]['request_id'],'profile':rs[0]['profile'],'concurrency':rs[0]['concurrency'],'cpu_ms':float(at['cpu_time_used'])})
measured_workers={r['execution_id'] for r in records if r.get('execution_id')}
shutdown_workers={e['log_attributes']['execution_id'] for e in shutdown}
summary['measured_workers_with_shutdown']=len(measured_workers & shutdown_workers)
summary['measured_workers_without_shutdown']=sorted(measured_workers - shutdown_workers)
summary['collection_errors']={}
for folder in sorted(root.glob('wave-*')):
 log=folder/'runtime-logs-final.json'
 if not log.exists():log=folder/'runtime-logs.json'
 if log.exists():summary['collection_errors'][folder.name]=json.loads(log.read_text()).get('collection_errors',[])
summary['sampled_memory_max_bytes']={k:max((r.get('memory_sampled_max',{}).get(k,0) for r in records),default=0) for k in ['heapUsed','heapTotal','external']}
summary['shutdown_cpu_ms_max']=max((float(e['log_attributes']['cpu_time_used']) for e in shutdown),default=None)
summary['shutdown_memory_bytes_max']=max((float(e['log_attributes'].get('memory_used.total',0)) for e in shutdown),default=None)
root.joinpath('summary.json').write_text(json.dumps(summary,indent=2)+'\n')
fields=['request_id','wave','concurrency','profile','case','outcome','http_status','error','started_at','completed_at','verify_wall_ms','download_ms','total_wall_ms','client_wall_ms','execution_id','request_index','region']
with (root/'requests.csv').open('w',newline='') as f:
 writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(sorted(records,key=lambda r:r['request_id']))
plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none'})
fig,axes=plt.subplots(1,2,figsize=(11,4.4),layout='constrained')
colors={1:'#2563eb',2:'#059669',4:'#d97706'}
for c in [1,2,4]:
 rs=[r for r in records if r['concurrency']==c];good=[r for r in rs if r['passed']]
 for ax,field in zip(axes,['verify_wall_ms','client_wall_ms']):
  xs=sorted(r[field]/1000 for r in good)
  if xs:ax.step(xs,[(i+1)/len(xs) for i in range(len(xs))],where='post',label=f'{c} in flight: {len(good)}/{len(rs)} accepted',color=colors[c],linewidth=1.8)
for ax,title in zip(axes,['Verification wall time','Client-observed request time']):
 ax.set_title(title);ax.set_xlabel('Seconds');ax.set_ylabel('Fraction of accepted requests');ax.set_ylim(0,1.01);ax.yaxis.set_major_formatter(PercentFormatter(1));ax.grid(alpha=.2);ax.legend(fontsize=8,loc='lower right')
axes[1].set_xscale('log')
fig.suptitle('SCI: 1,000 listed files, 50 MB · Empirical latency distributions',fontsize=14)
fig.savefig(root/'latency-ecdf.png',dpi=180);fig.savefig(root/'latency-ecdf.svg')
print(json.dumps({k:v for k,v in summary.items() if k not in ['failures','single_request_cpu_ms','case_counts']},indent=2))
