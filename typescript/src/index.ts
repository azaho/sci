/** SCI 0.1 byte-oriented verifier, shared by browsers and services. */
import { ed25519 } from "@noble/curves/ed25519";
import { sha256 } from "@noble/hashes/sha256";

export class Invalid extends Error {}
export class Unsupported extends Error {}
export class Incomplete extends Error {}
export type ObjectValue = Record<string, any>;
export interface FileEntry {
  sha256: string;
  bytes: number;
}
export interface PublicKey {
  kty: "OKP";
  crv: "Ed25519";
  x: string;
}
export interface Manifest {
  schema: "sci-publication/0.1";
  public_key: PublicKey;
  files: Record<string, FileEntry>;
}
export interface Publication {
  id: string;
  manifest: Manifest;
  metadata: ObjectValue;
  entries: Record<string, Uint8Array>;
  baseline: boolean;
}
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
export const bytes = (s: string): Uint8Array => encoder.encode(s);
export const hex = (b: Uint8Array): string =>
  Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
export const hash = (b: Uint8Array): string => hex(sha256(b));
export function requireValue(ok: unknown, message: string): asserts ok {
  if (!ok) throw new Invalid(message);
}
export function decode(b: Uint8Array): string {
  try {
    return decoder.decode(b);
  } catch {
    throw new Invalid("invalid UTF-8");
  }
}
const full = (pattern: RegExp, s: unknown): s is string =>
  typeof s === "string" && pattern.exec(s)?.[0] === s;

