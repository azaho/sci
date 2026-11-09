/**
 * Network retrieval for readers and backends (§8), built on an injectable `fetch`.
 *
 * Backends inject a `fetch` that enforces their outbound address policy (no private, loopback,
 * link-local, or metadata destinations; DNS-rebinding protection); tests inject mocks.
 * Every byte obtained here is verified before it is returned.
 */
import { type Budget, resolveBudget } from './budget.ts';
import { type VerifiedBundle, verifyLooseBundle, verifyZipBundle } from './bundle.ts';
import { isAbsoluteHttpUrl } from './citation.ts';
import { type Account, type Claim, claimLocations, type Revocation, verifyClaim, verifyRevocation } from './claims.ts';
import {
  type ArchiveDescriptor,
  type IndexDescriptor,
  interpretPublicationLookup,
  parseArchiveDescriptor,
  parseIndexDescriptor,
  routes,
} from './descriptors.ts';
import { type CheckResult, captureAsync, fail, incomplete, SciError, type SciIssue, toIssue } from './errors.ts';
import { sha256Hex } from './hash.ts';
import { parsePublicationRef } from './ids.ts';
import { interpretRevocationLookup, interpretTimestampLookup } from './lookups.ts';
import { type Manifest, verifyFileBytes, verifyManifest, zipSizeOf } from './manifest.ts';
import { type Metadata, parseMetadata } from './metadata.ts';
import { type ByteSource, verificationSource } from './source.ts';
import { checkTextFile } from './text.ts';
import { readZipDirectory, readZipEntriesSpan, type ZipDirectory } from './zip.ts';

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface RetrievalOptions {
  /** `fetch` implementation (default `globalThis.fetch`). Inject to enforce outbound policy or mock. */
  readonly fetch?: FetchLike;
  readonly budget?: Partial<Budget>;
  readonly signal?: AbortSignal;
  /** Redirect mode for claim retrieval (default `manual`: redirects are not followed). */
  readonly claimRedirect?: RequestRedirect;
}

interface Fetched {
  readonly status: number;
  readonly headers: Headers;
  readonly body: Uint8Array;
}

function getFetch(opts: RetrievalOptions): FetchLike {
  const f = opts.fetch ?? (globalThis.fetch?.bind(globalThis) as FetchLike | undefined);
  if (!f) throw new Error('No fetch implementation available');
  return f;
}

/** Read a decoded response body with a hard byte limit. */
async function readLimited(res: Response, maxBytes: number): Promise<Uint8Array> {
  // Fetch decodes HTTP content encodings, but Content-Length can describe the
  // encoded transfer. CORS may also hide Content-Encoding. Bound the actual
  // decoded bytes below instead of comparing transport headers to file sizes.
  if (!res.body) {
    const buf = new Uint8Array(await res.arrayBuffer());
    if (buf.length > maxBytes) incomplete('budget.response_bytes', '§6', 'Response exceeds the local size budget');
    return buf;
  }
  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > maxBytes) {
      await reader.cancel().catch(() => undefined);
      incomplete('budget.response_bytes', '§6', 'Response exceeds the local size budget');
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let o = 0;
  for (const c of chunks) {
    out.set(c, o);
    o += c.length;
  }
  return out;
}

async function fetchBytes(url: string, opts: RetrievalOptions, maxBytes: number, init: RequestInit = {}): Promise<Fetched> {
  if (!isAbsoluteHttpUrl(url)) fail('retrieval.url', '§8', 'URL must be an absolute HTTP(S) URL', { observed: url });
  let res: Response;
  try {
    res = await getFetch(opts)(url, { ...init, ...(opts.signal ? { signal: opts.signal } : {}) });
  } catch (error) {
    if (error instanceof SciError) throw error;
    return incomplete('transport.fetch_failed', '§6', `Request failed: ${error instanceof Error ? error.message : String(error)}`, {
      path: url,
    });
  }
  if (res.status !== 200 && res.status !== 206) {
    await res.body?.cancel().catch(() => undefined);
    return { status: res.status, headers: res.headers, body: new Uint8Array(0) };
  }
  try {
    return { status: res.status, headers: res.headers, body: await readLimited(res, maxBytes) };
  } catch (error) {
    if (error instanceof SciError) throw error;
    return incomplete('transport.read_failed', '§6', 'Failed to read response body', { path: url });
  }
}

