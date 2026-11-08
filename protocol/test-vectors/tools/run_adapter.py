#!/usr/bin/env python3
"""Run any SCI implementation through the documented JSON-lines adapter contract.

This runner needs only Python's standard library. Expected results stay in the
runner; the adapter receives file paths, operation context, and case identity.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1] / '0.1'


def matches(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return all(key in actual and matches(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(matches(a, e) for a, e in zip(actual, expected))
    return actual == expected


def compare(cases, stdout):
    expected = {case['id']: case['expected'] for case in cases}
    seen = set()
    failures = []
    for line in stdout.splitlines():
        try:
            reply = json.loads(line)
            case_id = reply['id']
            result = reply['result']
            if not isinstance(case_id, str) or case_id not in expected:
                raise ValueError('unknown case ID')
            if case_id in seen:
                raise ValueError('duplicate result for ' + case_id)
            seen.add(case_id)
            if not isinstance(result, dict):
                raise ValueError('result must be an object')
            # Null is a value, not a substitute for an omitted required result.
            for key, value in expected[case_id].items():
                if key not in result or not matches(result[key], value):
                    failures.append(f'{case_id}: {key}: expected {value!r}, got {result.get(key)!r}')
        except (ValueError, KeyError, TypeError) as exc:
            failures.append('Malformed adapter response: ' + str(exc))
    failures.extend('Missing result: ' + case_id for case_id in sorted(expected.keys() - seen))
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=ROOT)
    parser.add_argument('--kind', action='append', help='Repeat to test selected case kinds; default is all.')
    parser.add_argument('--timeout', type=float, default=120, help='Whole adapter process deadline in seconds.')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('supply an adapter command after --')
    corpus = args.corpus.resolve()
    catalog = json.loads((corpus / 'catalog.json').read_text())
    kinds = {c['kind'] for c in catalog['cases']}
    if args.kind and not set(args.kind) <= kinds:
        parser.error('unknown case kind')
    cases = [c for c in catalog['cases'] if not args.kind or c['kind'] in args.kind]
    if not cases:
        parser.error('selection contains no cases')
    requests = []
    for case in cases:
        request = {'adapter_version': '1', 'corpus': str(corpus),
                   'case': {key: value for key, value in case.items() if key != 'expected'}}
        requests.append(json.dumps(request, ensure_ascii=True))
    try:
        completed = subprocess.run(command, input='\n'.join(requests)+'\n',
                                   text=True, capture_output=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit('Adapter exceeded the deadline; no passing result is claimed.')
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end='')
    if completed.returncode:
        raise SystemExit(f'Adapter exited with status {completed.returncode}.')
    failures = compare(cases, completed.stdout)
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        raise SystemExit(f'{len(failures)} adapter result errors.')
    print(f'Adapter passed {len(cases)} cases ({", ".join(sorted({c["kind"] for c in cases}))}).')


if __name__ == '__main__':
    main()
