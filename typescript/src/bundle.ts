/**
 * SCI §6.3 metadata checks, §6.4 complete bundle checks, and §2.1 baseline classification,
 * for ZIP bundles (streamed from a {@link ByteSource}) and loose files.
 */
import { type Budget, resolveBudget } from './budget.ts';
import { toHex } from './bytes.ts';
import { type CheckResult, captureAsync, check, fail, incomplete } from './errors.ts';
import { Crc32, createSha256, type IncrementalHash, sha256 } from './hash.ts';
import { type Manifest, verifyManifest, zipSizeOf } from './manifest.ts';
import { type Metadata, parseMetadata } from './metadata.ts';
import { type ByteSource, bytesSource } from './source.ts';
import { checkTextFile } from './text.ts';
import {
  canonicalZipChunks,
  checkZipInventory,
  compareAscii,
  readSource,
  readZipDirectory,
  streamZipEntry,
  type ZipInputEntry,
} from './zip.ts';

/** SHA-256 of the approved SCI baseline license 0.1 (§2.1). */
export const BASELINE_LICENSE_SHA256 = '8ccfd5fdaa93e5e71dc8812bf11e0a7198bd0a534fa64b8970fd2de58e6c2478';
export const APPROVED_LICENSE_SHA256: readonly string[] = [BASELINE_LICENSE_SHA256];

/** §2.1 limits. */
export const BASELINE_LIMITS = Object.freeze({
  zipBytes: 67_108_864,
  files: 1000,
  displayItems: 3,
  manifestBytes: 262_144,
  metadataBytes: 65_536,
  readmeBytes: 1_048_576,
});

export interface BaselineReport {
  readonly baseline: boolean;
  /** Names of the §2.1 requirements that are not met. */
  readonly unmet: readonly ('zip_bytes' | 'files' | 'display_items' | 'manifest_bytes' | 'metadata_bytes' | 'readme_bytes' | 'license')[];
}

export function evaluateBaseline(input: {
  readonly zipBytes: number;
  readonly fileCount: number;
  readonly displayItems: number;
  readonly manifestBytes: number;
  readonly metadataBytes: number;
  readonly readmeBytes: number;
  readonly licenseSha256: string | undefined;
}): BaselineReport {
  const unmet: BaselineReport['unmet'][number][] = [];
  if (input.zipBytes > BASELINE_LIMITS.zipBytes) unmet.push('zip_bytes');
  if (input.fileCount > BASELINE_LIMITS.files) unmet.push('files');
  if (input.displayItems > BASELINE_LIMITS.displayItems) unmet.push('display_items');
  if (input.manifestBytes > BASELINE_LIMITS.manifestBytes) unmet.push('manifest_bytes');
  if (input.metadataBytes > BASELINE_LIMITS.metadataBytes) unmet.push('metadata_bytes');
  if (input.readmeBytes > BASELINE_LIMITS.readmeBytes) unmet.push('readme_bytes');
  if (input.licenseSha256 === undefined || !APPROVED_LICENSE_SHA256.includes(input.licenseSha256)) unmet.push('license');
  return { baseline: unmet.length === 0, unmet };
}

export interface VerifiedBundle {
  readonly manifest: Manifest;
  readonly signature: Uint8Array;
  readonly metadata: Metadata;
  readonly metadataBytes: Uint8Array;
  readonly readme: string;
  readonly license?: string;
  readonly baseline: BaselineReport;
  /** Size of the canonical ZIP (§2.1 formula). */
  readonly zipBytes: number;
  /** SHA-256 of the canonical ZIP: the verified ZIP itself, or the one built from loose files. */
  readonly zipSha256?: string;
  /** Verified bytes retained at the caller's request (always includes sci.json, README.md, LICENSE). */
  readonly files: ReadonlyMap<string, Uint8Array>;
}

export interface BundleOptions {
  /** Expected publication ID (any protocol spelling). */
  readonly expectedId?: string | undefined;
  readonly budget?: Partial<Budget> | undefined;
  /** Retain verified bytes of these listed paths (default: none beyond metadata/text files). */
  readonly retain?: ((path: string) => boolean) | undefined;
  /** For loose input: also compute the canonical ZIP SHA-256 (default true). */
  readonly computeZipSha256?: boolean | undefined;
}

const TEXT_FILES = new Set(['sci.json', 'README.md', 'LICENSE']);

