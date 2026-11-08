/** SCI §4 publication metadata (`sci.json`) and §6.3 metadata checks. */
import { type Budget, resolveBudget } from './budget.ts';
import { parseCitationTarget } from './citation.ts';
import { check, fail, incomplete } from './errors.ts';
import { checkFields } from './ids.ts';
import { isArray, isObject, type JsonObject, type JsonValue, parseJsonBytes } from './json.ts';
import type { ManifestEntry } from './manifest.ts';
import { checkText } from './text.ts';

export interface Category {
  readonly scheme: string;
  readonly code: string;
}

export interface DisplayItem {
  readonly path: string;
  readonly caption?: string;
}

export const CITATION_RELATIONS = ['uses', 'reviews', 'supports', 'challenges', 'supersedes'] as const;
export type CitationRelation = (typeof CITATION_RELATIONS)[number];

export interface Citation {
  readonly target: string;
  readonly relations?: readonly CitationRelation[];
  readonly reason?: string;
  /** Derived equivalence key (not a wire field). */
  readonly targetKey: string;
  readonly targetKind: 'sci' | 'doi' | 'url';
}

export interface Metadata {
  readonly title: string;
  readonly abstract: string;
  readonly categories?: readonly Category[];
  readonly keywords?: readonly string[];
  readonly display?: readonly DisplayItem[];
  readonly citations?: readonly Citation[];
}

/** Validate all decoded strings in sci.json, including object keys, against §4.2 Assignables. */
function checkAllStrings(v: JsonValue, path: string): void {
  if (typeof v === 'string') checkText(v, { controls: true, path });
  else if (isArray(v)) {
    for (let i = 0; i < v.length; i++) checkAllStrings(v[i] as JsonValue, `${path}/${i}`);
  } else if (isObject(v)) {
    for (const [k, x] of v) {
      checkText(k, { controls: false, path: `${path}/${k}` });
      checkAllStrings(x, `${path}/${k}`);
    }
  }
}

function str(v: JsonValue | undefined, path: string, max: number, controls = false): string {
  if (typeof v !== 'string') fail('metadata.type', '§4', 'Expected a string', { path, observed: v });
  checkText(v, { controls, nonblank: true, maxCodePoints: max, path });
  return v;
}

function arr(obj: JsonObject, key: string, max: number | undefined): JsonValue[] | undefined {
  if (!obj.has(key)) return undefined;
  const a = obj.get(key);
  if (!isArray(a)) fail('metadata.type', '§4', `${key} must be an array`, { path: `/${key}` });
  if (max !== undefined) {
    check(a.length <= max, 'metadata.array_length', '§4', `${key} has more than ${max} items`, {
      path: `/${key}`,
      observed: a.length,
    });
  }
  return a;
}

/**
 * Validate parsed sci.json against §4 given the manifest's listed files (for display paths).
 */
