/** SCI §8.1 index descriptor, §8.2 archive descriptor, §8.1.4 submission, and lookup responses. */
import { type Budget, resolveBudget } from './budget.ts';
import { isAbsoluteHttpUrl } from './citation.ts';
import { type Account, parseAccount } from './claims.ts';
import { check, fail, incomplete } from './errors.ts';
import { checkFields, checkRecordSchema, isDigest, SCHEMAS } from './ids.ts';
import { isArray, isObject, type JsonObject, type JsonValue, parseJsonBytes } from './json.ts';
import { checkUrlTemplate, expandParts, type TemplatePart } from './template.ts';

export interface IndexDescriptor {
  readonly schema: typeof SCHEMAS.index;
  readonly publication_lookup_url: string;
  readonly revocation_lookup_url: string;
  readonly timestamp_lookup_url: string;
  readonly submit_url?: string;
  readonly policy_url?: string;
  readonly docs_url?: string;
  /** All fields as parsed, including extensions. */
  readonly fields: JsonObject;
}

export interface ArchiveDescriptor {
  readonly schema: typeof SCHEMAS.archive;
  readonly bundle_url?: string;
  readonly entry_url?: string;
  readonly blob_url?: string;
  readonly list_url?: string;
  readonly fields: JsonObject;
}

const INDEX_FIELDS: Record<string, { allowed: string[]; required: string[]; mandatory: boolean }> = {
  publication_lookup_url: { allowed: ['id'], required: ['id'], mandatory: true },
  revocation_lookup_url: { allowed: ['x', 'file'], required: ['x', 'file'], mandatory: true },
  timestamp_lookup_url: { allowed: ['id'], required: ['id'], mandatory: true },
  submit_url: { allowed: [], required: [], mandatory: false },
  policy_url: { allowed: [], required: [], mandatory: false },
  docs_url: { allowed: [], required: [], mandatory: false },
};

const ARCHIVE_FIELDS: Record<string, { allowed: string[] }> = {
  bundle_url: { allowed: ['id', 'id2'] },
  entry_url: { allowed: ['id', 'id2', '+path'] },
  blob_url: { allowed: ['sha256'] },
  list_url: { allowed: [] },
};

function parseDoc(bytes: Uint8Array, path: string, budget?: Partial<Budget>): JsonValue {
  const b = resolveBudget(budget);
  if (bytes.length > b.maxJsonBytes) incomplete('budget.json_bytes', '§6', 'Document exceeds the local size budget', { path });
  return parseJsonBytes(bytes, { maxDepth: b.maxJsonDepth, path });
}

/** Validate an index descriptor (§8.1). Additional fields are permitted. */
export function validateIndexDescriptor(value: JsonValue): IndexDescriptor {
  const obj = checkRecordSchema(value, SCHEMAS.index, '§8.1', 'index.json');
  const out: Record<string, string> = {};
  for (const [name, rule] of Object.entries(INDEX_FIELDS)) {
    if (!obj.has(name)) {
      check(!rule.mandatory, 'descriptor.missing_field', '§8.1', `Missing required field ${name}`, { path: `/${name}` });
      continue;
    }
    const v = obj.get(name);
    if (typeof v !== 'string') fail('descriptor.type', '§8.1', `${name} must be a string`, { path: `/${name}` });
    checkUrlTemplate(v, rule.allowed, rule.required, '§8.1', `/${name}`);
    out[name] = v;
  }
  return { ...(out as unknown as Omit<IndexDescriptor, 'fields' | 'schema'>), schema: SCHEMAS.index, fields: obj };
}

/**
 * Validate an archive descriptor (§8.2). At least one of `bundle_url` and `entry_url` is required,
 * since the archiver must serve the manifest and signature through one of them.
 */
