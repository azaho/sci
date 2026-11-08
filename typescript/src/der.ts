/**
 * Minimal strict DER reader (X.690) for RFC 3161 / CMS / X.509 processing.
 * Definite minimal lengths only; no indefinite lengths; bounded tag sizes.
 */
import { fail } from './errors.ts';
import { compareInstants, type Instant, parseRfc3339 } from './time.ts';

export interface Tlv {
  /** Identifier class: 0 universal, 1 application, 2 context-specific, 3 private. */
  readonly cls: number;
  readonly constructed: boolean;
  readonly tag: number;
  /** Offset of the identifier octet within the original buffer. */
  readonly start: number;
  /** Offset one past the last content octet. */
  readonly end: number;
  /** Full encoding (identifier + length + content). */
  readonly bytes: Uint8Array;
  /** Content octets. */
  readonly content: Uint8Array;
}

const REQ = 'X.690 DER';

/**
 * §6.8 framing: return the end offset of a complete DER value at `offset`, or null if the
 * remaining bytes do not begin with a complete, minimally encoded, definite-length value.
 * Never throws and never scans ahead.
 */
export function frameDer(data: Uint8Array, offset: number): number | null {
  let pos = offset;
  if (pos >= data.length) return null;
  const id = data[pos++] ?? 0;
  if ((id & 0x1f) === 0x1f) {
    let first = true;
    let count = 0;
    for (;;) {
      if (pos >= data.length) return null;
      const v = data[pos++] ?? 0;
      if (first && (v & 0x7f) === 0) return null;
      first = false;
      if (++count > 4) return null;
      if (!(v & 0x80)) break;
    }
  }
  if (pos >= data.length) return null;
  const l0 = data[pos++] ?? 0;
  let length: number;
  if (l0 < 0x80) length = l0;
  else {
    const n = l0 & 0x7f;
    if (n === 0 || n > 8 || pos + n > data.length || data[pos] === 0) return null;
    length = 0;
    for (let i = 0; i < n; i++) {
      length = length * 256 + (data[pos + i] ?? 0);
      if (length > data.length) return null; // cannot fit; avoids precision loss
    }
    pos += n;
    if (length < 0x80) return null;
  }
  if (length > data.length - pos) return null;
  return pos + length;
}

/** Parse one TLV at `offset` within `data`, throwing `invalid` on malformed encodings. */
export function readTlv(data: Uint8Array, offset: number, limit = data.length): Tlv {
  const end = frameDer(data.subarray(0, limit), offset);
  if (end === null) fail('der.malformed', REQ, 'Malformed or incomplete DER value', { path: `@${offset}` });
  let pos = offset;
  const id = data[pos++] ?? 0;
  let tag = id & 0x1f;
  if (tag === 0x1f) {
    tag = 0;
    for (;;) {
      const v = data[pos++] ?? 0;
      tag = tag * 128 + (v & 0x7f);
      if (!(v & 0x80)) break;
    }
    if (tag < 0x1f) fail('der.tag', REQ, 'High tag number form used for a low tag', { path: `@${offset}` });
  }
  const l0 = data[pos++] ?? 0;
  if (l0 >= 0x80) pos += l0 & 0x7f;
  return {
    cls: id >> 6,
    constructed: (id & 0x20) !== 0,
    tag,
    start: offset,
    end,
    bytes: data.subarray(offset, end),
    content: data.subarray(pos, end),
  };
}

/** Parse a buffer that must contain exactly one TLV. */
export function parseDer(data: Uint8Array): Tlv {
  const t = readTlv(data, 0);
  if (t.end !== data.length) fail('der.trailing', REQ, 'Trailing bytes after DER value');
  return t;
}

/** Children of a constructed TLV (content must be exactly a sequence of TLVs). */
export function children(t: Tlv): Tlv[] {
  if (!t.constructed) fail('der.primitive', REQ, 'Expected a constructed value');
  const out: Tlv[] = [];
  let pos = 0;
  while (pos < t.content.length) {
    const c = readTlv(t.content, pos);
    out.push(c);
    pos = c.end;
  }
  return out;
}

export const UNIVERSAL = {
  BOOLEAN: 1,
  INTEGER: 2,
  BIT_STRING: 3,
  OCTET_STRING: 4,
  NULL: 5,
  OID: 6,
  UTF8String: 12,
  SEQUENCE: 16,
  SET: 17,
  PrintableString: 19,
  TeletexString: 20,
  IA5String: 22,
  UTCTime: 23,
  GeneralizedTime: 24,
  UniversalString: 28,
  BMPString: 30,
} as const;

declare const matched: unique symbol;
/** A TLV that matched a tag test (nominal, so a failed test does not narrow to `never`). */
export type MatchedTlv = Tlv & { readonly [matched]: true };

export function isUniversal(t: Tlv | undefined, tag: number): t is MatchedTlv {
  return t !== undefined && t.cls === 0 && t.tag === tag;
}

export function isContext(t: Tlv | undefined, tag: number): t is MatchedTlv {
  return t !== undefined && t.cls === 2 && t.tag === tag;
}