function finishMetadata(
  manifest: Manifest,
  signature: Uint8Array,
  retained: Map<string, Uint8Array>,
  budget: Budget,
  zipSha256: string | undefined,
): VerifiedBundle {
  const metadataBytes = retained.get('sci.json');
  const readmeBytes = retained.get('README.md');
  if (!metadataBytes || !readmeBytes) fail('bundle.missing_file', '§6.3', 'sci.json and README.md are required');
  const metadata = parseMetadata(metadataBytes, manifest.files, budget);
  if (readmeBytes.length > budget.maxTextBytes) incomplete('budget.text_bytes', '§6', 'README.md exceeds the local text budget');
  const readme = checkTextFile(readmeBytes, 'README.md');
  const licenseBytes = retained.get('LICENSE');
  let license: string | undefined;
  if (licenseBytes) {
    if (licenseBytes.length > budget.maxTextBytes) incomplete('budget.text_bytes', '§6', 'LICENSE exceeds the local text budget');
    license = checkTextFile(licenseBytes, 'LICENSE');
  }
  const zipBytes = zipSizeOf(manifest.bytes.length, signature.length, manifest.files);
  const baseline = evaluateBaseline({
    zipBytes,
    fileCount: manifest.files.size,
    displayItems: metadata.display?.length ?? 0,
    manifestBytes: manifest.bytes.length,
    metadataBytes: metadataBytes.length,
    readmeBytes: readmeBytes.length,
    licenseSha256: licenseBytes ? toHex(sha256(licenseBytes)) : undefined,
  });
  return {
    manifest,
    signature,
    metadata,
    metadataBytes,
    readme,
    ...(license !== undefined ? { license } : {}),
    baseline,
    zipBytes,
    ...(zipSha256 !== undefined ? { zipSha256 } : {}),
    files: retained,
  };
}

/**
 * Verify a complete ZIP bundle (§6.4 with §2.2), streaming entry data with incremental SHA-256
 * so memory stays bounded by the chunk size plus the retained metadata/text files.
 */
export async function verifyZipBundle(input: ByteSource | Uint8Array, options: BundleOptions = {}): Promise<VerifiedBundle> {
  const source = input instanceof Uint8Array ? bytesSource(input) : input;
  const budget = resolveBudget(options.budget);
  if (source.size > budget.maxTotalBytes) {
    incomplete('budget.total_bytes', '§6', 'Bundle exceeds the local verification budget', { observed: source.size });
  }
  const dir = await readZipDirectory(source, budget.maxEntries);
  const [m, s] = dir.entries;
  check(
    m?.name === 'manifest.json' && s?.name === 'manifest.json.sig',
    'zip.first_entries',
    '§2.2',
    'First entries must be manifest.json then manifest.json.sig',
  );
  if (m.size > budget.maxManifestBytes) incomplete('budget.manifest_bytes', '§6', 'manifest.json exceeds the local size budget');
  check(s.size === 64, 'signature.length', '§3', 'manifest.json.sig must be exactly 64 bytes', { observed: s.size });
  const zipHash = createSha256();
  const collect = async (entry: typeof m, hashers: IncrementalHash[] = []): Promise<Uint8Array> => {
    const out = new Uint8Array(entry.size);
    let o = 0;
    await streamZipEntry(source, entry, {
      chunkBytes: budget.chunkBytes,
      hashers,
      archiveHashers: [zipHash],
      onChunk(c) {
        out.set(c, o);
        o += c.length;
      },
    });
    return out;
  };
  const manifestBytes = await collect(m);
  const signature = await collect(s);
  const manifest = verifyManifest(manifestBytes, signature, { expectedId: options.expectedId, budget });
  checkZipInventory(dir.entries, manifest.files.keys());
  const retained = new Map<string, Uint8Array>();
  for (const entry of dir.entries.slice(2)) {
    const listed = manifest.files.get(entry.name);
    if (!listed) fail('bundle.unlisted_file', '§6.4', 'Unlisted entry', { path: entry.name });
    check(entry.size === listed.bytes, 'file.size_mismatch', '§6.2', 'Entry size differs from the manifest', {
      path: entry.name,
      observed: entry.size,
    });
    const keep = TEXT_FILES.has(entry.name) || (options.retain?.(entry.name) ?? false);
    if (keep && TEXT_FILES.has(entry.name)) {
      const limit = entry.name === 'sci.json' ? budget.maxMetadataBytes : budget.maxTextBytes;
      if (entry.size > limit) incomplete('budget.text_bytes', '§6', `${entry.name} exceeds the local budget`, { path: entry.name });
    }
    const h = createSha256();
    if (keep) {
      retained.set(entry.name, await collect(entry, [h]));
    } else {
      await streamZipEntry(source, entry, { chunkBytes: budget.chunkBytes, hashers: [h], archiveHashers: [zipHash] });
    }
    check(toHex(h.digest()) === listed.sha256, 'file.hash_mismatch', '§6.2', 'Entry SHA-256 differs from the manifest', {
      path: entry.name,
    });
  }
  zipHash.update(dir.centralBytes);
  zipHash.update(dir.eocdBytes);
  const result = finishMetadata(manifest, signature, retained, budget, toHex(zipHash.digest()));
  check(result.zipBytes === source.size, 'zip.size_formula', '§2.1', 'ZIP size differs from the §2.1 formula');
  return result;
}

