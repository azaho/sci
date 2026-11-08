/**
 * SCI §5.1 keys and signatures.
 *
 * - Public keys are RFC 8037 OKP/Ed25519 JWKs with exactly `kty`, `crv`, `x`.
 * - `x` must be canonical unpadded base64url of 32 bytes.
 * - Signatures are 64 raw bytes, verified under RFC 8032 §5.1.7 with strict (non-ZIP-215)
 *   decoding: canonical point encodings and S < L.
 * - Additionally, A and R must be non-identity members of the prime-order subgroup.
 */
import { ed25519 } from '@noble/curves/ed25519.js';
import { base64UrlDecodeStrict, base64UrlEncode } from './bytes.ts';
import { check, fail } from './errors.ts';
import { isObject, type JsonValue } from './json.ts';

export interface PublicJwk {
  readonly kty: 'OKP';
  readonly crv: 'Ed25519';
  readonly x: string;
}

const L = 2n ** 252n + 27742317777372353535851937790883648493n;

/** Validate a parsed JWK object (§5.1) and return the canonical JWK and raw key bytes. */
export function parsePublicJwk(value: JsonValue | undefined, path = 'public_key'): { jwk: PublicJwk; bytes: Uint8Array } {
  if (!isObject(value)) fail('key.type', '§5.1', 'Public key must be a JSON object', { path });
  for (const k of value.keys()) {
    check(k === 'kty' || k === 'crv' || k === 'x', 'key.extra_field', '§5.1', 'Only kty, crv, and x are permitted', {
      path: `${path}/${k}`,
    });
  }
  const kty = value.get('kty');
  const crv = value.get('crv');
  const x = value.get('x');
  check(kty === 'OKP', 'key.kty', '§5.1', 'kty must be "OKP"', { path: `${path}/kty`, observed: kty });
  check(crv === 'Ed25519', 'key.crv', '§5.1', 'crv must be "Ed25519"', { path: `${path}/crv`, observed: crv });
  if (typeof x !== 'string') fail('key.x', '§5.1', 'x must be a string', { path: `${path}/x` });
  const bytes = decodePublicKeyX(x, `${path}/x`);
  return { jwk: { kty: 'OKP', crv: 'Ed25519', x }, bytes };
}

/** Decode a JWK `x` value, rejecting noncanonical base64url and invalid points. */
export function decodePublicKeyX(x: string, path = 'x'): Uint8Array {
  const bytes = /^[A-Za-z0-9_-]{43}$/.test(x) ? base64UrlDecodeStrict(x) : null;
  if (bytes?.length !== 32 || base64UrlEncode(bytes) !== x) {
    fail('key.x_encoding', '§5.1', 'x must be canonical unpadded base64url of 32 bytes', { path, observed: x });
  }
  checkSubgroupPoint(bytes, 'key.point', path);
  return bytes;
}

/** True if `x` is a syntactically canonical key whose point passes the §5.1 checks. */
export function isCanonicalPublicKeyX(x: string): boolean {
  try {
    decodePublicKeyX(x);
    return true;
  } catch {
    return false;
  }
}

function checkSubgroupPoint(encoded: Uint8Array, code: string, path: string): void {
  let point: ReturnType<typeof ed25519.Point.fromBytes>;
  try {
    // zip215 = false: reject y >= p and other noncanonical encodings (RFC 8032 decoding).
    point = ed25519.Point.fromBytes(encoded, false);
  } catch {
    fail(code, '§5.1', 'Point is not a canonical Ed25519 encoding', { path });
  }
  check(!point.is0(), code, '§5.1', 'Point must not be the identity', { path });
  check(point.isTorsionFree(), code, '§5.1', 'Point must be in the prime-order subgroup', { path });
}

/**
 * Verify a raw 64-byte Ed25519 signature over `message` with the given 32-byte public key.
 * Throws `invalid` issues on any failure.
 */
export function verifyEd25519(publicKey: Uint8Array, message: Uint8Array, signature: Uint8Array, path = 'signature'): void {
  check(signature.length === 64, 'signature.length', '§3, §5.1', 'Signature must be exactly 64 raw bytes', {
    path,
    observed: signature.length,
  });
  check(publicKey.length === 32, 'key.length', '§5.1', 'Public key must be 32 bytes', { path });
  checkSubgroupPoint(publicKey, 'key.point', path);
  checkSubgroupPoint(signature.subarray(0, 32), 'signature.r_point', path);
  let s = 0n;
  for (let i = 63; i >= 32; i--) s = (s << 8n) | BigInt(signature[i] ?? 0);
  check(s < L, 'signature.s_range', '§5.1', 'Signature scalar S must be less than L', { path });
  let ok = false;
  try {
    ok = ed25519.verify(signature, message, publicKey, { zip215: false });
  } catch {
    ok = false;
  }
  check(ok, 'signature.invalid', '§5.1', 'Ed25519 signature verification failed', { path });
}

/** Sign with a 32-byte Ed25519 seed (RFC 8032 deterministic). Intended for publishing tools and tests. */
export function signEd25519(seed: Uint8Array, message: Uint8Array): Uint8Array {
  return ed25519.sign(message, seed);
}

/** Derive the public JWK for a 32-byte Ed25519 seed. */
export function publicJwkFromSeed(seed: Uint8Array): PublicJwk {
  return { kty: 'OKP', crv: 'Ed25519', x: base64UrlEncode(ed25519.getPublicKey(seed)) };
}
