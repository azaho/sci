/** SCI §3 manifest and §6.1 manifest checks. */
import { type Budget, resolveBudget } from './budget.ts';
import { toHex } from './bytes.ts';
import { type PublicJwk, parsePublicJwk, verifyEd25519 } from './ed25519.ts';
import { check, fail, incomplete } from './errors.ts';
import { sha256 } from './hash.ts';
import { checkFields, checkRecordSchema, parsePublicationRef, SCHEMAS } from './ids.ts';
import { isObject, JsonNumber, parseJsonBytes } from './json.ts';
import { checkManifestPaths } from './paths.ts';

export interface ManifestEntry {
  readonly sha256: string;
  readonly bytes: number;
}

export interface Manifest {
  /** Publication identifier `sha256:<digest>`. */
  readonly id: string;
  /** Bare digest (§8.1.1). */
  readonly digest: string;
  readonly schema: typeof SCHEMAS.publication;
  readonly publicKey: PublicJwk;
  readonly publicKeyBytes: Uint8Array;
  /** Listed files in manifest order. */
  readonly files: ReadonlyMap<string, ManifestEntry>;
  /** Exact manifest bytes (hashes and signatures are always checked against these). */
  readonly bytes: Uint8Array;
}

export interface ManifestOptions {
  /** Expected publication ID in any protocol spelling (`sha256:<hex>`, bare digest, or `sci:sha256:`). */
  readonly expectedId?: string | undefined;
  readonly budget?: Partial<Budget> | undefined;
}

const MAX_SAFE = 9007199254740991n;

/** Compute the publication identifier `sha256:<hex>` of exact manifest bytes (§2). */
export function publicationIdOf(manifestBytes: Uint8Array): string {
  return `sha256:${toHex(sha256(manifestBytes))}`;
}

/**
 * Parse and validate manifest bytes (§3, §3.1, §7) and check the expected ID. Does not verify the
 * signature; use {@link verifyManifest} for the complete §6.1 checks.
 */
export function parseManifest(bytes: Uint8Array, options: ManifestOptions = {}): Manifest {
  const budget = resolveBudget(options.budget);
  if (bytes.length > budget.maxManifestBytes) {
    incomplete('budget.manifest_bytes', '§6', 'manifest.json exceeds the local size budget', {
      path: 'manifest.json',
      observed: bytes.length,
    });
  }
  const value = parseJsonBytes(bytes, { maxDepth: budget.maxJsonDepth, path: 'manifest.json' });
  const obj = checkRecordSchema(value, SCHEMAS.publication, '§3', 'manifest.json');
  checkFields(obj, ['schema', 'public_key', 'files'], [], '§3', '');
  const digest = toHex(sha256(bytes));
  if (options.expectedId !== undefined) {
    const expected = parsePublicationRef(options.expectedId);
    check(expected !== null, 'manifest.expected_id_syntax', '§6.1', 'Expected publication ID is malformed', {
      observed: options.expectedId,
    });
    check(expected === digest, 'manifest.id_mismatch', '§6.1', 'Manifest hash does not match the expected publication ID', {
      observed: `sha256:${digest}`,
    });
  }
  const { jwk, bytes: keyBytes } = parsePublicJwk(obj.get('public_key'), '/public_key');
  const files = obj.get('files');
  if (!isObject(files)) fail('manifest.files_type', '§3', 'files must be a JSON object', { path: '/files' });
  if (files.size > budget.maxEntries) {
    incomplete('budget.entries', '§6', 'Manifest lists more files than the local budget', { observed: files.size });
  }
  checkManifestPaths(files.keys());
  check(files.has('sci.json'), 'manifest.missing_metadata', '§3', 'files must list sci.json', { path: '/files' });
  check(files.has('README.md'), 'manifest.missing_readme', '§3', 'files must list README.md', { path: '/files' });
  const entries = new Map<string, ManifestEntry>();
  for (const [path, entry] of files) {
    const p = `/files/${path}`;
    if (!isObject(entry)) fail('manifest.entry_type', '§3', 'File entry must be an object', { path: p });
    checkFields(entry, ['sha256', 'bytes'], [], '§3', p);
    const hash = entry.get('sha256');
    check(
      typeof hash === 'string' && /^[0-9a-f]{64}$/.test(hash),
      'manifest.entry_sha256',
      '§3',
      'sha256 must be 64 lowercase hex characters',
      {
        path: `${p}/sha256`,
        observed: hash,
      },
    );
    const n = entry.get('bytes');
    check(
      n instanceof JsonNumber && n.isCanonicalNonnegativeInteger() && n.raw.length <= 16 && BigInt(n.raw) <= MAX_SAFE,
      'manifest.entry_bytes',
      '§3',
      'bytes must be a decimal integer 0..9007199254740991 without sign, fraction, exponent, or leading zeros',
      { path: `${p}/bytes`, observed: n instanceof JsonNumber ? n.raw : n },
    );
    entries.set(path, { sha256: hash, bytes: n.value });
  }
  return {
    id: `sha256:${digest}`,
    digest,
    schema: SCHEMAS.publication,
    publicKey: jwk,
    publicKeyBytes: keyBytes,
    files: entries,
    bytes,
  };
}

/** Complete §6.1 manifest checks, including the signature in `manifest.json.sig`. */
export function verifyManifest(manifestBytes: Uint8Array, signature: Uint8Array, options: ManifestOptions = {}): Manifest {
  const manifest = parseManifest(manifestBytes, options);
  verifyEd25519(manifest.publicKeyBytes, manifestBytes, signature, 'manifest.json.sig');
  return manifest;
}

/** §6.2 file checks for bytes already retrieved. */
export function verifyFileBytes(manifest: Manifest, path: string, data: Uint8Array): void {
  const entry = manifest.files.get(path);
  if (!entry) fail('file.not_listed', '§6.2', 'Requested path is not listed in the manifest', { path });
  check(data.length === entry.bytes, 'file.size_mismatch', '§6.2', 'Retrieved byte count differs from the manifest', {
    path,
    observed: data.length,
  });
  check(toHex(sha256(data)) === entry.sha256, 'file.hash_mismatch', '§6.2', 'Retrieved SHA-256 differs from the manifest', {
    path,
  });
}

/** §2.1 ZIP size formula: `22 + Σ(file_bytes + 76 + 2·path_bytes)` over every ZIP entry. */
export function zipSizeOf(manifestBytesLength: number, signatureLength: number, files: ReadonlyMap<string, ManifestEntry>): number {
  let total = 22 + manifestBytesLength + 76 + 2 * 'manifest.json'.length + signatureLength + 76 + 2 * 'manifest.json.sig'.length;
  for (const [path, entry] of files) total += entry.bytes + 76 + 2 * path.length;
  return total;
}