function requireOk(f: Fetched, url: string): Uint8Array {
  if (f.status !== 200) incomplete('transport.status', '§6', `Unexpected HTTP status ${f.status}`, { path: url, observed: f.status });
  return f.body;
}

export async function fetchArchiveDescriptor(url: string, opts: RetrievalOptions = {}): Promise<ArchiveDescriptor> {
  const b = resolveBudget(opts.budget);
  return parseArchiveDescriptor(requireOk(await fetchBytes(url, opts, b.maxJsonBytes), url), b);
}

export async function fetchIndexDescriptor(url: string, opts: RetrievalOptions = {}): Promise<IndexDescriptor> {
  const b = resolveBudget(opts.budget);
  return parseIndexDescriptor(requireOk(await fetchBytes(url, opts, b.maxJsonBytes), url), b);
}

/** §8.1.1 publication lookup; `404` and `{"descriptors":[]}` both yield `[]`. */
export async function lookupPublicationSources(
  index: IndexDescriptor,
  id: string,
  opts: RetrievalOptions = {},
): Promise<readonly string[]> {
  const digest = parsePublicationRef(id);
  if (!digest) fail('retrieval.id', '§8.1.1', 'Malformed publication ID', { observed: id });
  const url = routes.publicationLookup(index, digest);
  const b = resolveBudget(opts.budget);
  const f = await fetchBytes(url, opts, b.maxJsonBytes);
  return interpretPublicationLookup(f.status, f.body, b).sources;
}

/**
 * §8.1.2: fetch and verify a key revocation from an index. Returns null on `404` (which does not
 * prove the key was never revoked).
 */
export async function fetchRevocation(
  index: IndexDescriptor,
  x: string,
  opts: RetrievalOptions = {},
): Promise<{ revocation: Revocation; record: Uint8Array; signature: Uint8Array } | null> {
  const b = resolveBudget(opts.budget);
  const recUrl = routes.revocationLookup(index, x, 'revocation.json');
  const rec = await fetchBytes(recUrl, opts, b.maxJsonBytes);
  const recResult = interpretRevocationLookup(rec.status, rec.body);
  if (recResult.status === 'unknown') return null;
  const sigUrl = routes.revocationLookup(index, x, 'revocation.json.sig');
  const sig = await fetchBytes(sigUrl, opts, 64);
  const sigResult = interpretRevocationLookup(sig.status, sig.body);
  if (sigResult.status === 'unknown')
    incomplete('revocation.signature_missing', '§8.1.2', 'Revocation signature not available', { path: sigUrl });
  const revocation = verifyRevocation(rec.body, sig.body, { expectedPublicKeyX: x, budget: b });
  return { revocation, record: rec.body, signature: sig.body };
}

/** §8.1.3: fetch retained timestamp proofs (unverified bytes) or null on `404`. Verify with `verifyTimestampFile`. */
export async function fetchPublicationTimestamps(
  index: IndexDescriptor,
  id: string,
  opts: RetrievalOptions = {},
): Promise<Uint8Array | null> {
  const digest = parsePublicationRef(id);
  if (!digest) fail('retrieval.id', '§8.1.3', 'Malformed publication ID', { observed: id });
  const b = resolveBudget(opts.budget);
  const f = await fetchBytes(routes.timestampLookup(index, digest), opts, b.maxTimestampBytes);
  const r = interpretTimestampLookup(f.status, f.body);
  return r.body ?? null;
}

export interface AssociationCheckOptions extends RetrievalOptions {
  readonly now?: Date;
  readonly knownRevocation?: boolean;
}

/**
 * §6.6: retrieve the claim from the account method's hosting location(s) and run every check.
 * Missing or unreachable claims are `incomplete` (unverified), never proof of anything.
 */
export async function checkKeyAssociation(account: Account, x: string, opts: AssociationCheckOptions = {}): Promise<Claim> {
  const b = resolveBudget(opts.budget);
  const init: RequestInit = { redirect: opts.claimRedirect ?? 'manual' };
  let lastIssue: SciIssue | undefined;
  for (const loc of claimLocations(account, x)) {
    const json = await fetchBytes(loc.json, opts, b.maxJsonBytes, init);
    if (json.status !== 200) {
      lastIssue = toIssue(
        new SciError({
          code: 'claim.not_found',
          outcome: 'incomplete',
          requirement: '§6.6',
          message: `Claim not available (HTTP ${json.status})`,
          path: loc.json,
        }),
      );
      continue;
    }
    const sig = await fetchBytes(loc.sig, opts, 64, init);
    if (sig.status !== 200) {
      lastIssue = toIssue(
        new SciError({
          code: 'claim.not_found',
          outcome: 'incomplete',
          requirement: '§6.6',
          message: `Claim signature not available (HTTP ${sig.status})`,
          path: loc.sig,
        }),
      );
      continue;
    }
    return verifyClaim(json.body, sig.body, {
      expectedPublicKeyX: x,
      requestedAccount: account,
      now: opts.now ?? new Date(),
      knownRevocation: opts.knownRevocation ?? false,
      retrievedFromAccountLocation: true,
      budget: b,
    });
  }
  throw new SciError(lastIssue ?? { code: 'claim.not_found', outcome: 'incomplete', requirement: '§6.6', message: 'Claim not available' });
}