/** Loose file input: exact bundle paths mapped to bytes or random-access sources. */
export type LooseFiles = ReadonlyMap<string, Uint8Array | ByteSource>;

async function streamSource(src: Uint8Array | ByteSource, chunk: number, sink: (c: Uint8Array) => void): Promise<void> {
  if (src instanceof Uint8Array) {
    sink(src);
    return;
  }
  for (let off = 0; off < src.size; off += chunk) sink(await readSource(src, off, Math.min(chunk, src.size - off)));
}

async function loadSmall(src: Uint8Array | ByteSource): Promise<Uint8Array> {
  return src instanceof Uint8Array ? src : readSource(src, 0, src.size);
}

/**
 * Verify a complete loose-file bundle (§6.4). The optional `manifest.json.sig.tsr` sidecar is
 * excluded from the inventory. Optionally computes the canonical ZIP SHA-256 (streamed).
 */
export async function verifyLooseBundle(files: LooseFiles, options: BundleOptions = {}): Promise<VerifiedBundle> {
  const budget = resolveBudget(options.budget);
  let total = 0;
  for (const v of files.values()) total += v instanceof Uint8Array ? v.length : v.size;
  if (total > budget.maxTotalBytes)
    incomplete('budget.total_bytes', '§6', 'Bundle exceeds the local verification budget', { observed: total });
  const mSrc = files.get('manifest.json');
  const sSrc = files.get('manifest.json.sig');
  if (!mSrc) fail('bundle.missing_file', '§2', 'manifest.json is missing', { path: 'manifest.json' });
  if (!sSrc) fail('bundle.missing_file', '§2', 'manifest.json.sig is missing', { path: 'manifest.json.sig' });
  const mSize = mSrc instanceof Uint8Array ? mSrc.length : mSrc.size;
  if (mSize > budget.maxManifestBytes) incomplete('budget.manifest_bytes', '§6', 'manifest.json exceeds the local size budget');
  const sSize = sSrc instanceof Uint8Array ? sSrc.length : sSrc.size;
  check(sSize === 64, 'signature.length', '§3', 'manifest.json.sig must be exactly 64 bytes', { observed: sSize });
  const manifestBytes = await loadSmall(mSrc);
  const signature = await loadSmall(sSrc);
  const manifest = verifyManifest(manifestBytes, signature, { expectedId: options.expectedId, budget });
  for (const path of files.keys()) {
    if (path === 'manifest.json' || path === 'manifest.json.sig' || path === 'manifest.json.sig.tsr') continue;
    check(manifest.files.has(path), 'bundle.unlisted_file', '§3, §6.4', 'Bundle contains a file not listed in the manifest', { path });
  }
  const retained = new Map<string, Uint8Array>();
  for (const [path, entry] of manifest.files) {
    const src = files.get(path);
    if (!src) fail('bundle.missing_file', '§3, §6.4', 'Listed file is missing', { path });
    const size = src instanceof Uint8Array ? src.length : src.size;
    check(size === entry.bytes, 'file.size_mismatch', '§6.2', 'File size differs from the manifest', { path, observed: size });
    const keep = TEXT_FILES.has(path) || (options.retain?.(path) ?? false);
    if (TEXT_FILES.has(path)) {
      const limit = path === 'sci.json' ? budget.maxMetadataBytes : budget.maxTextBytes;
      if (size > limit) incomplete('budget.text_bytes', '§6', `${path} exceeds the local budget`, { path });
    }
    const h = createSha256();
    if (keep) {
      const data = await loadSmall(src);
      h.update(data);
      retained.set(path, data);
    } else {
      await streamSource(src, budget.chunkBytes, (c) => h.update(c));
    }
    check(toHex(h.digest()) === entry.sha256, 'file.hash_mismatch', '§6.2', 'File SHA-256 differs from the manifest', { path });
  }
  let zipSha256: string | undefined;
  if (options.computeZipSha256 ?? true) {
    zipSha256 = await canonicalZipSha256(manifestBytes, signature, manifest, files, budget.chunkBytes);
  }
  return finishMetadata(manifest, signature, retained, budget, zipSha256);
}

