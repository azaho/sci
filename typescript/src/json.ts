/**
 * Strict JSON parser for SCI §7.
 *
 * - RFC 8259 grammar only (no comments, trailing commas, NaN, leading `+`, etc.).
 * - UTF-8 without a byte-order mark; malformed UTF-8 is rejected.
 * - Duplicate object keys are rejected after escape decoding, at every depth.
 * - Unpaired UTF-16 surrogate escapes are rejected.
 * - Number tokens are preserved verbatim so integer spelling rules can be enforced.
 *
 * `JSON.parse` is insufficient: it accepts duplicate keys and lone surrogate escapes.
 */
import { utf8DecodeStrict } from './bytes.ts';
import { fail, incomplete } from './errors.ts';

/** A JSON number with its exact source token. */
export class JsonNumber {
  readonly raw: string;
  readonly value: number;
  constructor(raw: string) {
    this.raw = raw;
    this.value = Number(raw);
  }
  /** True if the token is a plain nonnegative decimal integer without leading zeros. */
  isCanonicalNonnegativeInteger(): boolean {
    return /^(0|[1-9][0-9]*)$/.test(this.raw);
  }
}

export type JsonObject = Map<string, JsonValue>;
export type JsonValue = null | boolean | string | JsonNumber | JsonValue[] | JsonObject;

export interface JsonParseOptions {
  /** Maximum nesting depth; exceeding it is a local limit (`incomplete`). Default 128. */
  readonly maxDepth?: number;
  /** Requirement label for error records. Default `§7`. */
  readonly requirement?: string;
  /** Path label for error records (e.g. the file name). */
  readonly path?: string;
}

const REQ = '§7';

/** Parse JSON bytes under SCI §7. Throws {@link SciError} on failure. */
export function parseJsonBytes(bytes: Uint8Array, options: JsonParseOptions = {}): JsonValue {
  const path = options.path;
  if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    fail('json.bom', REQ, 'JSON must not begin with a UTF-8 byte-order mark', { path });
  }
  const text = utf8DecodeStrict(bytes, REQ, path);
  return parseJsonText(text, options);
}

/** Parse already-decoded JSON text under SCI §7 rules (except the BOM byte check). */
export function parseJsonText(text: string, options: JsonParseOptions = {}): JsonValue {
  const parser = new Parser(text, options.maxDepth ?? 128, options.path);
  return parser.parseDocument();
}

class Parser {
  readonly #s: string;
  #i = 0;
  readonly #maxDepth: number;
  readonly #path: string | undefined;

  constructor(s: string, maxDepth: number, path: string | undefined) {
    this.#s = s;
    this.#maxDepth = maxDepth;
    this.#path = path;
  }

  #fail(code: string, message: string): never {
    return fail(code, REQ, `${message} at character ${this.#i}`, { path: this.#path });
  }

  parseDocument(): JsonValue {
    this.#ws();
    const value = this.#value(0);
    this.#ws();
    if (this.#i !== this.#s.length) this.#fail('json.trailing_data', 'Unexpected data after JSON value');
    return value;
  }

  #ws(): void {
    const s = this.#s;
    while (this.#i < s.length) {
      const c = s.charCodeAt(this.#i);
      if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) this.#i++;
      else break;
    }
  }

  #value(depth: number): JsonValue {
    const s = this.#s;
    if (this.#i >= s.length) this.#fail('json.unexpected_end', 'Unexpected end of JSON');
    const c = s[this.#i];
    switch (c) {
      case '{':
        return this.#object(depth + 1);
      case '[':
        return this.#array(depth + 1);
      case '"':
        return this.#string();
      case 't':
        return this.#literal('true', true);
      case 'f':
        return this.#literal('false', false);
      case 'n':
        return this.#literal('null', null);
      default:
        if (c === '-' || (c !== undefined && c >= '0' && c <= '9')) return this.#number();
        return this.#fail('json.syntax', 'Unexpected character');
    }
  }

  #literal<T>(word: string, value: T): T {
    if (this.#s.startsWith(word, this.#i)) {
      this.#i += word.length;
      return value;
    }
    return this.#fail('json.syntax', 'Invalid literal');
  }

  #enter(depth: number): void {
    if (depth > this.#maxDepth) {
      incomplete('budget.json_depth', '§6', `JSON nesting exceeds local limit of ${this.#maxDepth}`, {
        path: this.#path,
      });
    }
  }