export function validateArchiveDescriptor(value: JsonValue): ArchiveDescriptor {
  const obj = checkRecordSchema(value, SCHEMAS.archive, '§8.2', 'archive.json');
  const out: Record<string, string> = {};
  for (const [name, rule] of Object.entries(ARCHIVE_FIELDS)) {
    if (!obj.has(name)) continue;
    const v = obj.get(name);
    if (typeof v !== 'string') fail('descriptor.type', '§8.2', `${name} must be a string`, { path: `/${name}` });
    checkUrlTemplate(v, rule.allowed, [], '§8.2', `/${name}`);
    out[name] = v;
  }
  check(
    out.bundle_url !== undefined || out.entry_url !== undefined,
    'descriptor.no_manifest_route',
    '§8.2',
    'Archive descriptor must provide bundle_url or entry_url',
  );
  return { ...(out as Omit<ArchiveDescriptor, 'fields' | 'schema'>), schema: SCHEMAS.archive, fields: obj };
}

export function parseIndexDescriptor(bytes: Uint8Array, budget?: Partial<Budget>): IndexDescriptor {
  return validateIndexDescriptor(parseDoc(bytes, 'index.json', budget));
}

export function parseArchiveDescriptor(bytes: Uint8Array, budget?: Partial<Budget>): ArchiveDescriptor {
  return validateArchiveDescriptor(parseDoc(bytes, 'archive.json', budget));
}

function expandField(template: string, allowed: string[], vars: Record<string, string>): string {
  const parts: TemplatePart[] = checkUrlTemplate(template, allowed, [], '§8', '');
  return expandParts(parts, vars);
}

/** URL builders for descriptor routes. IDs are bare digests (§8.1.1). */
export const routes = {
  publicationLookup(d: IndexDescriptor, digest: string): string {
    return expandField(d.publication_lookup_url, ['id'], { id: digest });
  },
  revocationLookup(d: IndexDescriptor, x: string, file: 'revocation.json' | 'revocation.json.sig' | 'revocation.json.sig.tsr'): string {
    return expandField(d.revocation_lookup_url, ['x', 'file'], { x, file });
  },
  timestampLookup(d: IndexDescriptor, digest: string): string {
    return expandField(d.timestamp_lookup_url, ['id'], { id: digest });
  },
  bundle(d: ArchiveDescriptor, digest: string): string | undefined {
    return d.bundle_url === undefined ? undefined : expandField(d.bundle_url, ['id', 'id2'], { id: digest, id2: digest.slice(0, 2) });
  },
  entry(d: ArchiveDescriptor, digest: string, path: string): string | undefined {
    return d.entry_url === undefined
      ? undefined
      : expandField(d.entry_url, ['id', 'id2', '+path'], { id: digest, id2: digest.slice(0, 2), path });
  },
  blob(d: ArchiveDescriptor, sha256: string): string | undefined {
    return d.blob_url === undefined ? undefined : expandField(d.blob_url, ['sha256'], { sha256 });
  },
};

/** Descriptor discovery locations (§8.2.1). */
export const descriptorLocations = {
  index(domain: string): string {
    return `https://${domain}/.well-known/sci/index.json`;
  },
  domain(domain: string): string {
    return `https://${domain}/.well-known/sci/archive.json`;
  },
  github(owner: string, repo: string): string {
    return `https://raw.githubusercontent.com/${owner}/${repo}/main/sci/archive.json`;
  },
  huggingface(repositoryUrl: string): string {
    return `${repositoryUrl.replace(/\/+$/, '')}/resolve/main/sci/archive.json`;
  },
};

// ---------------------------------------------------------------------------------------------
// §8.1.4 submission

export interface SubmissionRequest {
  readonly id: string;
  readonly descriptors: readonly string[];
  readonly account?: Account;
}

function descriptorUrlList(value: JsonValue | undefined, path: string, requirement: string, nonempty: boolean): string[] {
  if (!isArray(value)) fail('submission.descriptors', requirement, 'descriptors must be an array', { path });
  check(!nonempty || value.length > 0, 'submission.descriptors_empty', requirement, 'descriptors must be nonempty', { path });
  const seen = new Set<string>();
  return value.map((u, i) => {
    check(
      typeof u === 'string' && isAbsoluteHttpUrl(u),
      'submission.descriptor_url',
      requirement,
      'Descriptor must be an absolute HTTP(S) URL',
      {
        path: `${path}/${i}`,
        observed: u,
      },
    );
    check(!seen.has(u), 'submission.descriptor_duplicate', requirement, 'Descriptor URLs must be distinct', { path: `${path}/${i}` });
    seen.add(u);
    return u;
  });
}