/** Stream-hash the canonical ZIP built from loose files without materializing it. */
async function canonicalZipSha256(
  manifestBytes: Uint8Array,
  signature: Uint8Array,
  manifest: Manifest,
  files: LooseFiles,
  chunk: number,
): Promise<string> {
  // CRCs are needed before each local header, so hash files whose bytes are not in memory twice.
  const h = createSha256();
  const entries: ZipInputEntry[] = [];
  const names = [...manifest.files.keys()].sort(compareAscii);
  const all = [
    { name: 'manifest.json', src: manifestBytes as Uint8Array | ByteSource },
    { name: 'manifest.json.sig', src: signature as Uint8Array | ByteSource },
    ...names.map((name) => ({ name, src: files.get(name) as Uint8Array | ByteSource })),
  ];
  const inMemory = all.every((e) => e.src instanceof Uint8Array);
  if (inMemory) {
    for (const e of all) entries.push({ name: e.name, data: e.src as Uint8Array });
    for (const c of canonicalZipChunks(entries)) h.update(c);
    return toHex(h.digest());
  }
  // Streaming path: emulate the writer with precomputed CRCs.
  const crcs: number[] = [];
  for (const e of all) {
    const c = new Crc32();
    await streamSource(e.src, chunk, (d) => c.update(d));
    crcs.push(c.value());
  }
  const central: Uint8Array[] = [];
  let offset = 0;
  for (let i = 0; i < all.length; i++) {
    const e = all[i];
    if (!e) continue;
    const size = e.src instanceof Uint8Array ? e.src.length : e.src.size;
    const [local, name] = localHeaderFor(e.name, crcs[i] ?? 0, size);
    h.update(local);
    h.update(name);
    await streamSource(e.src, chunk, (d) => h.update(d));
    central.push(centralHeaderFor(e.name, crcs[i] ?? 0, size, offset));
    offset += local.length + name.length + size;
  }
  let cdSize = 0;
  for (const c of central) {
    h.update(c);
    cdSize += c.length;
  }
  h.update(eocdFor(all.length, cdSize, offset));
  return toHex(h.digest());
}

function le(fields: readonly [number, 2 | 4][]): Uint8Array {
  const len = fields.reduce((n, [, w]) => n + w, 0);
  const out = new Uint8Array(len);
  const dv = new DataView(out.buffer);
  let o = 0;
  for (const [v, w] of fields) {
    if (w === 2) dv.setUint16(o, v, true);
    else dv.setUint32(o, v >>> 0, true);
    o += w;
  }
  return out;
}

function localHeaderFor(name: string, crc: number, size: number): [Uint8Array, Uint8Array] {
  const nb = new TextEncoder().encode(name);
  return [
    le([
      [0x04034b50, 4],
      [10, 2],
      [0, 2],
      [0, 2],
      [0, 2],
      [0x21, 2],
      [crc, 4],
      [size, 4],
      [size, 4],
      [nb.length, 2],
      [0, 2],
    ]),
    nb,
  ];
}

function centralHeaderFor(name: string, crc: number, size: number, offset: number): Uint8Array {
  const nb = new TextEncoder().encode(name);
  const h = le([
    [0x02014b50, 4],
    [0x000a, 2],
    [10, 2],
    [0, 2],
    [0, 2],
    [0, 2],
    [0x21, 2],
    [crc, 4],
    [size, 4],
    [size, 4],
    [nb.length, 2],
    [0, 2],
    [0, 2],
    [0, 2],
    [0, 2],
    [0, 4],
    [offset, 4],
  ]);
  const out = new Uint8Array(h.length + nb.length);
  out.set(h);
  out.set(nb, h.length);
  return out;
}

function eocdFor(n: number, cdSize: number, cdOffset: number): Uint8Array {
  return le([
    [0x06054b50, 4],
    [0, 2],
    [0, 2],
    [n, 2],
    [n, 2],
    [cdSize, 4],
    [cdOffset, 4],
    [0, 2],
  ]);
}

export interface ManifestScopeReport {
  readonly manifest: Manifest;
  readonly zipBytes: number;
  readonly withinBaselineZipLimit: boolean;
}

/** §6.1 manifest checks plus the §2.1 ZIP size calculation, without payload files. */
export function verifyManifestScope(manifestBytes: Uint8Array, signature: Uint8Array, options: BundleOptions = {}): ManifestScopeReport {
  const manifest = verifyManifest(manifestBytes, signature, { expectedId: options.expectedId, budget: options.budget });
  const zipBytes = zipSizeOf(manifestBytes.length, signature.length, manifest.files);
  return { manifest, zipBytes, withinBaselineZipLimit: zipBytes <= BASELINE_LIMITS.zipBytes };
}

/** Outcome-returning wrappers. */
export function checkZipBundle(input: ByteSource | Uint8Array, options?: BundleOptions): Promise<CheckResult<VerifiedBundle>> {
  return captureAsync(() => verifyZipBundle(input, options));
}

export function checkLooseBundle(files: LooseFiles, options?: BundleOptions): Promise<CheckResult<VerifiedBundle>> {
  return captureAsync(() => verifyLooseBundle(files, options));
}
