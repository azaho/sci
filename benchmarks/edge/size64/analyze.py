"""Join each single verification to its worker's platform CPU/shutdown telemetry."""
import argparse,collections,csv,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args();root=a.root
rows=[];boots=set();stops=set();collection_errors={}
for folder in sorted(root.glob('wave-*')):
 if not (folder/'results.json').exists():continue
 results=json.loads((folder/'results.json').read_text());assert len(results)==1;r=results[0]
 events=[];log=folder/'runtime-logs-final.json'
 if log.exists():
  data=json.loads(log.read_text());events=data['result'];collection_errors[folder.name]=data['collection_errors']
 starts=[];shutdown={}
 for e in events:
  attr=e['log_attributes'];worker=attr.get('execution_id')
  if attr.get('event_type')=='Boot':boots.add(worker)
  if attr.get('event_type')=='Shutdown':stops.add(worker);shutdown[worker]=attr
  try:message=json.loads(e['event_message'])
  except (TypeError,ValueError):continue
  if isinstance(message,dict) and 'sci_request_start' in message:starts.append(message['sci_request_start'])
 if events:
  assert len(starts)==1 and starts[0]['request_id']==r['request_id'] and starts[0]['verification_call']==1
 worker=r.get('execution_id') or (starts[0]['execution_id'] if starts else None)
 if r.get('execution_id') and starts:assert r['execution_id']==starts[0]['execution_id']
 stop=shutdown.get(worker,{})
 row={k:r.get(k) for k in ['case','trial','request_id','passed','http_status','error','verify_wall_ms','download_ms','client_wall_ms']}
 row.update(worker=worker,worker_cpu_ms=float(stop['cpu_time_used']) if stop else None,
   shutdown_reason=stop.get('reason'),shutdown_memory_bytes=int(stop.get('memory_used.total',0)) if stop else None,
   verification_starts=len(starts),**{k:r['fixture'][k] for k in ['zip_bytes','payload_bytes','manifest_bytes','metadata_bytes','readme_bytes','largest_file_bytes','files']})
 rows.append(row)
summary={'measurements':rows,'boot_workers':len(boots),'shutdown_workers':len(stops),'missing_shutdowns':sorted(boots-stops),'collection_errors':collection_errors}
summary['by_case']={name:{'attempts':len(rs:=[r for r in rows if r['case']==name]),'accepted':sum(r['passed'] for r in rs),
 'worker_cpu_ms':[r['worker_cpu_ms'] for r in rs],'verify_wall_ms':[r['verify_wall_ms'] for r in rs],
 'shutdown_reasons':dict(collections.Counter(r['shutdown_reason'] for r in rs))} for name in sorted({r['case'] for r in rows})}
(root/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
if rows:
 with (root/'measurements.csv').open('w',newline='') as f:
  writer=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');writer.writeheader();writer.writerows(rows)
print(json.dumps({k:v for k,v in summary.items() if k!='measurements'},indent=2))
