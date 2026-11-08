/**
 * Citation targets (SCI §4.4) and the Sciscape `target_key` derivation.
 *
 * - `sci:sha256:<digest>` — matched by publication ID.
 * - `doi:<name>` — DOI URI; matched by DOI equivalence (percent-decoding, ASCII case-insensitive).
 * - `http(s)://…` — RFC 9110 §4.2 URI with optional fragment; matched by exact string.
 */
import { utf8Encode } from './bytes.ts';

export type CitationTargetKind = 'sci' | 'doi' | 'url';

export interface ParsedCitationTarget {
  readonly kind: CitationTargetKind;
  readonly target: string;
  /** Bare digest for `sci`, decoded DOI name for `doi`, the URL for `url`. */
  readonly value: string;
  /** Equivalence key: two targets refer to the same work under §4.4 iff their keys are equal. */
  readonly key: string;
}

const UNRESERVED = 'A-Za-z0-9\\-._~';
const SUB_DELIMS = "!$&'()*+,;=";
const PCT = '%[0-9A-Fa-f]{2}';
const PCHAR = `(?:[${UNRESERVED}${SUB_DELIMS}:@]|${PCT})`;
const REG_NAME = new RegExp(`^(?:[${UNRESERVED}${SUB_DELIMS}]|${PCT})+$`);
const PATH_ABEMPTY = new RegExp(`^(?:/${PCHAR}*)*$`);
const QUERY_OR_FRAGMENT = new RegExp(`^(?:${PCHAR}|[/?])*$`);
const DEC_OCTET = '(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])';
const IPV4 = new RegExp(`^${DEC_OCTET}\\.${DEC_OCTET}\\.${DEC_OCTET}\\.${DEC_OCTET}$`);
const IPVFUTURE = new RegExp(`^v[0-9A-Fa-f]+\\.[${UNRESERVED}${SUB_DELIMS}:]+$`);

function isIpv6(s: string): boolean {
  let body = s;
  let groupsNeeded = 8;
  const lastColon = body.lastIndexOf(':');
  if (lastColon >= 0 && body.slice(lastColon + 1).includes('.')) {
    if (!IPV4.test(body.slice(lastColon + 1))) return false;
    body = `${body.slice(0, lastColon + 1)}0`;
    groupsNeeded = 7;
  }
  const h16 = /^[0-9A-Fa-f]{1,4}$/;
  const double = body.indexOf('::');
  if (double !== body.lastIndexOf('::')) return false;
  if (double < 0) {
    const parts = body.split(':');
    return parts.length === groupsNeeded && parts.every((p) => h16.test(p));
  }
  const head = body.slice(0, double);
  const tail = body.slice(double + 2);
  const hp = head === '' ? [] : head.split(':');
  const tp = tail === '' ? [] : tail.split(':');
  if (![...hp, ...tp].every((p) => h16.test(p))) return false;
  return hp.length + tp.length <= groupsNeeded - 1;
}

/** Validate an HTTP(S) URI under RFC 9110 §4.2 (RFC 3986 syntax, nonempty host, no userinfo). */
export function isHttpUri(value: string): boolean {
  const m = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^/?#]*)([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/s.exec(value);
  if (!m) return false;
  const scheme = (m[1] ?? '').toLowerCase();
  if (scheme !== 'http' && scheme !== 'https') return false;
  const authority = m[2] ?? '';
  const path = m[3] ?? '';
  const query = m[4];
  const fragment = m[5];
  // RFC 9110 §4.2.4: userinfo is deprecated; senders MUST NOT generate it.
  if (authority.includes('@')) return false;
  let host = authority;
  let port: string | undefined;
  if (authority.startsWith('[')) {
    const close = authority.indexOf(']');
    if (close < 0) return false;
    host = authority.slice(0, close + 1);
    const rest = authority.slice(close + 1);
    if (rest !== '') {
      if (!rest.startsWith(':')) return false;
      port = rest.slice(1);
    }
    const inner = host.slice(1, -1);
    if (!isIpv6(inner) && !IPVFUTURE.test(inner)) return false;
  } else {
    const colon = authority.lastIndexOf(':');
    if (colon >= 0) {
      host = authority.slice(0, colon);
      port = authority.slice(colon + 1);
    }
    if (host === '' || !REG_NAME.test(host)) return false; // RFC 9110: host must be nonempty
  }
  if (port !== undefined && !/^[0-9]*$/.test(port)) return false;
  if (!PATH_ABEMPTY.test(path)) return false;
  if (query !== undefined && !QUERY_OR_FRAGMENT.test(query)) return false;
  if (fragment !== undefined && !QUERY_OR_FRAGMENT.test(fragment)) return false;
  return true;
}

