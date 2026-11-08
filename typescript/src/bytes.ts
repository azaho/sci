/** Byte helpers built only on Web-standard APIs. */
import { fail } from './errors.ts';

const HEX = '0123456789abcdef';

export function toHex(bytes: Uint8Array): string {
  let out = '';
  for (const b of bytes) out += (HEX[b >> 4] ?? '') + (HEX[b & 15] ?? '');
  return out;
}

/** Decode lowercase or uppercase hex. Returns null for malformed input. */
export function fromHex(text: string): Uint8Array | null {
  if (text.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(text)) return null;
  const out = new Uint8Array(text.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = Number.parseInt(text.slice(2 * i, 2 * i + 2), 16);
  return out;
}

export function equalBytes(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= (a[i] ?? 0) ^ (b[i] ?? 0);
  return diff === 0;
}

export function concatBytes(...parts: readonly Uint8Array[]): Uint8Array {
  let total = 0;
  for (const p of parts) total += p.length;
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}

export function asciiBytes(text: string): Uint8Array {
  const out = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c > 0x7f) throw new RangeError('non-ASCII text');
    out[i] = c;
  }
  return out;
}

export function utf8Encode(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

/**
 * Strict UTF-8 decoding: rejects malformed sequences, overlong forms, surrogates, and
 * values above U+10FFFF. A leading U+FEFF is preserved (not stripped).
 */
export function utf8DecodeStrict(bytes: Uint8Array, requirement: string, path?: string): string {
  try {
    return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch {
    return fail('text.invalid_utf8', requirement, 'Text is not valid UTF-8', { path });
  }
}

const B64URL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
const B64URL_INDEX: Record<string, number> = Object.fromEntries([...B64URL].map((c, i) => [c, i]));

/**
 * Strict unpadded base64url decoding (RFC 4648 §5, RFC 7515 Appendix C).
 * Returns null for any noncanonical spelling: padding, other alphabets, or nonzero pad bits.
 */
export function base64UrlDecodeStrict(text: string): Uint8Array | null {
  if (text.length % 4 === 1) return null;
  const out = new Uint8Array(Math.floor((text.length * 3) / 4));
  let acc = 0;
  let bits = 0;
  let o = 0;
  for (const c of text) {
    const v = B64URL_INDEX[c];
    if (v === undefined) return null;
    acc = (acc << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[o++] = (acc >> bits) & 0xff;
    }
  }
  if (bits > 0 && (acc & ((1 << bits) - 1)) !== 0) return null;
  return out.subarray(0, o);
}

export function base64UrlEncode(bytes: Uint8Array): string {
  let out = '';
  let i = 0;
  for (; i + 2 < bytes.length; i += 3) {
    const n = ((bytes[i] ?? 0) << 16) | ((bytes[i + 1] ?? 0) << 8) | (bytes[i + 2] ?? 0);
    out += (B64URL[(n >> 18) & 63] ?? '') + (B64URL[(n >> 12) & 63] ?? '') + (B64URL[(n >> 6) & 63] ?? '') + (B64URL[n & 63] ?? '');
  }
  const rest = bytes.length - i;
  if (rest === 1) {
    const n = (bytes[i] ?? 0) << 16;
    out += (B64URL[(n >> 18) & 63] ?? '') + (B64URL[(n >> 12) & 63] ?? '');
  } else if (rest === 2) {
    const n = ((bytes[i] ?? 0) << 16) | ((bytes[i + 1] ?? 0) << 8);
    out += (B64URL[(n >> 18) & 63] ?? '') + (B64URL[(n >> 12) & 63] ?? '') + (B64URL[(n >> 6) & 63] ?? '');
  }
  return out;
}

/** Standard base64 decoding (used for PEM bodies). */
export function base64DecodeStandard(text: string): Uint8Array {
  const clean = text.replace(/[\r\n\t ]/g, '');
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(clean) || clean.length % 4 !== 0) {
    throw new RangeError('invalid base64');
  }
  const bin = atob(clean);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Read a little-endian unsigned 16-bit integer. Caller must check bounds. */
export function u16le(b: Uint8Array, o: number): number {
  return (b[o] ?? 0) | ((b[o + 1] ?? 0) << 8);
}

/** Read a little-endian unsigned 32-bit integer. Caller must check bounds. */
export function u32le(b: Uint8Array, o: number): number {
  return ((b[o] ?? 0) | ((b[o + 1] ?? 0) << 8) | ((b[o + 2] ?? 0) << 16) | ((b[o + 3] ?? 0) << 24)) >>> 0;
}
