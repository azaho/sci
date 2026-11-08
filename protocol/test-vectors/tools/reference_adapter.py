#!/usr/bin/env python3
"""Example adapter using the independent checker for three offline case kinds."""
import json
from pathlib import Path
import sys
from independent import check_case

for line in sys.stdin:
    request = json.loads(line)
    case = request['case']
    if request['adapter_version'] != '1' or case['kind'] not in ('publication', 'claim', 'revocation'):
        raise SystemExit('Example adapter supports only publication, claim, and revocation cases.')
    result = check_case(case, Path(request['corpus']))
    print(json.dumps({'id': case['id'], 'result': result}), flush=True)
