import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { verifyTimestampFile } from '../src/index.ts';

const fixture = async (name: string) => new Uint8Array(await readFile(new URL(`fixtures/freetsa/${name}`, import.meta.url)));
const proof = await fixture('proof.tsr');
const signature = await fixture('signature.bin');
const trust = {
  trustAnchors: [await fixture('ca.pem')],
  verificationTime: new Date('2026-11-09T07:00:00Z'),
  revocationPolicy: 'crl_if_available' as const,
};

test('real FreeTSA P-384/SHA-512 proof verifies, including on Deno 2.1.4', async () => {
  const result = await verifyTimestampFile(proof, signature, trust);
  assert.equal(result.outcome, 'valid');
  assert.equal(result.proofs[0]?.genTime, '2026-11-09T06:44:34Z');
  const altered = proof.slice();
  altered[altered.length - 1] = (altered[altered.length - 1] ?? 0) ^ 1;
  assert.equal((await verifyTimestampFile(altered, signature, trust)).outcome, 'invalid');
  const wrongSignature = signature.slice();
  wrongSignature[0] = (wrongSignature[0] ?? 0) ^ 1;
  assert.equal((await verifyTimestampFile(proof, wrongSignature, trust)).outcome, 'invalid');
  assert.equal((await verifyTimestampFile(proof, signature, { ...trust, trustAnchors: [] })).outcome, 'untrusted');
});

test('ECDSA fallback only handles NotSupportedError, and still verifies the signature', async () => {
  const original = crypto.subtle.verify;
  let mode = 'unsupported';
  let fallbackCalls = 0;
  crypto.subtle.verify = async function (...args: Parameters<SubtleCrypto['verify']>) {
    const algorithm = args[0];
    if (typeof algorithm !== 'string' && algorithm.name === 'ECDSA') {
      if (mode === 'false') return false;
      if (mode === 'other') throw new DOMException('bad input', 'OperationError');
      fallbackCalls++;
      throw new DOMException('Not implemented', 'NotSupportedError');
    }
    return original.apply(this, args);
  };
  try {
    assert.equal((await verifyTimestampFile(proof, signature, trust)).outcome, 'valid');
    assert.ok(fallbackCalls > 0);
    const altered = proof.slice();
    altered[altered.length - 1] = (altered[altered.length - 1] ?? 0) ^ 1;
    assert.equal((await verifyTimestampFile(altered, signature, trust)).outcome, 'invalid');
    mode = 'false';
    assert.equal((await verifyTimestampFile(proof, signature, trust)).outcome, 'invalid');
    mode = 'other';
    assert.equal((await verifyTimestampFile(proof, signature, trust)).outcome, 'invalid');
  } finally {
    crypto.subtle.verify = original;
  }
});