export function validateSubmissionRequest(value: JsonValue): SubmissionRequest {
  if (!isObject(value)) fail('submission.not_object', '§8.1.4', 'Request must be a JSON object');
  checkFields(value, ['id', 'descriptors'], ['account'], '§8.1.4');
  const id = value.get('id');
  check(isDigest(id), 'submission.id', '§8.1.4', 'id must be 64 lowercase hexadecimal characters', { path: '/id', observed: id });
  const descriptors = descriptorUrlList(value.get('descriptors'), '/descriptors', '§8.1.4', true);
  if (value.has('account')) return { id, descriptors, account: parseAccount(value.get('account'), '/account', '§8.1.4, §5.2.1') };
  return { id, descriptors };
}

export function parseSubmissionRequest(bytes: Uint8Array, budget?: Partial<Budget>): SubmissionRequest {
  return validateSubmissionRequest(parseDoc(bytes, 'request', budget));
}

export type SubmissionResponse =
  | { readonly kind: 'success'; readonly status: number; readonly id: string; readonly fields: JsonObject }
  | {
      readonly kind: 'error';
      readonly status: number;
      readonly code: string;
      readonly message: string;
      readonly retryable: boolean;
      readonly fields: JsonObject;
    };

/** True if a Content-Type header names `application/json` (parameters allowed). */
export function isJsonContentType(contentType: string | null | undefined): boolean {
  return typeof contentType === 'string' && contentType.split(';', 1)[0]?.trim().toLowerCase() === 'application/json';
}

/** Validate a §8.1.4 response for the requested digest. */
export function validateSubmissionResponse(
  status: number,
  contentType: string | null | undefined,
  body: Uint8Array,
  requestedId: string,
  budget?: Partial<Budget>,
): SubmissionResponse {
  check(isJsonContentType(contentType), 'submission.content_type', '§8.1.4', 'Response must use application/json', {
    observed: contentType ?? null,
  });
  const value = parseDoc(body, 'response', budget);
  if (!isObject(value)) fail('submission.not_object', '§8.1.4', 'Response must be a JSON object');
  if (status === 200 || status === 201) {
    check(value.get('id') === requestedId, 'submission.response_id', '§8.1.4', 'Response id must equal the requested digest', {
      observed: value.get('id') ?? null,
    });
    return { kind: 'success', status, id: requestedId, fields: value };
  }
  check(status >= 400 && status <= 599, 'submission.status', '§8.1.4', 'Response status must be 200, 201, 4xx, or 5xx', {
    observed: status,
  });
  const error = value.get('error');
  check(isObject(error), 'submission.error', '§8.1.4', 'Error response must contain an error object');
  const code = error.get('code');
  const message = error.get('message');
  const retryable = error.get('retryable');
  check(
    typeof code === 'string' && typeof message === 'string' && typeof retryable === 'boolean',
    'submission.error',
    '§8.1.4',
    'error must contain string code, string message, and Boolean retryable',
  );
  return { kind: 'error', status, code, message, retryable, fields: value };
}

// ---------------------------------------------------------------------------------------------
// Lookup responses (§8.1.1–§8.1.3)

/** Interpret a publication lookup response (§8.1.1). `404` and an empty list both mean no sources. */
export function interpretPublicationLookup(
  status: number,
  body: Uint8Array,
  budget?: Partial<Budget>,
): { readonly sources: readonly string[] } {
  if (status === 404) return { sources: [] };
  if (status !== 200) incomplete('lookup.status', '§8.1.1', 'Publication lookup did not succeed', { observed: status });
  const value = parseDoc(body, 'lookup', budget);
  if (!isObject(value)) fail('lookup.not_object', '§8.1.1', 'Lookup response must be a JSON object');
  check(value.has('descriptors'), 'lookup.descriptors', '§8.1.1', 'Lookup response must contain descriptors');
  return { sources: descriptorUrlList(value.get('descriptors'), '/descriptors', '§8.1.1', false) };
}
