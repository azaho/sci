"""Regression tests for the audit tools and downstream adapter contract."""
import json
import unittest
from pathlib import Path
import audit
import independent
from run_adapter import compare


class TextPolicyTests(unittest.TestCase):
    def test_entire_unicode_codespace(self):
        # Cross-check exclusion-based validation against the RFC's independent
        # positive intervals for every code point, including all surrogates.
        for cp in range(0x110000):
            allowed = any(lo <= cp <= hi for lo, hi in independent.ASSIGNABLE)
            try:
                audit.text(chr(cp), blank=True, controls=True)
                actual = True
            except audit.Invalid:
                actual = False
            if actual != allowed:
                self.fail(f'Assignables disagreement at U+{cp:04X}')

    def test_multiline_exceptions_and_nonblank(self):
        for checker in (audit.text, lambda s, **kw: independent.check_text(
            s, kw.get('controls', False), not kw.get('blank', False))):
            for control in '\t\r\n':
                with self.assertRaises((audit.Invalid, independent.Invalid)):
                    checker('x' + control)
                checker('x' + control, controls=True)
            with self.assertRaises((audit.Invalid, independent.Invalid)):
                checker(' \t\r\n', controls=True)
            checker('\u00a0')
            checker('\u200d')


class AdapterContractTests(unittest.TestCase):
    cases = [{'id': 'one', 'expected': {'outcome': 'invalid', 'baseline': None}},
             {'id': 'two', 'expected': {'outcome': 'valid', 'baseline': False}}]

    def line(self, case_id, result):
        return json.dumps({'id': case_id, 'result': result}) + '\n'

    def test_exact_results_and_extra_diagnostics(self):
        lines = ''.join(self.line(c['id'], {**c['expected'], 'diagnostic': 'extra'}) for c in self.cases)
        self.assertEqual(compare(self.cases, lines), [])

    def test_missing_duplicate_unknown_and_incorrect_results(self):
        good = ''.join(self.line(c['id'], c['expected']) for c in self.cases)
        for output in ('', good + good, good + self.line('unknown', {}), 'not json\n',
                       self.line('one', {'outcome': 'invalid'}) + self.line('two', self.cases[1]['expected']),
                       self.line('one', self.cases[0]['expected']) + self.line('two', {'outcome':'valid','baseline':0})):
            self.assertTrue(compare(self.cases, output))


class SourceMapTests(unittest.TestCase):
    def test_every_original_case_is_accounted_for(self):
        root=Path(__file__).resolve().parents[1]
        catalog=json.loads((root/'0.1/catalog.json').read_text())
        ids={c['id'] for c in catalog['cases']}
        rows=json.loads((root/'source-map.json').read_text())['mapping']
        self.assertEqual(len(rows),414)
        self.assertEqual(len({(r['source'],r['source_id']) for r in rows}),414)
        self.assertEqual(sum(r['source']=='codex' for r in rows),225)
        self.assertEqual(sum(r['source']=='claude' for r in rows),189)
        for row in rows:
            self.assertTrue(row['cases'])
            self.assertLessEqual(set(row['cases']),ids)


if __name__ == '__main__':
    unittest.main()
