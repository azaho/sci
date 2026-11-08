import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import {
  base64UrlDecodeStrict,
  base64UrlEncode,
  checkManifestPaths,
  decodePublicKeyX,
  isValidManifestPath,
  parsePublicationRef,
  publicJwkFromSeed,
  signEd25519,
  verifyEd25519,
} from '../src/index.ts';
import { enc, issueOf, SEED } from './helpers.ts';

const L = 2n ** 252n + 27742317777372353535851937790883648493n;

describe('Ed25519 keys and signatures (§5.1)', () => {
  const jwk = publicJwkFromSeed(SEED);
  const key = decodePublicKeyX(jwk.x);
  const msg = enc('message');
  const sig = signEd25519(SEED, msg);

  test('round trip', () => {
    assert.equal(issueOf(() => verifyEd25519(key, msg, sig)).outcome, 'valid');
    assert.equal(issueOf(() => verifyEd25519(key, enc('other'), sig)).code, 'signature.invalid');
  });

  test('rejects S >= L (noncanonical scalar)', () => {
    let s = 0n;
    for (let i = 63; i >= 32; i--) s = (s << 8n) | BigInt(sig[i] ?? 0);
    const t = s + L;
    const bad = sig.slice();
    let v = t;
    for (let i = 32; i < 64; i++) {
      bad[i] = Number(v & 0xffn);
      v >>= 8n;
    }
    if (v === 0n) assert.equal(issueOf(() => verifyEd25519(key, msg, bad)).code, 'signature.s_range');
  });

  test('rejects identity, small-order, and noncanonical encodings', () => {
    const identity = new Uint8Array(32);
    identity[0] = 1;
    assert.equal(issueOf(() => decodePublicKeyX(base64UrlEncode(identity))).code, 'key.point');
    // Small-order point of order 4: y = 0.
    assert.equal(issueOf(() => decodePublicKeyX(base64UrlEncode(new Uint8Array(32)))).code, 'key.point');
    // y >= p (noncanonical): 2^255 - 19 + 1 encodes y = 1 noncanonically.
    const nonCanon = new Uint8Array(32).fill(0xff);
    nonCanon[0] = 0xee;
    nonCanon[31] = 0x7f;
    assert.equal(issueOf(() => decodePublicKeyX(base64UrlEncode(nonCanon))).code, 'key.point');
    const rIdentity = sig.slice();
    rIdentity.set(identity, 0);
    assert.equal(issueOf(() => verifyEd25519(key, msg, rIdentity)).code, 'signature.r_point');
    assert.equal(issueOf(() => verifyEd25519(key, msg, sig.subarray(0, 63))).code, 'signature.length');
  });

  test('base64url strictness', () => {
    assert.equal(base64UrlDecodeStrict(`${jwk.x}=`), null);
    assert.equal(issueOf(() => decodePublicKeyX(`${jwk.x.slice(0, 42)}${jwk.x.endsWith('A') ? 'B' : 'A'}`)).outcome, 'invalid');
    assert.equal(base64UrlDecodeStrict('AB'), null, 'nonzero pad bits');
    assert.deepEqual(base64UrlDecodeStrict('AA'), new Uint8Array([0]));
    assert.equal(base64UrlDecodeStrict('A+'), null);
  });
});

describe('paths (§3.1)', () => {
  test('syntax', () => {
    for (const ok of ['README.md', 'files/a', 'files/COM10', 'files/CONSOLE.txt', `files/${'a'.repeat(249)}`])
      assert.ok(isValidManifestPath(ok), ok);
    for (const bad of [
      '/files/a',
      'files/../a',
      'files/./a',
      'files//a',
      'files/a/',
      'files/a.',
      'files/a b',
      'files/é',
      'files/a\\b',
      'files/a:b',
      'files/con',
      'files/Nul.txt',
      'files/lpt9/x',
      'other.txt',
      `files/${'a'.repeat(250)}`,
    ]) {
      assert.ok(!isValidManifestPath(bad), bad);
    }
  });
  test('case-insensitive conflicts', () => {
    assert.equal(issueOf(() => checkManifestPaths(['files/a', 'files/A'])).code, 'path.case_conflict');
    assert.equal(issueOf(() => checkManifestPaths(['files/a', 'files/A/b.txt'])).code, 'path.file_directory_conflict');
    assert.equal(issueOf(() => checkManifestPaths(['files/Data/a.csv', 'files/data/b.csv'])).code, 'path.directory_case');
    assert.equal(issueOf(() => checkManifestPaths(['files/d/a', 'files/d/b', 'sci.json'])).outcome, 'valid');
  });
});

describe('publication identifiers', () => {
  const d = 'a'.repeat(64);
  test('accepts the three protocol spellings only', () => {
    assert.equal(parsePublicationRef(d), d);
    assert.equal(parsePublicationRef(`sha256:${d}`), d);
    assert.equal(parsePublicationRef(`sci:sha256:${d}`), d);
    assert.equal(parsePublicationRef(d.toUpperCase()), null);
    assert.equal(parsePublicationRef(`sha256:${d}`, ['digest']), null);
    assert.equal(parsePublicationRef(d.slice(1)), null);
  });
});
