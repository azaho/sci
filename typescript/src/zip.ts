/**
 * SCI §2.2 single-file bundle: strict ZIP reader, range locator, and canonical writer.
 *
 * The reader never extracts or decompresses. It checks every structural rule with explicit
 * bounds and accounts for every byte: local headers + data from offset 0, then the central
 * directory in the same order, then a 22-byte end-of-central-directory record at the end.
 */
import { asciiBytes, u16le, u32le } from './bytes.ts';
import { check, fail, incomplete, SciError } from './errors.ts';
import { Crc32, type IncrementalHash } from './hash.ts';
import type { ByteSource } from './source.ts';

export const LOCAL_HEADER_SIG = 0x04034b50;
export const CENTRAL_HEADER_SIG = 0x02014b50;
export const EOCD_SIG = 0x06054b50;
const DOS_DATE = 0x0021;
const REQ = '§2.2';

export interface ZipEntry {
  /** Index in archive order. */
  readonly index: number;
  /** ASCII filename. */
  readonly name: string;
  readonly crc32: number;
  readonly size: number;
  /** Offset of the local file header. */
  readonly localOffset: number;
  /** Offset of the first data byte (local header + filename; extra fields are prohibited). */
  readonly dataOffset: number;
}

export interface ZipDirectory {
  readonly size: number;
  readonly centralOffset: number;
  readonly centralSize: number;
  readonly entries: readonly ZipEntry[];
  /** Raw central directory bytes (for whole-archive hashing). */
  readonly centralBytes: Uint8Array;
  /** Raw EOCD bytes. */
  readonly eocdBytes: Uint8Array;
}