class NumberToken {
  constructor(readonly raw: string) {}
  valueOf(): number {
    return Number(this.raw);
  }
}
/** Recursive descent preserves integer spelling and rejects duplicates before object construction. */
export function parseJSON(raw: Uint8Array): any {
  if (raw.length > 16 * 1024 * 1024) throw new Incomplete("JSON byte budget");
  const s = decode(raw);
  let i = 0;
  const ws = (): void => {
    while (/[ \n\r\t]/.test(s[i] ?? "\0")) i++;
  };
  function string(): string {
    const start = i++;
    let escape = false;
    while (i < s.length) {
      const c = s[i++];
      if (c === '"' && !escape) {
        let value: string;
        try {
          value = JSON.parse(s.slice(start, i)) as string;
        } catch {
          throw new Invalid("JSON string");
        }
        for (const ch of value) {
          const n = ch.codePointAt(0)!;
          requireValue(n < 0xd800 || n > 0xdfff, "unpaired surrogate");
        }
        return value;
      }
      if (c === "\\" && !escape) escape = true;
      else escape = false;
    }
    throw new Invalid("unterminated JSON string");
  }
  function value(depth: number): any {
    if (depth > 100) throw new Incomplete("JSON depth budget");
    ws();
    const c = s[i];
    if (c === '"') return string();
    if (c === "{") {
      i++;
      ws();
      const result: ObjectValue = Object.create(null);
      if (s[i] === "}") {
        i++;
        return result;
      }
      for (;;) {
        ws();
        requireValue(s[i] === '"', "object key");
        const key = string();
        requireValue(!Object.hasOwn(result, key), "duplicate JSON key");
        ws();
        requireValue(s[i++] === ":", "colon");
        result[key] = value(depth + 1);
        ws();
        const next = s[i++];
        if (next === "}") return result;
        requireValue(next === ",", "object delimiter");
      }
    }
    if (c === "[") {
      i++;
      ws();
      const result: any[] = [];
      if (s[i] === "]") {
        i++;
        return result;
      }
      for (;;) {
        result.push(value(depth + 1));
        ws();
        const next = s[i++];
        if (next === "]") return result;
        requireValue(next === ",", "array delimiter");
      }
    }
    for (const [literal, result] of [
      ["true", true],
      ["false", false],
      ["null", null],
    ] as const) {
      if (s.startsWith(literal, i)) {
        i += literal.length;
        return result;
      }
    }
    const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(
      s.slice(i),
    );
    requireValue(match, "JSON value");
    i += match[0].length;
    return new NumberToken(match[0]);
  }
  const result = value(0);
  ws();
  requireValue(i === s.length, "trailing JSON bytes");
  return result;
}
export function keys(
  o: any,
  required: string[],
  optional: string[] = [],
): asserts o is ObjectValue {
  requireValue(
    o !== null &&
      typeof o === "object" &&
      !Array.isArray(o) &&
      !(o instanceof NumberToken),
    "object required",
  );
  requireValue(
    required.every((k) => Object.hasOwn(o, k)) &&
      Object.keys(o).every((k) => [...required, ...optional].includes(k)),
    "record fields",
  );
}
export function schema(o: any, expected: string): void {
  requireValue(
    o !== null && typeof o === "object" && typeof o.schema === "string",
    "schema required",
  );
  if (o.schema !== expected) {
    if (
      [
        "sci-publication/0.1",
        "sci-key-claim/0.1",
        "sci-key-revocation/0.1",
        "sci-index/0.1",
        "sci-archive/0.1",
      ].includes(o.schema)
    )
      throw new Invalid("wrong record type");
    throw new Unsupported(o.schema);
  }
}
export function publicKey(o: any): Uint8Array {
  keys(o, ["kty", "crv", "x"]);
  requireValue(
    o.kty === "OKP" && o.crv === "Ed25519" && full(/^[A-Za-z0-9_-]{43}$/, o.x),
    "public JWK",
  );
  const raw = Uint8Array.from(
    atob(o.x.replaceAll("-", "+").replaceAll("_", "/") + "="),
    (c) => c.charCodeAt(0),
  );
  const encoded = btoa(String.fromCharCode(...raw))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
  requireValue(encoded === o.x, "noncanonical base64url");
  return raw;
}
export function signature(
  jwk: any,
  sig: Uint8Array,
  message: Uint8Array,
): void {
  const key = publicKey(jwk);
  requireValue(sig.length === 64, "signature length");
  try {
    for (const p of [key, sig.slice(0, 32)]) {
      const point = ed25519.ExtendedPoint.fromHex(p, false);
      requireValue(
        !point.isSmallOrder() && point.isTorsionFree(),
        "non-prime-order point",
      );
    }
    requireValue(
      ed25519.verify(sig, message, key, { zip215: false }),
      "signature",
    );
  } catch (e) {
    throw new Invalid("Ed25519: " + String(e));
  }
}
export function paths(names: string[]): void {
  const files = new Set<string>(),
    dirs = new Map<string, string>();
  for (const name of names) {
    requireValue(
      name.length <= 255 && full(/^[A-Za-z0-9._/-]+$/, name),
      "portable path",
    );
    const components = name.split("/");
    for (const c of components)
      requireValue(
        c &&
          c !== "." &&
          c !== ".." &&
          !c.endsWith(".") &&
          !/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(c),
        "path component",
      );
    requireValue(!files.has(name.toLowerCase()), "file case conflict");
    files.add(name.toLowerCase());
    for (let n = 1; n < components.length; n++) {
      const d = components.slice(0, n).join("/"),
        lower = d.toLowerCase();
      requireValue(
        !dirs.has(lower) || dirs.get(lower) === d,
        "directory capitalization",
      );
      dirs.set(lower, d);
    }
  }
  requireValue(
    [...dirs.keys()].every((d) => !files.has(d)),
    "file/directory conflict",
  );
}
export function manifest(
  raw: Uint8Array,
  sig: Uint8Array,
  expected?: string,
): Manifest {
  const o = parseJSON(raw);
  schema(o, "sci-publication/0.1");
  keys(o, ["schema", "public_key", "files"]);
  requireValue(
    o.files !== null && typeof o.files === "object" && !Array.isArray(o.files),
    "files map",
  );
  const names = Object.keys(o.files);
  paths(names);
  requireValue(
    names.includes("sci.json") && names.includes("README.md"),
    "required files",
  );
  for (const p of names) {
    requireValue(
      ["sci.json", "README.md", "LICENSE"].includes(p) ||
        p.startsWith("files/"),
      "allowed file path",
    );
    const e = o.files[p];
    keys(e, ["sha256", "bytes"]);
    requireValue(full(/^[0-9a-f]{64}$/, e.sha256), "file hash");
    requireValue(
      e.bytes instanceof NumberToken &&
        /^(0|[1-9][0-9]*)$/.test(e.bytes.raw) &&
        Number.isSafeInteger(Number(e.bytes.raw)),
      "file byte count",
    );
    e.bytes = Number(e.bytes.raw);
  }
  if (expected !== undefined)
    requireValue(
      hash(raw) === expected.replace(/^sci:/, "").replace(/^sha256:/, ""),
      "expected publication ID",
    );
  signature(o.public_key, sig, raw);
  return o as Manifest;
}
export function text(
  value: any,
  max = Infinity,
  multiline = false,
  nonblank = true,
): void {
  requireValue(typeof value === "string", "text required");
  requireValue([...value].length <= max, "text length");
  for (const ch of value) {
    const n = ch.codePointAt(0)!;
    const allowed =
      n === 9 ||
      n === 10 ||
      n === 13 ||
      (n >= 0x20 && n <= 0x7e) ||
      (n >= 0xa0 && n <= 0xd7ff) ||
      (n >= 0xe000 && n <= 0xfdcf) ||
      (n >= 0xfdf0 && n <= 0xfffd) ||
      (n >= 0x10000 && (n & 0xffff) <= 0xfffd);
    requireValue(
      allowed && (multiline || !"\t\r\n".includes(ch)),
      "text character",
    );
  }
  requireValue(!nonblank || /[^ \t\r\n]/.test(value), "blank text");
}
export function citationKey(target: string): string {
  return target.startsWith("doi:")
    ? "doi:" + decodeURIComponent(target.slice(4)).toLowerCase()
    : target;
}
export function metadata(
  raw: Uint8Array,
  files: Record<string, FileEntry>,
): ObjectValue {
  const o = parseJSON(raw);
  keys(
    o,
    ["title", "abstract"],
    ["categories", "keywords", "display", "citations"],
  );
  text(o.title, 200);
  text(o.abstract, 2500, true);
  const checkArray = (v: any, max: number): any[] => {
    requireValue(Array.isArray(v) && v.length <= max, "array limit");
    return v;
  };
  const cats = new Set<string>();
  for (const c of checkArray(o.categories ?? [], 3)) {
    keys(c, ["scheme", "code"]);
    requireValue(full(/^[a-z][a-z0-9.-]{0,63}$/, c.scheme), "scheme");
    text(c.code, 128);
    const key = JSON.stringify([c.scheme, c.code]);
    requireValue(!cats.has(key), "duplicate category");
    cats.add(key);
  }
  for (const k of checkArray(o.keywords ?? [], 20)) text(k, 64);
  const displays = new Set<string>();
  for (const d of checkArray(o.display ?? [], 4096)) {
    keys(d, ["path"], ["caption"]);
    requireValue(
      typeof d.path === "string" &&
        d.path.startsWith("files/") &&
        Object.hasOwn(files, d.path) &&
        !displays.has(d.path),
      "display path",
    );
    displays.add(d.path);
    if ("caption" in d) text(d.caption, 2000);
  }
  for (const c of checkArray(o.citations ?? [], 1000)) {
    keys(c, ["target"], ["relations", "reason"]);
    text(c.target);
    requireValue(
      /^sci:sha256:[0-9a-f]{64}$/.test(c.target) ||
        /^doi:10\.[0-9]{4,}(\.[0-9]+)*\/\S+$/.test(c.target) ||
        /^https?:\/\/[^\s/?#]+[^\s#]*(#[^\s#]*)?$/.test(c.target),
      "citation target",
    );
    const relations = checkArray(c.relations ?? [], 5);
    requireValue(
      relations.every((r) =>
        ["uses", "reviews", "supports", "challenges", "supersedes"].includes(r),
      ) && new Set(relations).size === relations.length,
      "relations",
    );
    if ("reason" in c) text(c.reason, 1000, true);
  }
  return o;
}
const crcTable = Uint32Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let i = 0; i < 8; i++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
function crc32(data: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < data.length; i++)
    c = crcTable[(c ^ data[i]) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, b) => n + b.length, 0));
  let i = 0;
  for (const p of parts) {
    out.set(p, i);
    i += p.length;
  }
  return out;
}
export function zip(entries: Record<string, Uint8Array>): Uint8Array {
  const names = [
    "manifest.json",
    "manifest.json.sig",
    ...Object.keys(entries)
      .filter((p) => p !== "manifest.json" && p !== "manifest.json.sig")
      .sort(),
  ];
  requireValue(names.length < 65535, "classic ZIP entry count");
  const local: Uint8Array[] = [],
    central: Uint8Array[] = [];
  let offset = 0;
  for (const name of names) {
    const n = bytes(name),
      data = entries[name];
    requireValue(data, "missing ZIP entry");
    const crc = crc32(data);
    const l = new Uint8Array(30),
      lv = new DataView(l.buffer);
    lv.setUint32(0, 0x04034b50, true);
    lv.setUint16(4, 10, true);
    lv.setUint16(12, 0x21, true);
    lv.setUint32(14, crc, true);
    lv.setUint32(18, data.length, true);
    lv.setUint32(22, data.length, true);
    lv.setUint16(26, n.length, true);
    const c = new Uint8Array(46),
      cv = new DataView(c.buffer);
    cv.setUint32(0, 0x02014b50, true);
    cv.setUint16(4, 10, true);
    cv.setUint16(6, 10, true);
    cv.setUint16(14, 0x21, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, data.length, true);
    cv.setUint32(24, data.length, true);
    cv.setUint16(28, n.length, true);
    cv.setUint32(42, offset, true);
    local.push(l, n, data);
    central.push(c, n);
    offset += 30 + n.length + data.length;
  }
  const cd = concat(central),
    end = new Uint8Array(22),
    ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, names.length, true);
  ev.setUint16(10, names.length, true);
  ev.setUint32(12, cd.length, true);
  ev.setUint32(16, offset, true);
  return concat([...local, cd, end]);
}
export function unzip(
  raw: Uint8Array,
  budget = 128 * 1024 * 1024,
): Record<string, Uint8Array> {
  if (raw.length > budget) throw new Incomplete("ZIP byte budget");
  const entries: Record<string, Uint8Array> = Object.create(null),
    order: string[] = [],
    offsets: number[] = [],
    crcs: number[] = [];
  let pos = 0;
  const v = new DataView(raw.buffer, raw.byteOffset, raw.length);
  const u16 = (p: number) => v.getUint16(p, true),
    u32 = (p: number) => v.getUint32(p, true);
  while (pos + 4 <= raw.length && u32(pos) === 0x04034b50) {
    requireValue(pos + 30 <= raw.length, "truncated local header");
    const size = u32(pos + 18),
      nl = u16(pos + 26),
      start = pos + 30 + nl;
    requireValue(
      size < 0xffffffff &&
        u32(pos + 22) === size &&
        u16(pos + 4) === 10 &&
        u16(pos + 6) === 0 &&
        u16(pos + 8) === 0 &&
        u16(pos + 10) === 0 &&
        u16(pos + 12) === 0x21 &&
        u16(pos + 28) === 0,
      "local header",
    );
    requireValue(
      start <= raw.length && size <= raw.length - start,
      "ZIP bounds",
    );
    const nameBytes = raw.subarray(pos + 30, start);
    requireValue(
      nameBytes.every((b) => b < 128),
      "ASCII ZIP path",
    );
    const name = decode(nameBytes);
    requireValue(!Object.hasOwn(entries, name), "duplicate ZIP entry");
    const data = raw.subarray(start, start + size),
      crc = crc32(data);
    requireValue(u32(pos + 14) === crc, "CRC");
    entries[name] = data;
    order.push(name);
    offsets.push(pos);
    crcs.push(crc);
    pos = start + size;
    if (order.length > 4096) throw new Incomplete("ZIP entry budget");
  }
  requireValue(
    order[0] === "manifest.json" && order[1] === "manifest.json.sig",
    "first entries",
  );
  const m = manifest(entries["manifest.json"], entries["manifest.json.sig"]);
  const names = [
    "manifest.json",
    "manifest.json.sig",
    ...Object.keys(m.files).sort(),
  ];
  requireValue(
    names.length === order.length && names.every((p, i) => order[i] === p),
    "ZIP order/inventory",
  );
  const centralStart = pos;
  for (let i = 0; i < names.length; i++) {
    requireValue(pos + 46 <= raw.length, "central header bounds");
    const nl = u16(pos + 28),
      size = entries[names[i]].length;
    requireValue(
      u32(pos) === 0x02014b50 &&
        u16(pos + 4) === 10 &&
        u16(pos + 6) === 10 &&
        u16(pos + 8) === 0 &&
        u16(pos + 10) === 0 &&
        u16(pos + 12) === 0 &&
        u16(pos + 14) === 0x21 &&
        u32(pos + 16) === crcs[i] &&
        u32(pos + 20) === size &&
        u32(pos + 24) === size &&
        u16(pos + 30) === 0 &&
        u16(pos + 32) === 0 &&
        u16(pos + 34) === 0 &&
        u16(pos + 36) === 0 &&
        u32(pos + 38) === 0 &&
        u32(pos + 42) === offsets[i],
      "central header",
    );
    requireValue(
      pos + 46 + nl <= raw.length &&
        decode(raw.subarray(pos + 46, pos + 46 + nl)) === names[i],
      "central filename",
    );
    pos += 46 + nl;
  }
  requireValue(
    pos + 22 === raw.length &&
      u32(pos) === 0x06054b50 &&
      u16(pos + 4) === 0 &&
      u16(pos + 6) === 0 &&
      u16(pos + 8) === names.length &&
      u16(pos + 10) === names.length &&
      u32(pos + 12) === pos - centralStart &&
      u32(pos + 16) === centralStart &&
      u16(pos + 20) === 0,
    "ZIP end record",
  );
  return entries;
}

