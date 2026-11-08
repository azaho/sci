/**
 * SCI conformance adapter (JSON-lines contract of protocol/test-vectors/tools/run_adapter.py).
 * Reads one request per stdin line and writes one result per stdout line; logs go to stderr.
 */
import { createInterface } from 'node:readline';
import { type Case, runCase } from './conformance.ts';

const rl = createInterface({ input: process.stdin, crlfDelay: Number.POSITIVE_INFINITY });
for await (const line of rl) {
  if (!line.trim()) continue;
  const req = JSON.parse(line) as { adapter_version: string; corpus: string; case: Case };
  if (req.adapter_version !== '1') throw new Error(`unsupported adapter version ${req.adapter_version}`);
  const started = Date.now();
  const result = await runCase(req.case, req.corpus);
  if (process.env.SCI_ADAPTER_TIMING && Date.now() - started > 200) console.error(`${req.case.id}: ${Date.now() - started} ms`);
  process.stdout.write(`${JSON.stringify({ id: req.case.id, result })}\n`);
}
