import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import {
  applyIndexEvent,
  EMPTY_INDEX_STATE,
  frameTimestampFile,
  type IndexPolicyState,
  keyStatus,
  locationCount,
  parseRfc3339,
  publicJwkFromSeed,
  signEd25519,
  timestampStatus,
  verifyClaim,
  verifyPublicationTimestamps,
  verifyRevocation,
  verifyTimestampFile,
} from '../src/index.ts';
import { corpusBytes, enc, issueOf, OTHER_SEED, SEED } from './helpers.ts';

describe('claims (§5.2, §6.6) and revocations (§5.3, §6.7)', () => {
  const jwk = publicJwkFromSeed(SEED);
  const claim = (fields: Record<string, unknown>) =>
    enc(
      JSON.stringify({
        schema: 'sci-key-claim/0.1',
        public_key: jwk,
        account: { type: 'domain', id: 'example.org' },
        expires: '2026-12-01T00:00:00Z',
        ...fields,
      }),
    );
  const base = {
    expectedPublicKeyX: jwk.x,
    requestedAccount: { type: 'domain', id: 'example.org' },
    now: new Date('2026-11-07T12:00:00Z'),
    knownRevocation: false,
    retrievedFromAccountLocation: true,
  };

  test('valid claim and each failing §6.6 check', () => {
    const c = claim({});
    assert.equal(issueOf(() => verifyClaim(c, signEd25519(SEED, c), base)).outcome, 'valid');
    assert.equal(issueOf(() => verifyClaim(c, signEd25519(OTHER_SEED, c), base)).code, 'signature.invalid');
    assert.equal(
      issueOf(() => verifyClaim(c, signEd25519(SEED, c), { ...base, now: new Date('2026-12-01T00:00:00Z') })).code,
      'claim.expired',
    );
    assert.equal(issueOf(() => verifyClaim(c, signEd25519(SEED, c), { ...base, knownRevocation: true })).code, 'claim.key_revoked');
    assert.equal(
      issueOf(() => verifyClaim(c, signEd25519(SEED, c), { ...base, retrievedFromAccountLocation: false })).code,
      'claim.not_hosted',
    );
    assert.equal(
      issueOf(() => verifyClaim(c, signEd25519(SEED, c), { ...base, requestedAccount: { type: 'github', id: 'example.org' } })).code,
      'claim.account_mismatch',
    );
    const offset = claim({ expires: '2026-11-07T13:00:00+02:00', note: 'tab\tok' });
    assert.equal(issueOf(() => verifyClaim(offset, signEd25519(SEED, offset), base)).code, 'claim.expired');
    const bad = claim({ expires: '2026-12-01' });
    assert.equal(issueOf(() => verifyClaim(bad, signEd25519(SEED, bad), base)).code, 'claim.expires');
  });

  test('publisher names preserve Unicode and reject invalid display text', () => {
    for (const name of [
      'Andrii Zahorodnii',
      'Massachusetts Institute of Technology',
      '\u674e\u660e',
      '\u0645\u0624\u0633\u0633\u0629 \u0627\u0644\u0628\u062d\u062b',
      '\u092a\u0930\u0940\u0915\u094d\u0937\u0923',
      'e\u0301',
      'a\u200cb',
      'a\u200db',
      '\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00',
    ]) {
      const bytes = claim({ schema: 'sci-key-claim/0.1', name });
      assert.equal(verifyClaim(bytes, signEd25519(SEED, bytes), base).name, name);
    }
    for (const name of [
      '',
      ' ',
      '\u00a0',
      ' Name',
      'Name ',
      '\u200d',
      'a\nb',
      'a\rb',
      'a\tb',
      'a\u2028b',
      'a\u2029b',
      'a\u202eb',
      'a\u2066b',
      'a\u200bb',
      'a\ufeffb',
      '\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00\ud801\udc00',
    ]) {
      const bytes = claim({ schema: 'sci-key-claim/0.1', name });
      assert.equal(issueOf(() => verifyClaim(bytes, signEd25519(SEED, bytes), base)).outcome, 'invalid');
    }
    const nameless = claim({ schema: 'sci-key-claim/0.1' });
    assert.equal(verifyClaim(nameless, signEd25519(SEED, nameless), base).name, undefined);
  });

  test('RFC 3339 parsing', () => {
    assert.deepEqual(parseRfc3339('2026-11-07T12:00:00.5z'), { seconds: Date.UTC(2026, 10, 7, 12) / 1000, nanos: 500_000_000 });
    assert.equal(parseRfc3339('2026-02-29T00:00:00Z'), null);
    assert.equal(parseRfc3339('2026-11-07 12:00:00Z'), null);
    const y50 = new Date(0);
    y50.setUTCFullYear(50, 0, 1);
    assert.equal(parseRfc3339('0050-01-01T00:00:00Z')?.seconds, y50.getTime() / 1000);
  });

  test('revocation', () => {
    const r = enc(JSON.stringify({ schema: 'sci-key-revocation/0.1', public_key: jwk }));
    assert.equal(verifyRevocation(r, signEd25519(SEED, r), { expectedPublicKeyX: jwk.x }).publicKey.x, jwk.x);
    assert.equal(
      issueOf(() => verifyRevocation(r, signEd25519(SEED, r), { expectedPublicKeyX: publicJwkFromSeed(OTHER_SEED).x })).code,
      'revocation.key_mismatch',
    );
    const unknown = enc(JSON.stringify({ schema: 'sci-key-revocation/0.2', public_key: jwk }));
    assert.equal(issueOf(() => verifyRevocation(unknown, signEd25519(SEED, unknown))).outcome, 'unsupported');
  });
});

