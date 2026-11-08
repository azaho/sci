/**
 * Conformance case execution shared by the JSON-lines adapter (Node) and the corpus test
 * (Node and Deno). Uses Node built-ins only for file access; protocol logic is the library's.
 */
import { open, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import {
  applyIndexEvent,
  type ByteSource,
  EMPTY_INDEX_STATE,
  expandTemplate,
  type IndexEvent,
  type IndexPolicyState,
  interpretPublicationLookup,
  interpretTimestampLookup,
  isAssociationVerified,
  keyStatus,
  locationCount,
  parseArchiveDescriptor,
  parseIndexDescriptor,
  parseSubmissionRequest,
  SciError,
  timestampStatus,
  toIssue,
  validateSubmissionResponse,
  verifyClaim,
  verifyLooseBundle,
  verifyManifest,
  verifyManifestScope,
  verifyRevocation,
  verifyTimestampFile,
  verifyZipBundle,
} from '../src/index.ts';

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export interface Case {
  readonly id: string;
  readonly kind: string;
  readonly artifacts: Record<string, string>;
  readonly context?: Record<string, Json>;
}

const VERBOSE = ((): boolean => {
  try {
    return Boolean(process.env.SCI_ADAPTER_VERBOSE);
  } catch {
    return false; // Deno without --allow-env
  }
})();

function log(message: string): void {
  console.error(message);
}

/** A bounded-memory ByteSource over a file. */
async function fileSource(path: string): Promise<ByteSource & { close(): Promise<void> }> {
  const handle = await open(path, 'r');
  const { size } = await handle.stat();
  return {
    size,
    async read(offset, length) {
      const buf = new Uint8Array(length);
      let done = 0;
      while (done < length) {
        const { bytesRead } = await handle.read(buf, done, length - done, offset + done);
        if (bytesRead === 0) throw new RangeError('short read');
        done += bytesRead;
      }
      return buf;
    },
    close: () => handle.close(),
  };
}

function outcomeOf(error: unknown, id: string): 'invalid' | 'unsupported' | 'incomplete' {
  if (!(error instanceof SciError))
    log(`${id}: unexpected error: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}`);
  const issue = toIssue(error);
  if (VERBOSE) log(`${id}: ${issue.outcome} ${issue.code} ${issue.message}${issue.path ? ` @${issue.path}` : ''}`);
  return issue.outcome;
}

async function publication(c: Case, corpus: string): Promise<Record<string, Json>> {
  const ctx = c.context ?? {};
  if (ctx.transport === 'unavailable') return { outcome: 'incomplete', baseline: null };
  const expectedId = typeof ctx.publication_id === 'string' ? ctx.publication_id : undefined;
  const budget = typeof ctx.verification_budget_bytes === 'number' ? { maxTotalBytes: ctx.verification_budget_bytes } : undefined;
  try {
    const zipPath = c.artifacts['bundle.zip'];
    if (zipPath !== undefined) {
      const src = await fileSource(join(corpus, zipPath));
      try {
        const r = await verifyZipBundle(src, { expectedId, budget });
        return { outcome: 'valid', baseline: r.baseline.baseline, zip_bytes: r.zipBytes, zip_sha256: r.zipSha256 ?? null };
      } finally {
        await src.close();
      }
    }
    const files = new Map<string, Uint8Array>();
    for (const [name, rel] of Object.entries(c.artifacts)) files.set(name, new Uint8Array(await readFile(join(corpus, rel))));
    if (ctx.scope === 'manifest') {
      const m = files.get('manifest.json') ?? new Uint8Array();
      const s = files.get('manifest.json.sig') ?? new Uint8Array();
      const r = verifyManifestScope(m, s, { expectedId, budget });
      return { outcome: 'valid', baseline: null, zip_bytes: r.zipBytes, within_baseline_zip_limit: r.withinBaselineZipLimit };
    }
    const r = await verifyLooseBundle(files, { expectedId, budget });
    return { outcome: 'valid', baseline: r.baseline.baseline, zip_bytes: r.zipBytes, zip_sha256: r.zipSha256 ?? null };
  } catch (error) {
    return { outcome: outcomeOf(error, c.id), baseline: null };
  }
}

async function artifact(c: Case, corpus: string, name: string): Promise<Uint8Array> {
  const rel = c.artifacts[name];
  if (rel === undefined) throw new Error(`missing artifact ${name}`);
  return new Uint8Array(await readFile(join(corpus, rel)));
}

async function record(c: Case, corpus: string): Promise<Record<string, Json>> {
  const ctx = c.context ?? {};
  if (ctx.transport === 'unavailable') return { outcome: 'incomplete' };
  try {
    const raw = await artifact(c, corpus, 'record.json');
    const sig = await artifact(c, corpus, 'record.json.sig');
    const x = String(ctx.expected_public_key_x);
    if (c.kind === 'revocation') {
      verifyRevocation(raw, sig, { expectedPublicKeyX: x });
    } else {
      const account = ctx.requested_account as { type: string; id: string };
      verifyClaim(raw, sig, {
        expectedPublicKeyX: x,
        requestedAccount: account,
        now: new Date(String(ctx.now)),
        knownRevocation: ctx.known_revocation === true,
        retrievedFromAccountLocation: ctx.retrieved_from_account_location === true,
      });
    }
    return { outcome: 'valid' };
  } catch (error) {
    return { outcome: outcomeOf(error, c.id) };
  }
}

async function timestamp(c: Case, corpus: string): Promise<Record<string, Json>> {
  const ctx = c.context ?? {};
  const tsr = await artifact(c, corpus, 'proofs.tsr');
  const sig = await artifact(c, corpus, 'signature.bin');
  const result: Record<string, Json> = {};
  const isRevocation = c.artifacts['revocation.json'] !== undefined;
  try {
    if (isRevocation) {
      verifyRevocation(await artifact(c, corpus, 'revocation.json'), sig);
      result.revocation_outcome = 'valid';
    } else {
      const m = verifyManifest(await artifact(c, corpus, 'manifest.json'), sig, { expectedId: String(ctx.publication_id) });
      result.publication_outcome = 'valid';
      result.publication_id_unchanged = m.id === ctx.publication_id;
    }
  } catch (error) {
    // §6.8: no verified timestamp can be reported without the record checks.
    const outcome = outcomeOf(error, c.id);
    return {
      ...result,
      [isRevocation ? 'revocation_outcome' : 'publication_outcome']: outcome,
      outcome: 'invalid',
      framing: 'malformed',
      proofs: [],
      stop_offset: 0,
    };
  }
  const read = async (list: Json | undefined) =>
    Promise.all(((list as string[] | undefined) ?? []).map(async (p) => new Uint8Array(await readFile(join(corpus, p)))));
  const r = await verifyTimestampFile(tsr, sig, {
    trustAnchors: await read(ctx.trust_anchors),
    crls: await read(ctx.crls),
    verificationTime: new Date(String(ctx.verification_time)),
    revocationPolicy:
      ctx.revocation_policy === 'none' ? 'none' : ctx.revocation_policy === 'crl_if_available' ? 'crl_if_available' : 'require_current_crl',
  });
  if (VERBOSE) for (const p of r.proofs) if (p.issue) log(`${c.id}@${p.offset}: ${p.result} ${p.issue.code} ${p.issue.message}`);
  return {
    ...result,
    outcome: r.outcome,
    framing: r.framing,
    stop_offset: r.stopOffset,
    proofs: r.proofs.map((p) => ({
      offset: p.offset,
      bytes: p.bytes,
      result: p.result,
      ...(p.result === 'verified' && p.genTime !== undefined ? { gen_time: p.genTime } : {}),
      ...(p.result === 'verified' && p.accuracySeconds !== undefined ? { accuracy_seconds: p.accuracySeconds } : {}),
    })),
  };
}

async function discovery(c: Case, corpus: string): Promise<Record<string, Json>> {
  try {
    const raw = await artifact(c, corpus, 'descriptor.json');
    if (c.context?.descriptor_kind === 'index') parseIndexDescriptor(raw);
    else parseArchiveDescriptor(raw);
    return { outcome: 'valid' };
  } catch (error) {
    return { outcome: outcomeOf(error, c.id) };
  }
}

async function submission(c: Case, corpus: string): Promise<Record<string, Json>> {
  const ctx = c.context ?? {};
  try {
    if (ctx.phase === 'request') parseSubmissionRequest(await artifact(c, corpus, 'request.json'));
    else
      validateSubmissionResponse(
        Number(ctx.status),
        String(ctx.content_type),
        await artifact(c, corpus, 'response.json'),
        String(ctx.requested_id),
      );
    return { outcome: 'valid' };
  } catch (error) {
    return { outcome: outcomeOf(error, c.id) };
  }
}

const ALLOWED: Record<string, string[]> = {
  bundle_url: ['id', 'id2'],
  entry_url: ['id', 'id2', '+path'],
  blob_url: ['sha256'],
  list_url: [],
  publication_lookup_url: ['id'],
  revocation_lookup_url: ['x', 'file'],
  timestamp_lookup_url: ['id'],
};

async function template(c: Case, corpus: string): Promise<Record<string, Json>> {
  const templates = JSON.parse(new TextDecoder().decode(await artifact(c, corpus, 'templates.json'))) as Record<string, string>;
  const vars = JSON.parse(new TextDecoder().decode(await artifact(c, corpus, 'variables.json'))) as Record<string, string>;
  const out: Record<string, Json> = {};
  for (const [field, allowed] of Object.entries(ALLOWED)) {
    const t = templates[field];
    if (typeof t === 'string') out[field] = expandTemplate(t, vars, allowed);
  }
  return out;
}

async function http(c: Case, corpus: string): Promise<Record<string, Json>> {
  const ctx = c.context ?? {};
  const body = await artifact(c, corpus, 'response.bin');
  const status = Number(ctx.status);
  // The operation is identified by the case (publication vs. timestamp lookup), as for a real client.
  if (c.id.startsWith('timestamp-lookup')) {
    const r = interpretTimestampLookup(status, body);
    return {
      proofs_retained: r.proofsRetained,
      timestamp_existence: r.timestampExistence,
      proofs: r.proofs,
      readers_must_verify: r.readersMustVerify,
    };
  }
  return { sources: [...interpretPublicationLookup(status, body).sources] };
}

const PRIMARY = 'https://archive.example/.well-known/sci/archive.json';
const MIRROR = 'https://mirror.example/.well-known/sci/archive.json';

async function scenario(c: Case, corpus: string): Promise<Record<string, Json>> {
  const ctx = c.context ?? {};
  const keyX = String(ctx.key);
  const id = `publication:${String(ctx.publication)}`;
  const hint = { type: 'domain', id: 'example.org' };
  const { events } = JSON.parse(new TextDecoder().decode(await artifact(c, corpus, 'events.json'))) as { events: string[] };
  const map: Record<string, IndexEvent[]> = {
    admit_publication: [{ type: 'admission_requested', id, keyX }],
    attempt_admission: [{ type: 'admission_requested', id, keyX }],
    verify_revocation: [{ type: 'revocation_verified', keyX }],
    source_returns_404: [{ type: 'revocation_source_removed', keyX }],
    lookup_revocation_404: [{ type: 'revocation_lookup_not_found', keyX }],
    verify_earlier_timestamp: [{ type: 'timestamp_verified', id }],
    timestamp_authority_unavailable: [{ type: 'timestamp_unavailable', id }],
    lookup_timestamp_404: [{ type: 'timestamp_lookup_not_found', id }],
    submit_location: [{ type: 'location_registered', id, descriptor: PRIMARY }],
    lose_success_response: [],
    repeat_same_submission: [{ type: 'location_registered', id, descriptor: PRIMARY }],
    submit_second_location_same_id: [{ type: 'location_registered', id, descriptor: MIRROR }],
    submit_location_with_unverified_account_hint: [
      { type: 'location_registered', id, descriptor: PRIMARY },
      { type: 'association_hint', keyX, account: hint },
    ],
  };
  let state: IndexPolicyState = EMPTY_INDEX_STATE;
  const out: Record<string, Json> = {};
  for (const name of events) {
    const list = map[name];
    if (!list) throw new Error(`unknown scenario event ${name}`);
    for (const event of list) {
      const r = applyIndexEvent(state, event);
      state = r.state;
      if (name === 'attempt_admission' && r.admission) {
        out.new_admission = r.admission.decision;
        if (r.admission.decision === 'reject') out.reason = r.admission.reason;
      }
    }
    if (name === 'lookup_revocation_404') out.must_not_infer = 'never_revoked';
    if (name === 'lookup_timestamp_404') out.must_not_infer = 'no_timestamp_exists';
  }
  out.catalog_entry = state.catalog.has(id) ? 'retained' : 'absent';
  out.key_status = keyStatus(state, keyX);
  out.timestamp_status = timestampStatus(state, id);
  out.location_registrations = locationCount(state);
  out.publication_ids = state.registrations.size;
  out.association_verified = isAssociationVerified(state, keyX, hint);
  return out;
}

export async function runCase(c: Case, corpus: string): Promise<Record<string, Json>> {
  switch (c.kind) {
    case 'publication':
      return publication(c, corpus);
    case 'claim':
    case 'revocation':
      return record(c, corpus);
    case 'timestamp':
      return timestamp(c, corpus);
    case 'discovery':
      return discovery(c, corpus);
    case 'submission':
      return submission(c, corpus);
    case 'template':
      return template(c, corpus);
    case 'http':
      return http(c, corpus);
    case 'scenario':
      return scenario(c, corpus);
    default:
      throw new Error(`unsupported kind ${c.kind}`);
  }
}
