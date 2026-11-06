"""Read late shutdown telemetry for recorded soak deployments; deploy nothing."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re

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
    bench.function_id = record['id']
    bench.started = datetime.fromisoformat(record['started'])
    bench.logs()
    (bench.output / 'runtime-logs.json').rename(bench.output / 'runtime-logs-final.json')