const dir = 'timestamp/timestamp-two-authorities/';
const tsr = await corpusBytes(`${dir}proofs.tsr`);
const sig = await corpusBytes(`${dir}signature.bin`);
const manifest = await corpusBytes(`${dir}manifest.json`);
const root = await corpusBytes('pki/root.pem');
const good = await corpusBytes('pki/good.crl.pem');
const revoked = await corpusBytes('pki/revoked-a.crl.pem');
const when = new Date('2026-11-07T12:00:00Z');

describe('timestamps (§6.8)', () => {
  test('two verified proofs with genTime, accuracy, and TSA identity', async () => {
    const r = await verifyPublicationTimestamps(manifest, sig, tsr, { trustAnchors: [root], crls: [good], verificationTime: when });
    assert.equal(r.outcome, 'valid');
    assert.equal(r.proofs.length, 2);
    assert.equal(r.proofs[0]?.genTime, '2026-11-07T12:00:00Z');
    assert.equal(r.proofs[0]?.accuracySeconds, 1);
    assert.equal(r.proofs[0]?.tsa?.subject, 'CN=SCI TEST ONLY tsa-a');
    assert.equal(r.proofs[1]?.tsa?.subject, 'CN=SCI TEST ONLY tsa-b');
  });

  test('trust, revocation, time, and imprint policy', async () => {
    assert.equal((await verifyTimestampFile(tsr, sig, { trustAnchors: [], crls: [good], verificationTime: when })).outcome, 'untrusted');
    const rev = await verifyTimestampFile(tsr, sig, { trustAnchors: [root], crls: [revoked], verificationTime: when });
    assert.deepEqual(
      rev.proofs.map((p) => p.result),
      ['invalid', 'verified'],
    );
    const noCrl = await verifyTimestampFile(tsr, sig, { trustAnchors: [root], verificationTime: when });
    assert.equal(noCrl.outcome, 'incomplete');
    assert.equal(
      (await verifyTimestampFile(tsr, sig, { trustAnchors: [root], verificationTime: when, revocationPolicy: 'none' })).outcome,
      'valid',
    );
    const stale = await verifyTimestampFile(tsr, sig, {
      trustAnchors: [root],
      crls: [good],
      verificationTime: new Date('2027-01-01T00:00:00Z'),
    });
    assert.equal(stale.outcome, 'incomplete', 'CRL nextUpdate passed');
    const wrongSig = sig.slice();
    wrongSig[0] = (wrongSig[0] ?? 0) ^ 1;
    assert.equal(
      (await verifyTimestampFile(tsr, wrongSig, { trustAnchors: [root], crls: [good], verificationTime: when })).outcome,
      'invalid',
    );
  });

  test('framing stops at incomplete values and never scans ahead', async () => {
    const one = tsr.subarray(0, 1451);
    const withTail = new Uint8Array([...one, 0x30, 0x84, 0x7f, 0xff, 0xff, 0xff, ...one]);
    const r = await verifyTimestampFile(withTail, sig, { trustAnchors: [root], crls: [good], verificationTime: when });
    assert.equal(r.outcome, 'partial');
    assert.equal(r.framing, 'malformed');
    assert.equal(r.stopOffset, 1451);
    assert.deepEqual(frameTimestampFile(new Uint8Array([0x30, 0x81, 0x05])), { values: 0, stopOffset: 0, framing: 'malformed' });
    assert.equal((await verifyTimestampFile(new Uint8Array(), sig, { trustAnchors: [root] })).outcome, 'invalid');
  });
});

describe('index policy state (§5.3, §6.5, §8.1)', () => {
  const x = 'key';
  const run = (events: Parameters<typeof applyIndexEvent>[1][]): { state: IndexPolicyState; last?: string } => {
    let state = EMPTY_INDEX_STATE;
    let last: string | undefined;
    for (const e of events) {
      const r = applyIndexEvent(state, e);
      state = r.state;
      if (r.admission) last = r.admission.decision === 'reject' ? `reject:${r.admission.reason}` : 'accept';
    }
    return last === undefined ? { state } : { state, last };
  };
  test('revocation blocks new admission but keeps existing entries', () => {
    assert.equal(
      run([
        { type: 'revocation_verified', keyX: x },
        { type: 'timestamp_verified', id: 'p' },
        { type: 'admission_requested', id: 'p', keyX: x },
      ]).last,
      'reject:revoked_key',
    );
    const after = run([
      { type: 'admission_requested', id: 'p', keyX: x },
      { type: 'revocation_verified', keyX: x },
      { type: 'revocation_source_removed', keyX: x },
    ]);
    assert.ok(after.state.catalog.has('p'));
    assert.equal(keyStatus(after.state, x), 'revoked');
  });
  test('404s prove nothing; registrations are idempotent', () => {
    const s = run([
      { type: 'revocation_lookup_not_found', keyX: x },
      { type: 'timestamp_lookup_not_found', id: 'p' },
    ]).state;
    assert.equal(keyStatus(s, x), 'unknown');
    assert.equal(timestampStatus(s, 'p'), 'unknown');
    const r = run([
      { type: 'location_registered', id: 'p', descriptor: 'https://a' },
      { type: 'location_registered', id: 'p', descriptor: 'https://a' },
      { type: 'location_registered', id: 'p', descriptor: 'https://b' },
    ]).state;
    assert.equal(locationCount(r), 2);
    assert.equal(r.registrations.size, 1);
  });
});