// ---------------------------------------------------------------------------------------------
// HTTP range source

function parseContentRange(v: string | null): { start: number; end: number; total: number } | null {
  if (!v) return null;
  const m = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(v.trim());
  if (!m) return null;
  const [start, end, total] = [Number(m[1]), Number(m[2]), Number(m[3])];
  if (!Number.isSafeInteger(total) || start > end || end >= total) return null;
  return { start, end, total };
}

/**
 * A {@link ByteSource} over a `bundle_url` using single-range requests (§8.2). If the server
 * ignores ranges or hides `Content-Range`, falls back to one bounded full download.
 */
export async function httpRangeSource(url: string, opts: RetrievalOptions & { readonly expectedSize?: number } = {}): Promise<ByteSource> {
  const b = resolveBudget(opts.budget);
  const expectedSize = opts.expectedSize;
  if (expectedSize !== undefined) {
    if (!Number.isSafeInteger(expectedSize) || expectedSize < 0) throw new RangeError('Invalid expected ZIP size');
    if (expectedSize > b.maxTotalBytes) incomplete('budget.total_bytes', '§6', 'Bundle exceeds the local verification budget');
  }
  const sizeMismatch = (): never => fail('zip.size_formula', '§2.1', 'ZIP size differs from the signed manifest');
  const checkSize = (size: number): void => {
    if (expectedSize !== undefined && size !== expectedSize) sizeMismatch();
  };
  const initialBytes = async (maxBytes: number, init: RequestInit = {}): Promise<Fetched> => {
    try {
      return await fetchBytes(url, opts, expectedSize ?? maxBytes, init);
    } catch (error) {
      // The signed size is a format constraint, not an operator-tunable budget.
      // readLimited has already cancelled the oversized stream before throwing.
      if (expectedSize !== undefined && error instanceof SciError && error.issue.code === 'budget.response_bytes') sizeMismatch();
      throw error;
    }
  };
  // GitHub release storage accepts explicit ranges but rejects suffix ranges with HTTP 501.
  const probe = await initialBytes(Math.max(22, b.maxTotalBytes), { headers: { Range: 'bytes=0-0' } });
  const cr = probe.status === 206 ? parseContentRange(probe.headers.get('content-range')) : null;
  if (probe.status === 200 || (probe.status === 206 && !cr)) {
    let full = probe.body;
    if (probe.status === 206) full = requireOk(await initialBytes(b.maxTotalBytes), url);
    checkSize(full.length);
    return {
      size: full.length,
      read(offset, length) {
        if (offset < 0 || length < 0 || offset > full.length - length) return Promise.reject(new RangeError('read out of bounds'));
        return Promise.resolve(full.subarray(offset, offset + length));
      },
    };
  }
  if (probe.status !== 206 || !cr) incomplete('transport.status', '§6', `Unexpected HTTP status ${probe.status}`, { path: url });
  const total = cr.total;
  checkSize(total);
  if (total > b.maxTotalBytes) incomplete('budget.total_bytes', '§6', 'Bundle exceeds the local verification budget', { observed: total });
  const cached = probe.body;
  if (cached.length !== cr.end - cr.start + 1) incomplete('transport.range', '§8.2', 'Range response length mismatch', { path: url });
  return {
    size: total,
    async read(offset, length) {
      if (offset < 0 || length < 0 || offset > total - length) throw new RangeError('read out of bounds');
      if (length === 0) return new Uint8Array(0);
      if (offset >= cr.start && offset + length - 1 <= cr.end) return cached.subarray(offset - cr.start, offset - cr.start + length);
      const r = await fetchBytes(url, opts, length, { headers: { Range: `bytes=${offset}-${offset + length - 1}` } });
      const rr = parseContentRange(r.headers.get('content-range'));
      if (
        r.status !== 206 ||
        !rr ||
        rr.start !== offset ||
        rr.end !== offset + length - 1 ||
        rr.total !== total ||
        r.body.length !== length
      ) {
        incomplete('transport.range', '§8.2', 'Range response does not match the request', { path: url, observed: r.status });
      }
      return r.body;
    },
  };
}

