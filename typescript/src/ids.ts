/** Publication identifier forms (§2, §4.4, §8.1.1) and schema checks (§5.1). */
import { fail, unsupported } from './errors.ts';
import { isObject, type JsonObject, type JsonValue } from './json.ts';

const DIGEST = /^[0-9a-f]{64}$/;

/** True for a bare digest: 64 lowercase hexadecimal characters (§8.1.1). */
export function isDigest(value: unknown): value is string {
  return typeof value === 'string' && DIGEST.test(value);
}

/** True for a publication identifier `sha256:<digest>` (§2). */
export function isPublicationId(value: unknown): value is string {
  return typeof value === 'string' && value.startsWith('sha256:') && DIGEST.test(value.slice(7));
}

export type IdForm = 'digest' | 'id' | 'sci';

/**
 * Parse a publication reference in any of its protocol spellings and return the bare digest:
 * `<digest>` (§8.1.1), `sha256:<digest>` (§2), or `sci:sha256:<digest>` (§4.4).
 * Returns null if the input is not exactly one of these forms.
 */
export function parsePublicationRef(value: string, accept: readonly IdForm[] = ['digest', 'id', 'sci']): string | null {
  if (accept.includes('digest') && DIGEST.test(value)) return value;
  if (accept.includes('id') && value.startsWith('sha256:') && DIGEST.test(value.slice(7))) return value.slice(7);
  if (accept.includes('sci') && value.startsWith('sci:sha256:') && DIGEST.test(value.slice(11))) return value.slice(11);
  return null;
}

export function digestToId(digest: string): string {
  return `sha256:${digest}`;
}

/** Schema strings this implementation supports. */
export const SCHEMAS = {
  publication: 'sci-publication/0.1',
  claim: 'sci-key-claim/0.1',
  revocation: 'sci-key-revocation/0.1',
  index: 'sci-index/0.1',
  archive: 'sci-archive/0.1',
} as const;

const KNOWN = new Set<string>(Object.values(SCHEMAS));

/**
 * Check that a parsed record is an object whose `schema` is `expected` (§5.1, §6).
 * A known schema in the wrong slot is `invalid`; an unknown schema string is `unsupported`.
 */
export function checkRecordSchema(value: JsonValue, expected: string, requirement: string, path?: string): JsonObject {
  if (!isObject(value)) fail('record.not_object', requirement, 'Record must be a JSON object', { path });
  const schema = value.get('schema');
  if (typeof schema !== 'string') {
    fail('record.schema_missing', requirement, 'Record must have a string schema field', { path });
  }
  if (schema !== expected) {
    if (KNOWN.has(schema)) {
      fail('record.wrong_schema', '§5.1', `Expected schema ${expected}, found another supported record type`, {
        path,
        observed: schema,
      });
    }
    unsupported('record.unsupported_schema', '§6', 'Unknown schema value', { path, observed: schema });
  }
  return value;
}

/** Ensure an object has exactly the required fields plus any of the optional ones. */
export function checkFields(
  obj: JsonObject,
  required: readonly string[],
  optional: readonly string[],
  requirement: string,
  path = '',
): void {
  for (const r of required) {
    if (!obj.has(r)) fail('record.missing_field', requirement, `Missing required field "${r}"`, { path: `${path}/${r}` });
  }
  for (const k of obj.keys()) {
    if (!required.includes(k) && !optional.includes(k)) {
      fail('record.unknown_field', requirement, `Field "${k}" is not permitted`, { path: `${path}/${k}` });
    }
  }
}
