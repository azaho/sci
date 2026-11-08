/** Interpretation of index lookup responses that carry bytes rather than JSON (§8.1.2, §8.1.3). */
import { incomplete } from './errors.ts';
import { frameTimestampFile } from './timestamp.ts';

export interface TimestampLookupResult {
  /** True if the index returned retained proofs. */
  readonly proofsRetained: boolean;
  /** A 404 never proves that no timestamp exists. */
  readonly timestampExistence: 'unknown' | 'proofs_returned';
  /** Complete DER values in the body (not yet verified). */
  readonly proofs: number;
  /** Returned proofs are untrusted until verified under §6.8. */
  readonly readersMustVerify: true;
  readonly body?: Uint8Array;
}

/** Interpret a §8.1.3 timestamp lookup response. Other statuses are `incomplete`. */
export function interpretTimestampLookup(status: number, body: Uint8Array): TimestampLookupResult {
  if (status === 404) return { proofsRetained: false, timestampExistence: 'unknown', proofs: 0, readersMustVerify: true };
  if (status !== 200) incomplete('lookup.status', '§8.1.3', 'Timestamp lookup did not succeed', { observed: status });
  const framed = frameTimestampFile(body);
  return { proofsRetained: framed.values > 0, timestampExistence: 'proofs_returned', proofs: framed.values, readersMustVerify: true, body };
}

export interface RevocationLookupResult {
  /** `found`: bytes returned (verify under §6.7); `unknown`: 404, which proves nothing. */
  readonly status: 'found' | 'unknown';
  readonly body?: Uint8Array;
}

/** Interpret a §8.1.2 revocation lookup response. */
export function interpretRevocationLookup(status: number, body: Uint8Array): RevocationLookupResult {
  if (status === 404) return { status: 'unknown' };
  if (status !== 200) incomplete('lookup.status', '§8.1.2', 'Revocation lookup did not succeed', { observed: status });
  return { status: 'found', body };
}