export function expectUniversal(t: Tlv | undefined, tag: number, what: string): Tlv {
  if (!isUniversal(t, tag)) fail('der.unexpected', REQ, `Expected ${what}`);
  const constructed = tag === UNIVERSAL.SEQUENCE || tag === UNIVERSAL.SET;
  if (t.constructed !== constructed) fail('der.unexpected', REQ, `Wrong primitive/constructed form for ${what}`);
  return t;
}

export function sequence(t: Tlv | undefined, what: string): Tlv[] {
  return children(expectUniversal(t, UNIVERSAL.SEQUENCE, what));
}

export function setOf(t: Tlv | undefined, what: string): Tlv[] {
  return children(expectUniversal(t, UNIVERSAL.SET, what));
}

/** Read an INTEGER's content octets, checking minimal encoding. */
export function integerBytes(t: Tlv | undefined, what = 'INTEGER'): Uint8Array {
  const v = expectUniversal(t, UNIVERSAL.INTEGER, what).content;
  checkIntegerContent(v);
  return v;
}

function checkIntegerContent(v: Uint8Array): void {
  if (v.length === 0) fail('der.integer', REQ, 'Empty INTEGER');
  if (v.length > 1 && ((v[0] === 0 && ((v[1] ?? 0) & 0x80) === 0) || (v[0] === 0xff && ((v[1] ?? 0) & 0x80) !== 0))) {
    fail('der.integer', REQ, 'Non-minimal INTEGER encoding');
  }
}

/** Read a small nonnegative INTEGER (or implicitly tagged INTEGER content) as a number. */
export function smallInt(t: Tlv | undefined, what = 'INTEGER', implicit = false): number {
  if (!t) fail('der.unexpected', REQ, `Expected ${what}`);
  const v = implicit ? t.content : integerBytes(t, what);
  if (implicit) checkIntegerContent(v);
  if (((v[0] ?? 0) & 0x80) !== 0 || v.length > 6) fail('der.integer', REQ, `${what} out of range`);
  let n = 0;
  for (const b of v) n = n * 256 + b;
  return n;
}

export function oid(t: Tlv | undefined, what = 'OBJECT IDENTIFIER'): string {
  const v = expectUniversal(t, UNIVERSAL.OID, what).content;
  if (v.length === 0 || ((v[v.length - 1] ?? 0) & 0x80) !== 0) fail('der.oid', REQ, 'Malformed OID');
  const arcs: number[] = [];
  let acc = 0;
  let started = false;
  for (const b of v) {
    if (!started && b === 0x80) fail('der.oid', REQ, 'Non-minimal OID arc');
    started = true;
    acc = acc * 128 + (b & 0x7f);
    if (acc > Number.MAX_SAFE_INTEGER / 128) fail('der.oid', REQ, 'OID arc too large');
    if (!(b & 0x80)) {
      arcs.push(acc);
      acc = 0;
      started = false;
    }
  }
  const first = arcs[0] ?? 0;
  const head = first < 40 ? [0, first] : first < 80 ? [1, first - 40] : [2, first - 80];
  return [...head, ...arcs.slice(1)].join('.');
}

export function octetString(t: Tlv | undefined, what = 'OCTET STRING'): Uint8Array {
  return expectUniversal(t, UNIVERSAL.OCTET_STRING, what).content;
}

export function bitStringBytes(t: Tlv | undefined, what = 'BIT STRING'): Uint8Array {
  const v = expectUniversal(t, UNIVERSAL.BIT_STRING, what).content;
  if (v.length === 0 || v[0] !== 0) fail('der.bit_string', REQ, `${what} must have zero unused bits`);
  return v.subarray(1);
}

/** Bit string as a set of named-bit indexes (for keyUsage). */
export function bitStringBits(t: Tlv | undefined): Set<number> {
  const v = expectUniversal(t, UNIVERSAL.BIT_STRING, 'BIT STRING').content;
  if (v.length === 0 || (v[0] ?? 0) > 7) fail('der.bit_string', REQ, 'Malformed BIT STRING');
  const out = new Set<number>();
  for (let i = 1; i < v.length; i++) for (let b = 0; b < 8; b++) if (((v[i] ?? 0) >> (7 - b)) & 1) out.add((i - 1) * 8 + b);
  return out;
}

export function boolean(t: Tlv | undefined): boolean {
  const v = expectUniversal(t, UNIVERSAL.BOOLEAN, 'BOOLEAN').content;
  if (v.length !== 1 || (v[0] !== 0 && v[0] !== 0xff)) fail('der.boolean', REQ, 'Non-DER BOOLEAN');
  return v[0] === 0xff;
}

export interface DerTime {
  readonly instant: Instant;
  /** RFC 3339 UTC rendering, preserving any fractional seconds. */
  readonly text: string;
}

function ascii(v: Uint8Array): string {
  let s = '';
  for (const b of v) s += String.fromCharCode(b);
  return s;
}