export function zipSize(raw: Uint8Array, m: Manifest): number {
  return (
    22 +
    raw.length +
    102 +
    64 +
    110 +
    Object.entries(m.files).reduce(
      (n, [p, e]) => n + e.bytes + 76 + 2 * p.length,
      0,
    )
  );
}
export function verify(
  entries: Record<string, Uint8Array>,
  expected?: string,
): Publication {
  if (
    Object.values(entries).reduce((n, b) => n + b.length, 0) >
      128 * 1024 * 1024 ||
    Object.keys(entries).length > 4096
  )
    throw new Incomplete("bundle budget");
  requireValue(
    entries["manifest.json"] && entries["manifest.json.sig"],
    "signed manifest required",
  );
  const raw = entries["manifest.json"],
    m = manifest(raw, entries["manifest.json.sig"], expected);
  const names = Object.keys(entries).filter(
    (p) =>
      !["manifest.json", "manifest.json.sig", "manifest.json.sig.tsr"].includes(
        p,
      ),
  );
  requireValue(
    names.length === Object.keys(m.files).length &&
      names.every((p) => Object.hasOwn(m.files, p)),
    "bundle inventory",
  );
  for (const [p, e] of Object.entries(m.files)) {
    const data = entries[p];
    requireValue(
      data && data.length === e.bytes && hash(data) === e.sha256,
      "file mismatch: " + p,
    );
  }
  const meta = metadata(entries["sci.json"], m.files);
  text(decode(entries["README.md"]), Infinity, true);
  if (entries.LICENSE) text(decode(entries.LICENSE), Infinity, true, false);
  const baseline =
    m.files.LICENSE?.sha256 ===
      "8ccfd5fdaa93e5e71dc8812bf11e0a7198bd0a534fa64b8970fd2de58e6c2478" &&
    zipSize(raw, m) <= 67108864 &&
    Object.keys(m.files).length <= 1000 &&
    (meta.display ?? []).length <= 3 &&
    raw.length <= 262144 &&
    m.files["sci.json"].bytes <= 65536 &&
    m.files["README.md"].bytes <= 1048576;
  return { id: hash(raw), manifest: m, metadata: meta, entries, baseline };
}
export function record(
  raw: Uint8Array,
  sig: Uint8Array,
  kind: "claim" | "revocation",
  ctx: ObjectValue,
): ObjectValue {
  const o = parseJSON(raw);
  schema(o, kind === "claim" ? "sci-key-claim/0.1" : "sci-key-revocation/0.1");
  keys(
    o,
    kind === "claim"
      ? ["schema", "public_key", "account", "expires"]
      : ["schema", "public_key"],
    kind === "claim" ? ["note"] : [],
  );
  signature(o.public_key, sig, raw);
  requireValue(o.public_key.x === ctx.expected_public_key_x, "expected key");
  if (kind === "claim") {
    keys(o.account, ["type", "id"]);
    const a = o.account;
    requireValue(
      typeof a.type === "string" && typeof a.id === "string",
      "account",
    );
    requireValue(
      a.type === ctx.requested_account.type &&
        a.id === ctx.requested_account.id,
      "requested account",
    );
    if (a.type === "domain") {
      requireValue(
        /^[a-z0-9.-]+$/.test(a.id) &&
          !a.id.endsWith(".") &&
          !/^\d+(\.\d+){0,3}$/.test(a.id) &&
          a.id.length <= 253,
        "domain",
      );
      requireValue(
        a.id
          .split(".")
          .every(
            (x: string) =>
              x.length > 0 &&
              x.length <= 63 &&
              !x.startsWith("-") &&
              !x.endsWith("-"),
          ),
        "domain label",
      );
      try {
        requireValue(new URL("https://" + a.id).hostname === a.id, "IDNA");
      } catch {
        throw new Invalid("IDNA");
      }
    } else if (a.type === "github")
      requireValue(
        /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(a.id) && a.id.length <= 39,
        "GitHub login",
      );
    else requireValue(a.type === "huggingface", "account type");
    if ("note" in o) text(o.note, 2500, true);
    requireValue(
      typeof o.expires === "string" &&
        /^\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d(?:\.\d+)?(?:[Zz]|[+-]\d\d:\d\d)$/.test(
          o.expires,
        ),
      "expiry",
    );
    requireValue(Date.parse(ctx.now) < Date.parse(o.expires), "expired");
    requireValue(
      !ctx.known_revocation && ctx.retrieved_from_account_location,
      "association evidence",
    );
  }
  return o;
}
export { verifyProofs, frame, type TimestampTrust } from "./timestamps.js";
export {
  absolute,
  descriptor,
  expandTemplate,
  submission,
  submissionResponse,
  lookupSources,
} from "./discovery.js";