// ---------------------------------------------------------------------------------------------
// Publication retrieval

export interface RetrievalAttempt {
  readonly descriptor: string;
  readonly route?: 'entry' | 'bundle';
  readonly outcome: 'invalid' | 'unsupported' | 'incomplete';
  readonly issue: SciIssue;
}

export interface MetadataView {
  readonly metadata: Metadata;
  readonly metadataBytes: Uint8Array;
  readonly readme: string;
  readonly license?: string;
}

export interface RemotePublication {
  readonly id: string;
  readonly digest: string;
  readonly manifest: Manifest;
  readonly signature: Uint8Array;
  readonly descriptorUrl: string;
  readonly descriptor: ArchiveDescriptor;
  /** Route used to obtain the manifest. */
  readonly route: 'entry' | 'bundle';
  /** Sources that were tried and failed before this one. */
  readonly attempts: readonly RetrievalAttempt[];
  /** Fetch one listed file and complete §6.2 before returning its bytes. */
  readFile(path: string): Promise<Uint8Array>;
  /** §6.3: fetch and verify sci.json, README.md, and any LICENSE. */
  readMetadata(): Promise<MetadataView>;
  /** §6.4: verify the complete bundle (ZIP via `bundle_url`, else loose files). */
  verifyComplete(options?: { readonly retain?: (path: string) => boolean }): Promise<VerifiedBundle>;
  /** Fetch the optional `manifest.json.sig.tsr` sidecar via `entry_url`; null if absent. Verify with `verifyTimestampFile`. */
  fetchTimestampSidecar(): Promise<Uint8Array | null>;
}

class Remote implements RemotePublication {
  readonly id: string;
  readonly digest: string;
  readonly manifest: Manifest;
  readonly signature: Uint8Array;
  readonly descriptorUrl: string;
  readonly descriptor: ArchiveDescriptor;
  readonly route: 'entry' | 'bundle';
  readonly attempts: readonly RetrievalAttempt[];
  readonly #opts: RetrievalOptions;
  #zip: Promise<{ source: ByteSource; dir: ZipDirectory }> | undefined;

  constructor(init: {
    manifest: Manifest;
    signature: Uint8Array;
    descriptorUrl: string;
    descriptor: ArchiveDescriptor;
    route: 'entry' | 'bundle';
    attempts: RetrievalAttempt[];
    opts: RetrievalOptions;
    zip?: { source: ByteSource; dir: ZipDirectory };
  }) {
    this.manifest = init.manifest;
    this.id = init.manifest.id;
    this.digest = init.manifest.digest;
    this.signature = init.signature;
    this.descriptorUrl = init.descriptorUrl;
    this.descriptor = init.descriptor;
    this.route = init.route;
    this.attempts = init.attempts;
    this.#opts = init.opts;
    if (init.zip) this.#zip = Promise.resolve(init.zip);
  }

  #openZip(): Promise<{ source: ByteSource; dir: ZipDirectory }> {
    if (!this.#zip) {
      const url = routes.bundle(this.descriptor, this.digest);
      if (!url)
        return Promise.reject(
          new SciError({ code: 'retrieval.no_route', outcome: 'incomplete', requirement: '§8.2', message: 'No bundle_url route' }),
        );
      this.#zip = (async () => {
        // The signed manifest is already verified. Do not receive more ZIP bytes
        // than its canonical size, even if the server ignores Range or lies in headers.
        const expectedSize = zipSizeOf(this.manifest.bytes.length, this.signature.length, this.manifest.files);
        const b = resolveBudget(this.#opts.budget);
        if (expectedSize > b.maxTotalBytes) incomplete('budget.total_bytes', '§6', 'Bundle exceeds the local verification budget');
        const source = await httpRangeSource(url, { ...this.#opts, expectedSize });
        if (source.size !== expectedSize) fail('zip.size_formula', '§2.1', 'ZIP size differs from the signed manifest');
        const dir = await readZipDirectory(source, resolveBudget(this.#opts.budget).maxEntries);
        return { source, dir };
      })();
      this.#zip.catch(() => {
        this.#zip = undefined;
      });
    }
    return this.#zip;
  }