/** Read UTCTime or GeneralizedTime in DER form (UTC `Z`, seconds present). */
export function time(t: Tlv | undefined, what = 'Time'): DerTime {
  if (t?.cls !== 0 || t.constructed) fail('der.time', REQ, `Expected ${what}`);
  const s = ascii(t.content);
  let year: number;
  let rest: string;
  if (t.tag === UNIVERSAL.UTCTime) {
    const m = /^(\d{2})(\d{10})Z$/.exec(s);
    if (!m) fail('der.time', REQ, 'Non-DER UTCTime');
    const yy = Number(m[1]);
    year = yy < 50 ? 2000 + yy : 1900 + yy;
    rest = m[2] ?? '';
  } else if (t.tag === UNIVERSAL.GeneralizedTime) {
    const m = /^(\d{4})(\d{10})(?:\.(\d*[1-9]))?Z$/.exec(s);
    if (!m) fail('der.time', REQ, 'Non-DER GeneralizedTime');
    year = Number(m[1]);
    rest = (m[2] ?? '') + (m[3] !== undefined ? `.${m[3]}` : '');
  } else {
    return fail('der.time', REQ, `Expected ${what}`);
  }
  const mo = rest.slice(0, 2);
  const d = rest.slice(2, 4);
  const h = rest.slice(4, 6);
  const mi = rest.slice(6, 8);
  const sec = rest.slice(8, 10);
  const frac = rest.slice(10);
  const text = `${String(year).padStart(4, '0')}-${mo}-${d}T${h}:${mi}:${sec}${frac}Z`;
  const instant = parseRfc3339Strict(text);
  return { instant, text };
}

function parseRfc3339Strict(text: string): Instant {
  const i = parseRfc3339(text);
  if (!i) fail('der.time', REQ, 'Invalid date/time value');
  return i;
}

export function instantLE(a: Instant, b: Instant): boolean {
  return compareInstants(a, b) <= 0;
}

export function instantLT(a: Instant, b: Instant): boolean {
  return compareInstants(a, b) < 0;
}

/** AlgorithmIdentifier. */
export interface AlgorithmId {
  readonly oid: string;
  /** DER of parameters, if present. */
  readonly params?: Tlv;
}

export function algorithmId(t: Tlv | undefined, what = 'AlgorithmIdentifier'): AlgorithmId {
  const parts = sequence(t, what);
  if (parts.length < 1 || parts.length > 2) fail('der.algorithm', REQ, `Malformed ${what}`);
  const id = oid(parts[0]);
  return parts[1] ? { oid: id, params: parts[1] } : { oid: id };
}

/** Decode an X.520 directory string value. */
export function directoryString(t: Tlv): string {
  const v = t.content;
  switch (t.tag) {
    case UNIVERSAL.UTF8String:
      try {
        return new TextDecoder('utf-8', { fatal: true }).decode(v);
      } catch {
        return '\uFFFD';
      }
    case UNIVERSAL.PrintableString:
    case UNIVERSAL.IA5String:
    case UNIVERSAL.TeletexString:
      return ascii(v);
    case UNIVERSAL.BMPString: {
      let s = '';
      for (let i = 0; i + 1 < v.length; i += 2) s += String.fromCharCode(((v[i] ?? 0) << 8) | (v[i + 1] ?? 0));
      return s;
    }
    case UNIVERSAL.UniversalString: {
      let s = '';
      for (let i = 0; i + 3 < v.length; i += 4) {
        const cp = (((v[i] ?? 0) << 24) | ((v[i + 1] ?? 0) << 16) | ((v[i + 2] ?? 0) << 8) | (v[i + 3] ?? 0)) >>> 0;
        s += cp <= 0x10ffff ? String.fromCodePoint(cp) : '\uFFFD';
      }
      return s;
    }
    default:
      return `#${Array.from(t.bytes, (b) => b.toString(16).padStart(2, '0')).join('')}`;
  }
}

const NAME_KEYS: Record<string, string> = {
  '2.5.4.3': 'CN',
  '2.5.4.6': 'C',
  '2.5.4.7': 'L',
  '2.5.4.8': 'ST',
  '2.5.4.10': 'O',
  '2.5.4.11': 'OU',
  '2.5.4.5': 'serialNumber',
  '1.2.840.113549.1.9.1': 'emailAddress',
  '0.9.2342.19200300.100.1.25': 'DC',
};

/** Render a Name (RDNSequence) in an RFC 4514-like string (most specific RDN first). */
export function renderName(nameDer: Uint8Array): string {
  try {
    const rdns = sequence(parseDer(nameDer), 'Name');
    const parts: string[] = [];
    for (const rdn of rdns) {
      const atvs = setOf(rdn, 'RDN').map((atv) => {
        const [type, value] = sequence(atv, 'AttributeTypeAndValue');
        const key = NAME_KEYS[oid(type)] ?? oid(type);
        const text = value ? directoryString(value) : '';
        return `${key}=${text.replace(/([,+"\\<>;])/g, '\\$1')}`;
      });
      parts.push(atvs.join('+'));
    }
    return parts.reverse().join(',');
  } catch {
    return '(unparseable name)';
  }
}