export function validateMetadata(value: JsonValue, files: ReadonlyMap<string, ManifestEntry>): Metadata {
  if (!isObject(value)) fail('metadata.not_object', '§4', 'sci.json must be a JSON object');
  checkAllStrings(value, '');
  checkFields(value, ['title', 'abstract'], ['categories', 'keywords', 'display', 'citations'], '§4');
  const title = str(value.get('title'), '/title', 200);
  const abstract = str(value.get('abstract'), '/abstract', 2500, true);

  const keywordsRaw = arr(value, 'keywords', 20);
  const keywords = keywordsRaw?.map((k, i) => str(k, `/keywords/${i}`, 64));

  const categoriesRaw = arr(value, 'categories', 3);
  const seenCategories = new Set<string>();
  const categories = categoriesRaw?.map((c, i): Category => {
    const p = `/categories/${i}`;
    if (!isObject(c)) fail('metadata.type', '§4.1', 'Category must be an object', { path: p });
    checkFields(c, ['scheme', 'code'], [], '§4.1', p);
    const scheme = c.get('scheme');
    check(
      typeof scheme === 'string' && /^[a-z][a-z0-9.-]{0,63}$/.test(scheme),
      'metadata.category_scheme',
      '§4.1',
      'Invalid category scheme',
      {
        path: `${p}/scheme`,
        observed: scheme,
      },
    );
    const code = str(c.get('code'), `${p}/code`, 128);
    const pair = JSON.stringify([scheme, code]);
    check(!seenCategories.has(pair), 'metadata.category_duplicate', '§4.1', 'Duplicate (scheme, code) pair', { path: p });
    seenCategories.add(pair);
    return { scheme, code };
  });

  const displayRaw = arr(value, 'display', undefined);
  const seenDisplay = new Set<string>();
  const display = displayRaw?.map((d, i): DisplayItem => {
    const p = `/display/${i}`;
    if (!isObject(d)) fail('metadata.type', '§4.3', 'Display item must be an object', { path: p });
    checkFields(d, ['path'], ['caption'], '§4.3', p);
    const path = d.get('path');
    check(
      typeof path === 'string' && path.startsWith('files/') && files.has(path),
      'metadata.display_path',
      '§4.3',
      'Display path must exactly match an entry under files/ in the manifest',
      { path: `${p}/path`, observed: path },
    );
    check(!seenDisplay.has(path), 'metadata.display_duplicate', '§4.3', 'Display paths must be distinct', {
      path: `${p}/path`,
      observed: path,
    });
    seenDisplay.add(path);
    if (d.has('caption')) return { path, caption: str(d.get('caption'), `${p}/caption`, 2000) };
    return { path };
  });

  const citationsRaw = arr(value, 'citations', 1000);
  const citations = citationsRaw?.map((c, i): Citation => {
    const p = `/citations/${i}`;
    if (!isObject(c)) fail('metadata.type', '§4.4', 'Citation must be an object', { path: p });
    checkFields(c, ['target'], ['relations', 'reason'], '§4.4', p);
    const target = c.get('target');
    if (typeof target !== 'string') fail('metadata.type', '§4.4', 'target must be a string', { path: `${p}/target` });
    const parsed = parseCitationTarget(target);
    if (!parsed)
      fail('metadata.citation_target', '§4.4', 'Citation target uses none of the permitted forms', {
        path: `${p}/target`,
        observed: target,
      });
    let relations: CitationRelation[] | undefined;
    if (c.has('relations')) {
      const r = c.get('relations');
      if (!isArray(r)) fail('metadata.type', '§4.4', 'relations must be an array', { path: `${p}/relations` });
      const seen = new Set<string>();
      relations = r.map((x, j) => {
        check(
          typeof x === 'string' && (CITATION_RELATIONS as readonly string[]).includes(x),
          'metadata.citation_relation',
          '§4.4',
          'Unknown relation',
          {
            path: `${p}/relations/${j}`,
            observed: x,
          },
        );
        check(!seen.has(x), 'metadata.citation_relation_duplicate', '§4.4', 'Relations must be distinct', { path: `${p}/relations/${j}` });
        seen.add(x);
        return x as CitationRelation;
      });
    }
    const out: Citation = {
      target,
      targetKey: parsed.key,
      targetKind: parsed.kind,
      ...(relations !== undefined ? { relations } : {}),
      ...(c.has('reason') ? { reason: str(c.get('reason'), `${p}/reason`, 1000, true) } : {}),
    };
    return out;
  });

  return {
    title,
    abstract,
    ...(categories !== undefined ? { categories } : {}),
    ...(keywords !== undefined ? { keywords } : {}),
    ...(display !== undefined ? { display } : {}),
    ...(citations !== undefined ? { citations } : {}),
  };
}

/** Parse and validate sci.json bytes (§7 + §4). */
export function parseMetadata(bytes: Uint8Array, files: ReadonlyMap<string, ManifestEntry>, budget?: Partial<Budget>): Metadata {
  const b = resolveBudget(budget);
  if (bytes.length > b.maxMetadataBytes) {
    incomplete('budget.metadata_bytes', '§6', 'sci.json exceeds the local size budget', { path: 'sci.json', observed: bytes.length });
  }
  return validateMetadata(parseJsonBytes(bytes, { maxDepth: b.maxJsonDepth, path: 'sci.json' }), files);
}