/** Wrap a source read so I/O failures are `incomplete`, not `invalid`. */
export async function readSource(source: ByteSource, offset: number, length: number): Promise<Uint8Array> {
  try {
    const data = await source.read(offset, length);
    if (data.length !== length) incomplete('transport.short_read', '§6', 'Byte source returned a short read', { observed: data.length });
    return data;
  } catch (error) {
    if (error instanceof SciError) throw error;
    return incomplete('transport.read_failed', '§6', `Could not read input: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function decodeAsciiName(bytes: Uint8Array, path: string): string {
  let s = '';
  for (const b of bytes) {
    check(b >= 0x20 && b < 0x7f, 'zip.name_not_ascii', REQ, 'ZIP filename must be printable ASCII', { path });
    s += String.fromCharCode(b);
  }
  return s;
}

/**
 * Read and check the EOCD record and central directory (§2.2). Data and local headers are not
 * read; use {@link readZipEntry} or {@link streamZipEntry}. This is all a range-reading client
 * needs to locate any entry's bytes.
 */
export async function readZipDirectory(source: ByteSource, maxEntries = 65_535): Promise<ZipDirectory> {
  const size = source.size;
  check(Number.isSafeInteger(size) && size >= 22, 'zip.too_short', REQ, 'Input is too short to be a ZIP', { observed: size });
  // Classic ZIP offsets and sizes are 32-bit; anything larger cannot be a valid SCI ZIP.
  check(size <= 0xffffffff + 22, 'zip.too_large', REQ, 'Input exceeds classic ZIP limits', { observed: size });
  const eocd = await readSource(source, size - 22, 22);
  check(
    u32le(eocd, 0) === EOCD_SIG,
    'zip.eocd_signature',
    REQ,
    'The last 22 bytes must be the end-of-central-directory record (no comment or trailing bytes)',
  );
  const disk = u16le(eocd, 4);
  const cdDisk = u16le(eocd, 6);
  const nDisk = u16le(eocd, 8);
  const nTotal = u16le(eocd, 10);
  const cdSize = u32le(eocd, 12);
  const cdOffset = u32le(eocd, 16);
  const commentLen = u16le(eocd, 20);
  check(disk === 0 && cdDisk === 0, 'zip.disk_number', REQ, 'Disk-number fields must be zero');
  check(commentLen === 0, 'zip.comment', REQ, 'ZIP comment must be empty');
  check(nDisk === nTotal, 'zip.entry_count', REQ, 'EOCD entry counts must agree', { observed: `${nDisk}/${nTotal}` });
  check(nTotal !== 0xffff && cdSize !== 0xffffffff && cdOffset !== 0xffffffff, 'zip.zip64', REQ, 'ZIP64 sentinel values are prohibited');
  check(cdOffset + cdSize === size - 22, 'zip.central_extent', REQ, 'Central directory must end exactly at the EOCD record', {
    observed: `${cdOffset}+${cdSize}`,
  });
  if (nTotal > maxEntries) incomplete('budget.entries', '§6', 'ZIP has more entries than the local budget', { observed: nTotal });
  // Each canonical central header is 46 bytes plus a filename of at most 255 bytes.
  check(
    cdSize >= nTotal * 46 && cdSize <= nTotal * (46 + 255),
    'zip.central_size',
    REQ,
    'Central directory size is inconsistent with its entry count',
  );
  const cd = await readSource(source, cdOffset, cdSize);
  const entries: ZipEntry[] = [];
  let pos = 0;
  let expectedLocal = 0;
  for (let i = 0; i < nTotal; i++) {
    const p = `central[${i}]`;
    check(pos + 46 <= cd.length, 'zip.central_bounds', REQ, 'Central header exceeds the central directory', { path: p });
    check(u32le(cd, pos) === CENTRAL_HEADER_SIG, 'zip.central_signature', REQ, 'Bad central file header signature', { path: p });
    check(u16le(cd, pos + 4) === 0x000a, 'zip.version_made_by', REQ, 'version made by must be 0x000A', {
      path: p,
      observed: u16le(cd, pos + 4),
    });
    check(u16le(cd, pos + 6) === 10, 'zip.version_needed', REQ, 'version needed to extract must be 10', { path: p });
    check(u16le(cd, pos + 8) === 0, 'zip.flags', REQ, 'General-purpose flags must be zero', { path: p });
    check(u16le(cd, pos + 10) === 0, 'zip.method', REQ, 'Compression method must be 0 (stored)', { path: p });
    check(u16le(cd, pos + 12) === 0, 'zip.time', REQ, 'Last-modification time must be 0x0000', { path: p });
    check(u16le(cd, pos + 14) === DOS_DATE, 'zip.date', REQ, 'Last-modification date must be 0x0021', { path: p });
    const crc = u32le(cd, pos + 16);
    const csize = u32le(cd, pos + 20);
    const usize = u32le(cd, pos + 24);
    const nameLen = u16le(cd, pos + 28);
    const extraLen = u16le(cd, pos + 30);
    const commentLength = u16le(cd, pos + 32);
    const diskStart = u16le(cd, pos + 34);
    const internal = u16le(cd, pos + 36);
    const external = u32le(cd, pos + 38);
    const localOffset = u32le(cd, pos + 42);
    check(csize === usize, 'zip.sizes', REQ, 'Compressed and uncompressed sizes must be equal', { path: p });
    check(csize !== 0xffffffff && localOffset !== 0xffffffff, 'zip.zip64', REQ, 'ZIP64 sentinel values are prohibited', { path: p });
    check(extraLen === 0 && commentLength === 0, 'zip.extra_or_comment', REQ, 'Extra fields and comments must be empty', { path: p });
    check(diskStart === 0, 'zip.disk_number', REQ, 'Disk-number fields must be zero', { path: p });
    check(internal === 0 && external === 0, 'zip.attributes', REQ, 'Internal and external attributes must be zero', { path: p });
    check(nameLen > 0 && pos + 46 + nameLen <= cd.length, 'zip.central_bounds', REQ, 'Central filename exceeds the central directory', {
      path: p,
    });
    const name = decodeAsciiName(cd.subarray(pos + 46, pos + 46 + nameLen), p);
    check(localOffset === expectedLocal, 'zip.local_offset', REQ, 'Central entry must point to the start of its consecutive local header', {
      path: name,
      observed: localOffset,
    });
    const dataOffset = localOffset + 30 + nameLen;
    entries.push({ index: i, name, crc32: crc, size: csize, localOffset, dataOffset });
    expectedLocal = dataOffset + csize;
    check(expectedLocal <= cdOffset, 'zip.local_extent', REQ, 'Local entries overlap the central directory', { path: name });
    pos += 46 + nameLen;
  }
  check(pos === cd.length, 'zip.central_unaccounted', REQ, 'Central directory contains unaccounted bytes');
  check(
    expectedLocal === cdOffset,
    'zip.local_unaccounted',
    REQ,
    'Bytes between local entries and the central directory are unaccounted for',
  );
  return { size, centralOffset: cdOffset, centralSize: cdSize, entries, centralBytes: cd, eocdBytes: eocd };
}

/** Byte range of an entry's data, for HTTP range requests. */
export interface ZipEntryRange {
  readonly name: string;
  /** Start of the local header (inclusive). */
  readonly headerOffset: number;
  /** Start of the file data (inclusive). */
  readonly dataOffset: number;
  /** One past the last data byte. */
  readonly end: number;
  readonly size: number;
  readonly crc32: number;
}

/** Locate an entry's local header and data range from a checked central directory. */
export function locateZipEntry(dir: ZipDirectory, name: string): ZipEntryRange | undefined {
  const e = dir.entries.find((x) => x.name === name);
  if (!e) return undefined;
  return { name, headerOffset: e.localOffset, dataOffset: e.dataOffset, end: e.dataOffset + e.size, size: e.size, crc32: e.crc32 };
}

/** Check a local header against its central entry. */
export function checkLocalHeader(header: Uint8Array, entry: ZipEntry): void {
  const p = entry.name;
  const nameLen = entry.dataOffset - entry.localOffset - 30;
  check(header.length === 30 + nameLen, 'zip.local_bounds', REQ, 'Local header is truncated', { path: p });
  check(u32le(header, 0) === LOCAL_HEADER_SIG, 'zip.local_signature', REQ, 'Bad local file header signature', { path: p });
  check(u16le(header, 4) === 10, 'zip.local_mismatch', REQ, 'Local version needed must be 10', { path: p });
  check(u16le(header, 6) === 0, 'zip.local_mismatch', REQ, 'Local flags must be zero', { path: p });
  check(u16le(header, 8) === 0, 'zip.local_mismatch', REQ, 'Local method must be 0', { path: p });
  check(u16le(header, 10) === 0 && u16le(header, 12) === DOS_DATE, 'zip.local_mismatch', REQ, 'Local time/date must be canonical', {
    path: p,
  });
  check(u32le(header, 14) === entry.crc32, 'zip.local_mismatch', REQ, 'Local CRC-32 differs from central', { path: p });
  check(
    u32le(header, 18) === entry.size && u32le(header, 22) === entry.size,
    'zip.local_mismatch',
    REQ,
    'Local sizes differ from central',
    { path: p },
  );
  check(u16le(header, 26) === nameLen, 'zip.local_mismatch', REQ, 'Local filename length differs from central', { path: p });
  check(u16le(header, 28) === 0, 'zip.extra_or_comment', REQ, 'Local extra field must be empty', { path: p });
  const nameBytes = asciiBytes(entry.name);
  for (let i = 0; i < nameLen; i++) {
    check(header[30 + i] === nameBytes[i], 'zip.local_mismatch', REQ, 'Local filename differs from central', { path: p });
  }
}

export interface StreamEntryOptions {
  readonly chunkBytes?: number;
  /** Additional hashers fed with the entry's data. */
  readonly hashers?: readonly Pick<IncrementalHash, 'update'>[];
  /** Hashers fed with the local header and data, in archive order (whole-ZIP hashing). */
  readonly archiveHashers?: readonly IncrementalHash[];
  /** Receive data chunks (e.g. to retain verified bytes). */
  readonly onChunk?: (chunk: Uint8Array) => void;
}

/** Read an entry's local header and stream its data, checking header agreement and CRC-32. */
export async function streamZipEntry(source: ByteSource, entry: ZipEntry, options: StreamEntryOptions = {}): Promise<void> {
  const header = await readSource(source, entry.localOffset, entry.dataOffset - entry.localOffset);
  checkLocalHeader(header, entry);
  for (const h of options.archiveHashers ?? []) h.update(header);
  const crc = new Crc32();
  const chunk = options.chunkBytes ?? 1024 * 1024;
  for (let off = 0; off < entry.size; off += chunk) {
    const n = Math.min(chunk, entry.size - off);
    const data = await readSource(source, entry.dataOffset + off, n);
    crc.update(data);
    for (const h of options.hashers ?? []) h.update(data);
    for (const h of options.archiveHashers ?? []) h.update(data);
    options.onChunk?.(data);
  }
  check(crc.value() === entry.crc32, 'zip.crc_mismatch', REQ, 'CRC-32 does not match the entry data', { path: entry.name });
}

/** Read an entry fully into memory (caller must bound `entry.size`). */
export async function readZipEntry(source: ByteSource, entry: ZipEntry, chunkBytes?: number): Promise<Uint8Array> {
  const out = new Uint8Array(entry.size);
  let o = 0;
  await streamZipEntry(source, entry, {
    ...(chunkBytes !== undefined ? { chunkBytes } : {}),
    onChunk(c) {
      out.set(c, o);
      o += c.length;
    },
  });
  return out;
}

/** Verify that the entry list is exactly manifest.json, manifest.json.sig, then the sorted listed paths. */
export function checkZipInventory(entries: readonly ZipEntry[], listed: Iterable<string>): void {
  const names = entries.map((e) => e.name);
  check(
    names[0] === 'manifest.json' && names[1] === 'manifest.json.sig',
    'zip.first_entries',
    REQ,
    'First entries must be manifest.json then manifest.json.sig',
  );
  const expected = [...listed].sort(compareAscii);
  const actual = names.slice(2);
  const seen = new Set<string>();
  for (const n of names) {
    check(!seen.has(n), 'zip.duplicate_entry', REQ, 'Duplicate ZIP entry', { path: n });
    seen.add(n);
  }
  const listedSet = new Set(expected);
  for (const n of actual)
    check(listedSet.has(n), 'bundle.unlisted_file', '§3, §6.4', 'ZIP contains an entry not listed in the manifest', { path: n });
  for (const n of expected) check(seen.has(n), 'bundle.missing_file', '§3, §6.4', 'ZIP is missing a listed file', { path: n });
  for (let i = 0; i < expected.length; i++) {
    check(actual[i] === expected[i], 'zip.entry_order', REQ, 'Listed files must appear in ascending ASCII order', { path: actual[i] });
  }
}

/** Compare ASCII strings by byte value. */
export function compareAscii(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

// ---------------------------------------------------------------------------------------------
// Canonical writer

export interface ZipInputEntry {
  readonly name: string;
  readonly data: Uint8Array;
}

function crcOf(data: Uint8Array): number {
  const c = new Crc32();
  c.update(data);
  return c.value();
}

function header(sig: number, fields: readonly [number, 2 | 4][]): Uint8Array {
  const len = 4 + fields.reduce((n, [, w]) => n + w, 0);
  const out = new Uint8Array(len);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, sig, true);
  let o = 4;
  for (const [v, w] of fields) {
    if (w === 2) dv.setUint16(o, v, true);
    else dv.setUint32(o, v >>> 0, true);
    o += w;
  }
  return out;
}

/**
 * Produce the canonical ZIP (§2.2) as a sequence of byte chunks, in order. Entries must already be
 * in canonical order; use {@link canonicalZipEntries}. Suitable for streaming responses.
 */
export function* canonicalZipChunks(entries: readonly ZipInputEntry[]): Generator<Uint8Array> {
  check(entries.length < 0xffff, 'zip.entry_count', REQ, 'Too many entries for classic ZIP');
  const centrals: Uint8Array[] = [];
  let offset = 0;
  for (const e of entries) {
    const name = asciiBytes(e.name);
    const crc = crcOf(e.data);
    const n = e.data.length;
    check(n < 0xffffffff && offset < 0xffffffff, 'zip.too_large', REQ, 'Entry exceeds classic ZIP limits', { path: e.name });
    const local = header(LOCAL_HEADER_SIG, [
      [10, 2],
      [0, 2],
      [0, 2],
      [0, 2],
      [DOS_DATE, 2],
      [crc, 4],
      [n, 4],
      [n, 4],
      [name.length, 2],
      [0, 2],
    ]);
    const central = header(CENTRAL_HEADER_SIG, [
      [0x000a, 2],
      [10, 2],
      [0, 2],
      [0, 2],
      [0, 2],
      [DOS_DATE, 2],
      [crc, 4],
      [n, 4],
      [n, 4],
      [name.length, 2],
      [0, 2],
      [0, 2],
      [0, 2],
      [0, 2],
      [0, 4],
      [offset, 4],
    ]);
    centrals.push(central, name);
    yield local;
    yield name;
    yield e.data;
    offset += local.length + name.length + n;
  }
  let cdSize = 0;
  for (const c of centrals) {
    cdSize += c.length;
    yield c;
  }
  check(offset < 0xffffffff, 'zip.too_large', REQ, 'Archive exceeds classic ZIP limits');
  yield header(EOCD_SIG, [
    [0, 2],
    [0, 2],
    [entries.length, 2],
    [entries.length, 2],
    [cdSize, 4],
    [offset, 4],
    [0, 2],
  ]);
}

/** Order entries canonically: manifest.json, manifest.json.sig, then listed paths by ASCII order. */
export function canonicalZipEntries(
  manifestBytes: Uint8Array,
  signature: Uint8Array,
  files: ReadonlyMap<string, Uint8Array>,
): ZipInputEntry[] {
  const out: ZipInputEntry[] = [
    { name: 'manifest.json', data: manifestBytes },
    { name: 'manifest.json.sig', data: signature },
  ];
  for (const name of [...files.keys()].sort(compareAscii)) {
    const data = files.get(name);
    if (!data) fail('zip.missing_input', REQ, 'Missing data for entry', { path: name });
    out.push({ name, data });
  }
  return out;
}

/** Build the exact canonical ZIP bytes (§2.2) in memory. */
export function buildCanonicalZip(manifestBytes: Uint8Array, signature: Uint8Array, files: ReadonlyMap<string, Uint8Array>): Uint8Array {
  const chunks = [...canonicalZipChunks(canonicalZipEntries(manifestBytes, signature, files))];
  let total = 0;
  for (const c of chunks) total += c.length;
  const out = new Uint8Array(total);
  let o = 0;
  for (const c of chunks) {
    out.set(c, o);
    o += c.length;
  }
  return out;
}

/**
 * Read one or more consecutive entries with a single source read (one HTTP range request),
 * checking each local header and CRC-32. Returns the data of each entry in order.
 */
export async function readZipEntriesSpan(source: ByteSource, entries: readonly ZipEntry[]): Promise<Uint8Array[]> {
  const first = entries[0];
  const last = entries[entries.length - 1];
  if (!first || !last) return [];
  for (let i = 1; i < entries.length; i++) {
    const prev = entries[i - 1] as ZipEntry;
    check((entries[i] as ZipEntry).localOffset === prev.dataOffset + prev.size, 'zip.span', REQ, 'Entries are not consecutive');
  }
  const span = await readSource(source, first.localOffset, last.dataOffset + last.size - first.localOffset);
  return entries.map((e) => {
    const rel = e.localOffset - first.localOffset;
    checkLocalHeader(span.subarray(rel, rel + (e.dataOffset - e.localOffset)), e);
    const data = span.subarray(rel + (e.dataOffset - e.localOffset), rel + (e.dataOffset - e.localOffset) + e.size);
    const crc = new Crc32();
    crc.update(data);
    check(crc.value() === e.crc32, 'zip.crc_mismatch', REQ, 'CRC-32 does not match the entry data', { path: e.name });
    return data;
  });
}
