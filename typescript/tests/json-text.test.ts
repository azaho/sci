import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { checkText, codePointLength, isUnicodeAssignable, JsonNumber, parseJsonBytes, toPlain } from '../src/index.ts';
import { enc, issueOf } from './helpers.ts';

describe('strict JSON (§7)', () => {
  test('parses RFC 8259 values and preserves number tokens', () => {
    const v = parseJsonBytes(enc(' {"a":[1,-0.5e+3,true,false,null,"x\\u00e9\\ud83d\\ude00"],"b":{}} '));
    const plain = toPlain(v) as { a: unknown[]; b: object };
    assert.deepEqual(plain.a, [1, -500, true, false, null, 'xé😀']);
    assert.ok(v instanceof Map);
    const n = (v.get('a') as unknown[])[0];
    assert.ok(n instanceof JsonNumber && n.raw === '1' && n.isCanonicalNonnegativeInteger());
  });

  const invalid: [string, string][] = [
    ['{"a":1,"a":2}', 'json.duplicate_key'],
    ['{"a":1,"\\u0061":2}', 'json.duplicate_key'],
    ['{"x":{"k":1,"k":1}}', 'json.duplicate_key'],
    ['[{"x":[{"k":1,"k":1}]}]', 'json.duplicate_key'],
    ['"\\ud800"', 'json.unpaired_surrogate'],
    ['"\\udc00"', 'json.unpaired_surrogate'],
    ['"\\ud800\\u0041"', 'json.unpaired_surrogate'],
    ['{"a":1,}', 'json.syntax'],
    ['[1,]', 'json.syntax'],
    ['01', 'json.bad_number'],
    ['1.', 'json.bad_number'],
    ['+1', 'json.syntax'],
    ['NaN', 'json.syntax'],
    ["{'a':1}", 'json.syntax'],
    ['"a\tb"', 'json.control_character'],
    ['"\\x"', 'json.bad_escape'],
    ['{} {}', 'json.trailing_data'],
    ['', 'json.unexpected_end'],
    ['"abc', 'json.unterminated_string'],
    ['/*c*/{}', 'json.syntax'],
  ];
  for (const [text, code] of invalid) {
    test(`rejects ${JSON.stringify(text)}`, () => {
      assert.deepEqual(
        issueOf(() => parseJsonBytes(enc(text))),
        { outcome: 'invalid', code },
      );
    });
  }

  test('rejects a byte-order mark and malformed UTF-8', () => {
    assert.equal(issueOf(() => parseJsonBytes(new Uint8Array([0xef, 0xbb, 0xbf, 0x7b, 0x7d]))).code, 'json.bom');
    assert.equal(issueOf(() => parseJsonBytes(new Uint8Array([0x22, 0xc0, 0xaf, 0x22]))).code, 'text.invalid_utf8');
    assert.equal(issueOf(() => parseJsonBytes(new Uint8Array([0x22, 0xed, 0xa0, 0x80, 0x22]))).code, 'text.invalid_utf8');
  });

  test('nesting beyond the local limit is incomplete, not invalid', () => {
    const deep = `${'['.repeat(200)}${']'.repeat(200)}`;
    assert.deepEqual(
      issueOf(() => parseJsonBytes(enc(deep))),
      { outcome: 'incomplete', code: 'budget.json_depth' },
    );
    assert.equal(issueOf(() => parseJsonBytes(enc(deep), { maxDepth: 300 })).outcome, 'valid');
  });
});

describe('text rules (§4.2, RFC 9839 Assignables)', () => {
  test('matches the RFC 9839 §4.3 intervals over the whole code space', () => {
    const ranges: [number, number][] = [
      [0x9, 0x9],
      [0xa, 0xa],
      [0xd, 0xd],
      [0x20, 0x7e],
      [0xa0, 0xd7ff],
      [0xe000, 0xfdcf],
      [0xfdf0, 0xfffd],
    ];
    for (let plane = 1; plane <= 16; plane++) ranges.push([plane * 0x10000, plane * 0x10000 + 0xfffd]);
    let r = 0;
    for (let cp = 0; cp <= 0x10ffff; cp++) {
      while (r < ranges.length && cp > (ranges[r] as [number, number])[1]) r++;
      const range = ranges[r];
      const expected = range !== undefined && cp >= range[0];
      if (isUnicodeAssignable(cp) !== expected) assert.fail(`mismatch at U+${cp.toString(16)}`);
    }
  });

  test('controls only where permitted; nonblank; code point limits', () => {
    assert.equal(issueOf(() => checkText('a\tb')).code, 'text.forbidden_control');
    assert.equal(issueOf(() => checkText('a\tb', { controls: true })).outcome, 'valid');
    assert.equal(issueOf(() => checkText(' \t\r\n', { controls: true, nonblank: true })).code, 'text.blank');
    assert.equal(issueOf(() => checkText(' ', { nonblank: true })).outcome, 'valid');
    assert.equal(issueOf(() => checkText('‍', { nonblank: true })).outcome, 'valid');
    assert.equal(issueOf(() => checkText('\u0085')).code, 'text.not_assignable');
    assert.equal(issueOf(() => checkText('﷐')).code, 'text.not_assignable');
    assert.equal(issueOf(() => checkText('\ud800')).code, 'text.not_assignable');
    assert.equal(codePointLength('😀a'), 2);
    assert.equal(issueOf(() => checkText('😀'.repeat(3), { maxCodePoints: 3 })).outcome, 'valid');
    assert.equal(issueOf(() => checkText('😀'.repeat(4), { maxCodePoints: 3 })).code, 'text.too_long');
  });
});
