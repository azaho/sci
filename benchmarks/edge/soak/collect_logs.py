"""Read late shutdown telemetry for recorded soak deployments; deploy nothing."""
import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
import time

from run import Soak

parser = argparse.ArgumentParser()
parser.add_argument('results', type=Path)
parser.add_argument('output', type=Path)
args = parser.parse_args()
deployments = sorted(args.results.glob('wave-*/deployment.json'))
assert len(deployments) == 4, 'Need all four deployments'
for path in deployments:
    record = json.loads(path.read_text())
    assert re.fullmatch(r'[0-9a-f-]{36}', record['id'])
    bench = Soak(args.output / path.parent.name)
    query = ("select timestamp, id, source, event_message, log_attributes from logs "
             "where source = 'function_logs' "
             f"and position(toString(log_attributes), '{record['id']}') > 0 "
             "order by timestamp asc limit 1000")
    events = {}
    retries = []

    def collect(start, end):
        for attempt in range(4):
            try:
                response = bench.call('GET', bench.management + '/analytics/endpoints/logs', management=True,
                    params={'sql': query, 'iso_timestamp_start': start.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                            'iso_timestamp_end': end.isoformat(timespec='milliseconds').replace('+00:00', 'Z')}).json()
                break
            except Exception as error:
                if getattr(error, 'status', None) != 429 or attempt == 3:
                    raise
                retries.append({'error': 'log_api_rate_limit', 'wait_seconds': 60})
                time.sleep(60)
        rows = response.get('result')
        assert isinstance(rows, list), 'Log query returned no result array'
        if len(rows) >= 1000:
            assert end - start > timedelta(seconds=1), 'Log density exceeds bounded query capacity'
            middle = start + (end - start) / 2
            collect(start, middle)
            collect(middle, end)
        else:
            for row in rows:
                events[row['id']] = row
        time.sleep(3)

    collect(datetime.fromisoformat(record['started']) - timedelta(minutes=1), datetime.now(timezone.utc))
    (bench.output / 'runtime-logs-final.json').write_text(json.dumps({
        'result': list(events.values()), 'collection_errors': [], 'collection_retries': retries,
    }, indent=2) + '\n')
    print(f'{path.parent.name}: {len(events)} events, {len(retries)} rate-limit retries.', flush=True)
