// Smoke-test the vendored single-file bundle in the current runtime (Node or Deno).
import { readFile } from 'node:fs/promises';
import * as sci from '../dist/sci.bundle.js';

const corpus = new URL('../../protocol/test-vectors/0.1/', import.meta.url);
const zip = new Uint8Array(await readFile(new URL('publication/minimal-baseline/bundle.zip', corpus)));
const result = await sci.checkZipBundle(zip);
if (result.outcome !== 'valid' || result.value.baseline.baseline !== true)
  throw new Error(`bundle smoke test failed: ${JSON.stringify(result.issues)}`);
const tsDir = new URL('timestamp/timestamp-one/', corpus);
const ts = await sci.verifyPublicationTimestamps(
  new Uint8Array(await readFile(new URL('manifest.json', tsDir))),
  new Uint8Array(await readFile(new URL('signature.bin', tsDir))),
  new Uint8Array(await readFile(new URL('proofs.tsr', tsDir))),
  {
    trustAnchors: [new Uint8Array(await readFile(new URL('pki/root.pem', corpus)))],
    crls: [new Uint8Array(await readFile(new URL('pki/good.crl.pem', corpus)))],
    verificationTime: new Date('2026-11-07T12:00:00Z'),
  },
);
if (ts.outcome !== 'valid') throw new Error('bundle timestamp smoke test failed');
const runtime = typeof Deno !== 'undefined' ? `Deno ${Deno.version.deno}` : `Node ${process.version}`;
console.log(`dist/sci.bundle.js OK under ${runtime}: ${result.value.manifest.id}`);