  async readFile(path: string): Promise<Uint8Array> {
    const entry = this.manifest.files.get(path);
    if (!entry) fail('file.not_listed', '§6.2', 'Requested path is not listed in the manifest', { path });
    const max = entry.bytes;
    const tried: SciIssue[] = [];
    const attempt = async (get: () => Promise<Uint8Array | null>): Promise<Uint8Array | null> => {
      try {
        const data = await get();
        if (data === null) return null;
        verifyFileBytes(this.manifest, path, data);
        return data;
      } catch (error) {
        tried.push(toIssue(error));
        return null;
      }
    };
    const fromUrl = async (url: string | undefined): Promise<Uint8Array | null> => {
      if (!url) return null;
      const f = await fetchBytes(url, this.#opts, max);
      return f.status === 200 ? f.body : null;
    };
    const viaEntry = await attempt(() => fromUrl(routes.entry(this.descriptor, this.digest, path)));
    if (viaEntry) return viaEntry;
    const viaBlob = await attempt(() => fromUrl(routes.blob(this.descriptor, entry.sha256)));
    if (viaBlob) return viaBlob;
    if (this.descriptor.bundle_url !== undefined) {
      const viaZip = await attempt(async () => {
        const { source, dir } = await this.#openZip();
        const e = dir.entries.find((x) => x.name === path);
        if (!e) fail('bundle.missing_file', '§6.2', 'ZIP has no entry for the requested path', { path });
        if (e.size !== max) fail('file.size_mismatch', '§6.2', 'ZIP entry size differs from the manifest', { path });
        const [data] = await readZipEntriesSpan(source, [e]);
        return data ?? null;
      });
      if (viaZip) return viaZip;
    }
    // A source serving wrong bytes does not make the publication invalid; report as incomplete.
    throw new SciError({
      code: 'retrieval.file_unavailable',
      outcome: 'incomplete',
      requirement: '§6.2',
      message: `No route returned verified bytes for ${path}${tried.length ? `: ${tried.map((t) => t.code).join(', ')}` : ''}`,
      path,
    });
  }

  async readMetadata(): Promise<MetadataView> {
    const b = resolveBudget(this.#opts.budget);
    const metadataBytes = await this.readFile('sci.json');
    const metadata = parseMetadata(metadataBytes, this.manifest.files, b);
    const readme = checkTextFile(await this.readFile('README.md'), 'README.md');
    if (this.manifest.files.has('LICENSE')) {
      return { metadata, metadataBytes, readme, license: checkTextFile(await this.readFile('LICENSE'), 'LICENSE') };
    }
    return { metadata, metadataBytes, readme };
  }

  async verifyComplete(options: { readonly retain?: (path: string) => boolean } = {}): Promise<VerifiedBundle> {
    const budget = this.#opts.budget;
    if (this.descriptor.bundle_url !== undefined) {
      const b = resolveBudget(budget);
      const expectedSize = zipSizeOf(this.manifest.bytes.length, this.signature.length, this.manifest.files);
      if (expectedSize > b.maxTotalBytes) incomplete('budget.total_bytes', '§6', 'Bundle exceeds the local verification budget');
      const { source } = await this.#openZip();
      if (source.size !== expectedSize) fail('zip.size_formula', '§2.1', 'ZIP size differs from the signed manifest');
      return verifyZipBundle(verificationSource(source), { expectedId: this.id, budget, retain: options.retain });
    }
    const files = new Map<string, Uint8Array>([
      ['manifest.json', this.manifest.bytes],
      ['manifest.json.sig', this.signature],
    ]);
    for (const path of this.manifest.files.keys()) files.set(path, await this.readFile(path));
    return verifyLooseBundle(files, { expectedId: this.id, budget, retain: options.retain ?? (() => true) });
  }

  async fetchTimestampSidecar(): Promise<Uint8Array | null> {
    const url = routes.entry(this.descriptor, this.digest, 'manifest.json.sig.tsr');
    if (!url) return null;
    const f = await fetchBytes(url, this.#opts, resolveBudget(this.#opts.budget).maxTimestampBytes);
    return f.status === 200 ? f.body : null;
  }
}

/**
 * Open a publication from archive descriptor URLs (§8.2) and an expected ID: fetch and validate
 * each descriptor, retrieve manifest + signature via `entry_url` or ZIP ranges on `bundle_url`,
 * and complete §6.1. Sources serving bytes that do not match the ID are skipped. If bytes matching
 * the ID fail the manifest checks, the publication itself is `invalid` (or `unsupported`).
 */
export async function openPublication(
  descriptorUrls: readonly string[],
  expectedId: string,
  opts: RetrievalOptions = {},
): Promise<RemotePublication> {
  const digest = parsePublicationRef(expectedId);
  if (!digest) fail('retrieval.id', '§8.1.1', 'Malformed publication ID', { observed: expectedId });
  const b = resolveBudget(opts.budget);
  const attempts: RetrievalAttempt[] = [];
  const record = (descriptor: string, route: 'entry' | 'bundle' | undefined, error: unknown): void => {
    const issue = toIssue(error);
    attempts.push({ descriptor, ...(route ? { route } : {}), outcome: issue.outcome, issue });
  };
  for (const url of descriptorUrls) {
    let descriptor: ArchiveDescriptor;
    try {
      descriptor = await fetchArchiveDescriptor(url, opts);
    } catch (error) {
      record(url, undefined, error);
      continue;
    }
    const entryManifest = routes.entry(descriptor, digest, 'manifest.json');
    const entrySig = routes.entry(descriptor, digest, 'manifest.json.sig');
    if (entryManifest && entrySig) {
      try {
        const m = requireOk(await fetchBytes(entryManifest, opts, b.maxManifestBytes), entryManifest);
        const s = requireOk(await fetchBytes(entrySig, opts, 64), entrySig);
        return new Remote({
          manifest: verifyManifestFor(m, s, digest, b),
          signature: s,
          descriptorUrl: url,
          descriptor,
          route: 'entry',
          attempts,
          opts,
        });
      } catch (error) {
        if (isDefinitive(error)) throw error;
        record(url, 'entry', error);
      }
    }
    const bundleUrl = routes.bundle(descriptor, digest);
    if (bundleUrl) {
      try {
        const source = await httpRangeSource(bundleUrl, opts);
        const dir = await readZipDirectory(source, b.maxEntries);
        const [me, se] = dir.entries;
        if (me?.name !== 'manifest.json' || se?.name !== 'manifest.json.sig')
          fail('zip.first_entries', '§2.2', 'First entries must be manifest.json then manifest.json.sig');
        if (me.size > b.maxManifestBytes) incomplete('budget.manifest_bytes', '§6', 'manifest.json exceeds the local size budget');
        const [m, s] = await readZipEntriesSpan(source, [me, se]);
        if (!m || !s) fail('zip.first_entries', '§2.2', 'Missing manifest entries');
        return new Remote({
          manifest: verifyManifestFor(m, s, digest, b),
          signature: s,
          descriptorUrl: url,
          descriptor,
          route: 'bundle',
          attempts,
          opts,
          zip: { source, dir },
        });
      } catch (error) {
        if (isDefinitive(error)) throw error;
        record(url, 'bundle', error);
      }
    }
  }
  throw new SciError({
    code: 'retrieval.no_verified_source',
    outcome: 'incomplete',
    requirement: '§6, §8',
    message: `No source provided a verifiable manifest (${attempts.length} attempts${attempts.length ? `: ${attempts.map((a) => a.issue.code).join(', ')}` : ''})`,
  });
}

class DefinitiveError extends SciError {}

function isDefinitive(error: unknown): error is DefinitiveError {
  return error instanceof DefinitiveError;
}

/**
 * Verify manifest bytes for a known digest. If the bytes hash to the requested ID, failures of the
 * manifest's own content are definitive for the publication. Signature failures are not, since the
 * signature file is not covered by the ID and another source may serve the correct one.
 */
function verifyManifestFor(m: Uint8Array, s: Uint8Array, digest: string, budget: Budget): Manifest {
  try {
    return verifyManifest(m, s, { expectedId: digest, budget });
  } catch (error) {
    const issue = toIssue(error);
    if (issue.outcome !== 'incomplete' && sha256Hex(m) === digest && !issue.code.startsWith('signature.')) throw new DefinitiveError(issue);
    throw error;
  }
}

/** Outcome-returning wrapper for {@link openPublication}. */
export function tryOpenPublication(
  descriptorUrls: readonly string[],
  expectedId: string,
  opts?: RetrievalOptions,
): Promise<CheckResult<RemotePublication>> {
  return captureAsync(() => openPublication(descriptorUrls, expectedId, opts));
}
