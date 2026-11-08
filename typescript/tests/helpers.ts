/** Shared test helpers (Node and Deno). */
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import {
  buildCanonicalZip,
  type CheckResult,
  publicJwkFromSeed,
  SciError,
  sha256Hex,
  signEd25519,
  utf8DecodeStrict,
} from '../src/index.ts';

export const CORPUS = fileURLToPath(new URL('../../protocol/test-vectors/0.1/', import.meta.url));
export const BASELINE_LICENSE_URL = new URL('../../protocol/sci-baseline-LICENSE.txt', import.meta.url);

export async function corpusBytes(rel: string): Promise<Uint8Array> {
  return new Uint8Array(await readFile(new URL(rel, new URL('../../protocol/test-vectors/0.1/', import.meta.url))));
}

export async function baselineLicense(): Promise<Uint8Array> {
  return new Uint8Array(await readFile(BASELINE_LICENSE_URL));
}

export const enc = (s: string): Uint8Array => new TextEncoder().encode(s);
export const dec = (b: Uint8Array): string => utf8DecodeStrict(b, 'test');

export const SEED = new Uint8Array(32).map((_, i) => i + 1);
export const OTHER_SEED = new Uint8Array(32).map((_, i) => 200 - i);

export interface TestPublication {
  readonly manifest: Uint8Array;
  readonly signature: Uint8Array;
  readonly files: Map<string, Uint8Array>;
  readonly zip: Uint8Array;
  readonly id: string;
  readonly digest: string;
  readonly loose: Map<string, Uint8Array>;
}

/** Build and sign a publication with the given payload files (sci.json and README.md defaulted). */
export function makePublication(extra: Record<string, Uint8Array> = {}, seed = SEED, metadata?: unknown): TestPublication {
  const files = new Map<string, Uint8Array>([
    ['sci.json', enc(JSON.stringify(metadata ?? { title: 'Test', abstract: 'An abstract.' }))],
    ['README.md', enc('# Hello\n')],
    ...Object.entries(extra),
  ]);
  const listed: Record<string, { sha256: string; bytes: number }> = {};
  for (const [p, d] of [...files].sort(([a], [b]) => (a < b ? -1 : 1))) listed[p] = { sha256: sha256Hex(d), bytes: d.length };
  const manifest = enc(JSON.stringify({ schema: 'sci-publication/0.1', public_key: publicJwkFromSeed(seed), files: listed }));
  const signature = signEd25519(seed, manifest);
  const zip = buildCanonicalZip(manifest, signature, files);
  const digest = sha256Hex(manifest);
  const loose = new Map<string, Uint8Array>([['manifest.json', manifest], ['manifest.json.sig', signature], ...files]);
  return { manifest, signature, files, zip, id: `sha256:${digest}`, digest, loose };
}

/** Assert that a function throws a SciError with the given outcome (and optional code). */
export function issueOf(fn: () => unknown): { outcome: string; code: string } {
  try {
    fn();
  } catch (error) {
    if (error instanceof SciError) return { outcome: error.issue.outcome, code: error.issue.code };
    throw error;
  }
  return { outcome: 'valid', code: '' };
}

export async function issueOfAsync(fn: () => Promise<unknown>): Promise<{ outcome: string; code: string }> {
  try {
    await fn();
  } catch (error) {
    if (error instanceof SciError) return { outcome: error.issue.outcome, code: error.issue.code };
    throw error;
  }
  return { outcome: 'valid', code: '' };
}

export function outcome<T>(r: CheckResult<T>): string {
  return r.outcome;
}
