"""Retrieve runtime telemetry for a recorded benchmark; deploy nothing."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re

from run import Bench

parser = argparse.ArgumentParser()
parser.add_argument("deployment", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
record = json.loads(args.deployment.read_text())
assert re.fullmatch(r"[0-9a-f-]{36}", record["id"])
bench = Bench(args.output)
bench.function_id = record["id"]
bench.started = datetime.fromisoformat(record["started"])
bench.logs()
