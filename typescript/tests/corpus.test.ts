/**
 * Runs every conformance catalog case in-process (Node and Deno) and compares results with the
 * expected values using the runner's matching rule (expected keys must match exactly by type and value).
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { describe, test } from 'node:test';
import { type Case, type Json, runCase } from './conformance.ts';
import { CORPUS } from './helpers.ts';

function matches(actual: unknown, expected: unknown): boolean {
  if (expected === null) return actual === null;
  if (Array.isArray(expected))
    return Array.isArray(actual) && actual.length === expected.length && expected.every((e, i) => matches(actual[i], e));
  if (typeof expected === 'object') {
    if (typeof actual !== 'object' || actual === null || Array.isArray(actual)) return false;
    return Object.entries(expected).every(([k, v]) => k in actual && matches((actual as Record<string, unknown>)[k], v));
  }
  if (typeof expected === 'number')
    return typeof actual === 'number' && actual === expected && Number.isInteger(actual) === Number.isInteger(expected);
  return actual === expected;
}

const catalog = JSON.parse(await readFile(`${CORPUS}catalog.json`, 'utf8')) as { cases: (Case & { expected: Record<string, Json> })[] };
const kinds = [...new Set(catalog.cases.map((c) => c.kind))];

describe('conformance corpus', () => {
  for (const kind of kinds) {
    test(`${kind} cases`, async () => {
      const failures: string[] = [];
      const cases = catalog.cases.filter((c) => c.kind === kind);
      for (const c of cases) {
        const result = await runCase(c, CORPUS);
        for (const [k, v] of Object.entries(c.expected)) {
          if (!(k in result) || !matches(result[k], v))
            failures.push(`${c.id}: ${k}: expected ${JSON.stringify(v)}, got ${JSON.stringify(result[k])}`);
        }
      }
      assert.deepEqual(failures, [], `${failures.length}/${cases.length} ${kind} cases failed`);
    });
  }
});