  #object(depth: number): JsonObject {
    this.#enter(depth);
    const s = this.#s;
    this.#i++; // {
    const out: JsonObject = new Map();
    this.#ws();
    if (s[this.#i] === '}') {
      this.#i++;
      return out;
    }
    for (;;) {
      this.#ws();
      if (s[this.#i] !== '"') this.#fail('json.syntax', 'Expected object key');
      const key = this.#string();
      if (out.has(key)) {
        fail('json.duplicate_key', REQ, 'Duplicate object key after escape decoding', {
          path: this.#path,
          observed: key,
        });
      }
      this.#ws();
      if (s[this.#i] !== ':') this.#fail('json.syntax', 'Expected ":"');
      this.#i++;
      this.#ws();
      out.set(key, this.#value(depth));
      this.#ws();
      const c = s[this.#i];
      if (c === ',') {
        this.#i++;
        continue;
      }
      if (c === '}') {
        this.#i++;
        return out;
      }
      this.#fail('json.syntax', 'Expected "," or "}"');
    }
  }

  #array(depth: number): JsonValue[] {
    this.#enter(depth);
    const s = this.#s;
    this.#i++; // [
    const out: JsonValue[] = [];
    this.#ws();
    if (s[this.#i] === ']') {
      this.#i++;
      return out;
    }
    for (;;) {
      this.#ws();
      out.push(this.#value(depth));
      this.#ws();
      const c = s[this.#i];
      if (c === ',') {
        this.#i++;
        continue;
      }
      if (c === ']') {
        this.#i++;
        return out;
      }
      this.#fail('json.syntax', 'Expected "," or "]"');
    }
  }

  #hex4(): number {
    const h = this.#s.slice(this.#i, this.#i + 4);
    if (!/^[0-9a-fA-F]{4}$/.test(h)) this.#fail('json.bad_escape', 'Invalid \\u escape');
    this.#i += 4;
    return Number.parseInt(h, 16);
  }

  #string(): string {
    const s = this.#s;
    this.#i++; // opening quote
    let out = '';
    let runStart = this.#i;
    for (;;) {
      if (this.#i >= s.length) this.#fail('json.unterminated_string', 'Unterminated string');
      const c = s.charCodeAt(this.#i);
      if (c === 0x22) {
        out += s.slice(runStart, this.#i);
        this.#i++;
        return out;
      }
      if (c < 0x20) this.#fail('json.control_character', 'Unescaped control character in string');
      if (c !== 0x5c) {
        this.#i++;
        continue;
      }
      out += s.slice(runStart, this.#i);
      this.#i++; // backslash
      const e = s[this.#i];
      this.#i++;
      switch (e) {
        case '"':
          out += '"';
          break;
        case '\\':
          out += '\\';
          break;
        case '/':
          out += '/';
          break;
        case 'b':
          out += '\b';
          break;
        case 'f':
          out += '\f';
          break;
        case 'n':
          out += '\n';
          break;
        case 'r':
          out += '\r';
          break;
        case 't':
          out += '\t';
          break;
        case 'u': {
          const u = this.#hex4();
          if (u >= 0xdc00 && u <= 0xdfff) {
            fail('json.unpaired_surrogate', REQ, 'Unpaired low surrogate escape', { path: this.#path });
          }
          if (u >= 0xd800 && u <= 0xdbff) {
            if (s[this.#i] !== '\\' || s[this.#i + 1] !== 'u') {
              fail('json.unpaired_surrogate', REQ, 'Unpaired high surrogate escape', { path: this.#path });
            }
            this.#i += 2;
            const low = this.#hex4();
            if (low < 0xdc00 || low > 0xdfff) {
              fail('json.unpaired_surrogate', REQ, 'High surrogate escape not followed by a low surrogate', {
                path: this.#path,
              });
            }
            out += String.fromCharCode(u, low);
          } else {
            out += String.fromCharCode(u);
          }
          break;
        }
        default:
          this.#i--;
          this.#fail('json.bad_escape', 'Invalid escape sequence');
      }
      runStart = this.#i;
    }
  }

  #number(): JsonNumber {
    const s = this.#s;
    const start = this.#i;
    const re = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;
    re.lastIndex = start;
    const m = re.exec(s);
    if (!m || m[0].length === 0 || m[0] === '-') this.#fail('json.bad_number', 'Invalid number');
    this.#i = start + m[0].length;
    // A digit directly after a complete number (e.g. "01") is a grammar error.
    const next = s[this.#i];
    if (next !== undefined && /[0-9.eE+-]/.test(next)) this.#fail('json.bad_number', 'Invalid number');
    return new JsonNumber(m[0]);
  }
}

/** Type guards and helpers for working with parsed values. */
export function isObject(v: JsonValue | undefined): v is JsonObject {
  return v instanceof Map;
}

export function isArray(v: JsonValue | undefined): v is JsonValue[] {
  return Array.isArray(v);
}

/** Convert a parsed value to plain JavaScript values (numbers become `number`). */
export function toPlain(v: JsonValue): unknown {
  if (v instanceof JsonNumber) return v.value;
  if (Array.isArray(v)) return v.map(toPlain);
  if (v instanceof Map) {
    const out: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
    for (const [k, x] of v) out[k] = toPlain(x);
    return out;
  }
  return v;
}