/** Validate an absolute URI (any scheme) under RFC 3986 §4.3, used for descriptor URLs. */
export function isAbsoluteHttpUrl(value: string): boolean {
  return isHttpUri(value) && !value.includes('#');
}

function percentDecodeUtf8(s: string): string | null {
  const bytes: number[] = [];
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0x25) {
      const h = s.slice(i + 1, i + 3);
      if (!/^[0-9A-Fa-f]{2}$/.test(h)) return null;
      bytes.push(Number.parseInt(h, 16));
      i += 2;
    } else if (c < 0x80) {
      bytes.push(c);
    } else {
      return null;
    }
  }
  try {
    return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(new Uint8Array(bytes));
  } catch {
    return null;
  }
}

const DOI_KEEP = new RegExp(`^[${UNRESERVED}${SUB_DELIMS}:@/]$`);

function encodeDoiName(name: string): string {
  let out = '';
  for (const ch of name) {
    if (DOI_KEEP.test(ch)) out += ch;
    else for (const b of utf8Encode(ch)) out += `%${b.toString(16).toUpperCase().padStart(2, '0')}`;
  }
  return out;
}

/**
 * Parse a `doi:` URI. The encoded name must use RFC 3986 path characters (`?` and `#` must be
 * percent-encoded), decode to UTF-8, and have the form `10.<registrant>/<suffix>`.
 * Returns the decoded DOI name, or null.
 */
export function parseDoiUri(value: string): string | null {
  if (!value.startsWith('doi:')) return null;
  const encoded = value.slice(4);
  if (!new RegExp(`^(?:${PCHAR}|/)+$`).test(encoded)) return null;
  const name = percentDecodeUtf8(encoded);
  if (name === null) return null;
  const m = /^10\.[0-9]+(?:\.[0-9]+)*\/(.+)$/s.exec(name);
  if (!m) return null;
  // Reject controls and whitespace in the decoded name.
  for (const ch of name) {
    const cp = ch.codePointAt(0) ?? 0;
    if (cp <= 0x20 || (cp >= 0x7f && cp <= 0x9f) || /\s/u.test(ch)) return null;
  }
  return name;
}

/** Canonical DOI key: ASCII-lowercased decoded name, re-encoded with uppercase percent escapes. */
export function doiKey(name: string): string {
  return `doi:${encodeDoiName(name.replace(/[A-Z]/g, (c) => c.toLowerCase()))}`;
}

/** Parse a citation target under §4.4. Returns null if the target uses none of the exact forms. */
export function parseCitationTarget(target: string): ParsedCitationTarget | null {
  const sci = /^sci:sha256:([0-9a-f]{64})$/.exec(target);
  if (sci) return { kind: 'sci', target, value: sci[1] ?? '', key: target };
  if (target.startsWith('doi:')) {
    const name = parseDoiUri(target);
    return name === null ? null : { kind: 'doi', target, value: name, key: doiKey(name) };
  }
  if (isHttpUri(target)) return { kind: 'url', target, value: target, key: target };
  return null;
}

/**
 * Derive the citation `target_key` used for indexing and lookup (Sciscape DESIGN, catalog citations):
 * SCI targets by ID, DOIs by DOI equivalence, URLs by exact string. Returns null for invalid targets.
 */
export function citationTargetKey(target: string): string | null {
  return parseCitationTarget(target)?.key ?? null;
}
