// Complete bundle checks for the frozen draft, with explicit service limits.
import { metadata, textField } from "./metadata.ts";
import { timestamp, type TimestampTrust } from "./timestamp.ts";
import { ed25519 } from "npm:@noble/curves@1.9.7/ed25519";

export const LIMITS = { zip: 64_000_000, payload: 50_000_000, manifest: 1_048_576, files: 1000, metadata: 4_000_000, text: 1_000_000, timestamp: 262_144 };
const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
const hex = (b: Uint8Array) => Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
function check(ok: unknown, code: string): asserts ok { if (!ok) throw new Error(code); }
export async function sha256(b: Uint8Array): Promise<string> {
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", b as BufferSource)));
}

// Preserve original signed bytes, but reject ambiguous JSON before using its fields.
// The numeric and depth limits here are benchmark policy, not universal SCI rules.
export function parseJson(data: Uint8Array): any {
  check(data[0] !== 0xef || data[1] !== 0xbb || data[2] !== 0xbf, "json_bom");
  const s = utf8.decode(data);
  let p = 0, nodes = 0;
  const ws = () => { while (p < s.length && /[\x20\t\r\n]/.test(s[p])) p++; };
  const str = (): string => {
    const start = p++;
    while (p < s.length) {
      if (s[p] === "\\") { p += 2; continue; }
      if (s[p++] === '"') {
        const value = JSON.parse(s.slice(start, p));
        check(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value), "json_surrogate");
        return value;
      }
    }
    throw new Error("json_string");
  };
  const value = (depth: number): any => {
    ws(); check(depth < 32, "json_depth"); check(++nodes<=20000,"json_nodes");
    if (s[p] === '"') return str();
    if (s[p] === "{" || s[p] === "[") {
      const object = s[p++] === "{";
      const end = object ? "}" : "]";
      const result: any = object ? Object.create(null) : [];
      ws(); if (s[p] === end) { p++; return result; }
      for (;;) {
        ws();
        if (object) {
          check(s[p] === '"', "json_key");
          const k = str(); ws(); check(s[p++] === ":", "json_colon");
          check(!Object.hasOwn(result, k), "json_duplicate"); result[k] = value(depth + 1);
        } else result.push(value(depth + 1));
        ws(); if (s[p] === end) { p++; return result; }
        check(s[p++] === ",", "json_comma");
      }
    }
    const n = /^(?:0|[1-9][0-9]*)/.exec(s.slice(p));
    check(n, "json_value"); p += n[0].length;
    const number = Number(n[0]); check(Number.isSafeInteger(number), "json_integer"); return number;
  };
  const result = value(0); ws(); check(p === s.length, "json_trailing"); return result;
}

const crcTable = new Uint32Array(256).map((_, n) => {
  for (let j = 0; j < 8; j++) n = (n >>> 1) ^ ((n & 1) ? 0xedb88320 : 0);
  return n >>> 0;
});
function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) crc = crcTable[(crc ^ bytes[i]) & 255] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}
function fields(o: any, expected: string[], code: string) {
  check(o && typeof o === "object" && !Array.isArray(o) &&
    Object.keys(o).sort().join(",") === expected.sort().join(","), code);
}
function validatePaths(paths: string[]) {
  const files = new Set(paths.map(p => p.toLowerCase()));
  check(files.size === paths.length, "path_collision");
  const directories = new Map<string, string>();
  for (const path of paths) {
    check(["sci.json", "README.md", "LICENSE"].includes(path) || path.startsWith("files/"), "path_root");
    check(path.length<=240,"path_limit");
    const components = path.split("/");
    for (let i = 0; i < components.length; i++) {
      const c = components[i]; check(c.length<=64,"path_limit");
      check(/^[A-Za-z0-9._-]+$/.test(c) && c !== "." && c !== ".." && !c.endsWith(".") &&
        !/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(c), "path_component");
      if (i < components.length - 1) {
        const dir = components.slice(0, i + 1).join("/"); const lower = dir.toLowerCase();
        check(!files.has(lower) && (!directories.has(lower) || directories.get(lower) === dir), "path_directory");
        directories.set(lower, dir);
      }
    }
  }
}
export async function verifyZip(bytes: Uint8Array, expectedId: string, sample = () => {}, trust: TimestampTrust = {roots:[],crls:[]}, fileLimit = LIMITS.files) {
  check(bytes.length <= LIMITS.zip, "zip_limit");
  const times: Record<string, number> = {};
  let stage = performance.now();
  const mark = (name: string) => { times[name] = performance.now() - stage; stage = performance.now(); sample(); };
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const u16 = (p: number) => v.getUint16(p, true);
  const u32 = (p: number) => v.getUint32(p, true);
  type Entry = { name: string; offset: number; start: number; size: number; crc: number };
  const entries: Entry[] = [];
  let p = 0;
  while (p + 30 <= bytes.length && u32(p) === 0x04034b50) {
    check(entries.length < fileLimit + 3, "file_count");
    const size = u32(p + 18); const n = u16(p + 26); const start = p + 30 + n;
    check(u16(p + 4) === 10 && u16(p + 6) === 0 && u16(p + 8) === 0 &&
      u16(p + 28) === 0 && size !== 0xffffffff && size === u32(p + 22) && start + size <= bytes.length, "zip_local");
    const name = utf8.decode(bytes.subarray(p + 30, start));
    entries.push({ name, offset: p, start, size, crc: u32(p + 14) }); p = start + size;
  }
  const central = p;
  for (const e of entries) {
    check(p + 46 <= bytes.length && u32(p) === 0x02014b50, "zip_directory");
    const n = u16(p + 28);
    check(u16(p + 4) >>> 8 === 0 && u16(p + 6) === 10 && u16(p + 8) === 0 && u16(p + 10) === 0 &&
      u32(p + 16) === e.crc && u32(p + 20) === e.size && u32(p + 24) === e.size &&
      u16(p + 30) === 0 && u16(p + 32) === 0 && u16(p + 34) === 0 && u16(p + 36) === 0 &&
      u32(p + 38) === 0 && u32(p + 42) === e.offset &&
      u32(p + 12) === u32(e.offset + 10) &&
      n === u16(e.offset + 26) && utf8.decode(bytes.subarray(p + 46, p + 46 + n)) === e.name, "zip_directory");
    p += 46 + n;
  }
  check(p + 22 === bytes.length && u32(p) === 0x06054b50 && u16(p + 4) === 0 && u16(p + 6) === 0 &&
    u16(p + 8) === entries.length && u16(p + 10) === entries.length && u32(p + 12) === p - central &&
    u32(p + 16) === central && u16(p + 20) === 0, "zip_end");
  check(entries[0]?.name === "manifest.json" && entries[1]?.name === "manifest.json.sig", "zip_order");
  mark("zip_structure_ms");
  const data = (e: Entry) => bytes.subarray(e.start, e.start + e.size);
  check(entries[0].size <= LIMITS.manifest, "manifest_limit");
  const raw = data(entries[0]);
  check(await sha256(raw) === expectedId, "manifest_hash");
  const m = parseJson(raw);
  fields(m, ["schema", "public_key", "files"], "manifest_fields");
  check(m.schema === "sci-publication/0.18", "schema");
  check(m.files && typeof m.files === "object" && !Array.isArray(m.files), "files");
  const paths = Object.keys(m.files).sort(); validatePaths(paths);
  check(paths.includes("sci.json") && paths.includes("README.md") && paths.length <= fileLimit, "required_files");
  let total = 0;
  for (const path of paths) {
    const f = m.files[path]; fields(f, ["sha256", "bytes"], "file_fields");
    check(typeof f.sha256 === "string" && /^[0-9a-f]{64}$/.test(f.sha256) && Number.isSafeInteger(f.bytes) && f.bytes >= 0, "file_descriptor");
    total += f.bytes; check(total <= LIMITS.payload, "payload_limit");
  }
  const hasTimestamp = entries.at(-1)?.name === "manifest.json.sig.tsr";
  check(entries.length === paths.length + (hasTimestamp ? 3 : 2) && paths.every((name, i) => entries[i + 2].name === name), "zip_order");
  if(hasTimestamp) check(entries.at(-1)!.size<=LIMITS.timestamp,"timestamp_limit");
  for(const e of entries) {
    if(e.name==='sci.json')check(e.size<=LIMITS.metadata,'metadata_limit');
    if(e.name==='README.md' || e.name==='LICENSE')check(e.size<=LIMITS.text,'text_limit');
  }
  mark("manifest_ms");
  fields(m.public_key, ["kty", "crv", "x"], "jwk");
  check(m.public_key.kty === "OKP" && m.public_key.crv === "Ed25519" && /^[A-Za-z0-9_-]{43}$/.test(m.public_key.x), "jwk");
  const pk = Uint8Array.from(atob(m.public_key.x.replaceAll("-", "+").replaceAll("_", "/") + "="), c => c.charCodeAt(0));
  check(btoa(String.fromCharCode(...pk)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "") === m.public_key.x, "jwk");
  const sig = data(entries[1]); check(sig.length === 64, "signature");
  try {
    for (const encoded of [pk, sig.subarray(0, 32)]) {
      const point = ed25519.ExtendedPoint.fromHex(encoded, false);
      check(!point.is0() && point.isTorsionFree(), "signature");
    }
    const s = BigInt("0x" + hex(sig.subarray(32).slice().reverse()));
    check(s < 2n ** 252n + 27742317777372353535851937790883648493n, "signature");
    const key = await crypto.subtle.importKey("raw", pk, "Ed25519", false, ["verify"]);
    check(await crypto.subtle.verify("Ed25519", key, sig as BufferSource, raw as BufferSource), "signature");
  } catch { throw new Error("signature"); }
  mark("signature_ms");
  let hashMs = 0, crcMs = 0;
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i]; const content = data(e);
    let now = performance.now();
    check(crc32(content) === e.crc, "zip_crc"); crcMs += performance.now() - now;
    if (i >= 2 && !(hasTimestamp && i===entries.length-1)) {
      check(e.size === m.files[e.name].bytes, "file_size"); now = performance.now();
      check(await sha256(content) === m.files[e.name].sha256, "file_hash"); hashMs += performance.now() - now;
    }
    if (i % 32 === 0) sample();
  }
  mark("files_ms"); times.hash_ms = hashMs; times.crc_ms = crcMs;
  for (const e of entries) {
    if (e.name === "sci.json") {
      const meta = parseJson(data(e));
      metadata(meta,new Set(paths));
    } else if (e.name === "README.md" || e.name === "LICENSE") {
      textField(utf8.decode(data(e)),LIMITS.text,true);
    }
  }
  mark("metadata_ms");
  const proof=hasTimestamp ? await timestamp(data(entries.at(-1)!),sig,trust) : null;
  mark("timestamp_ms");
  return { files: paths.length, payload_bytes: total, manifest_bytes: raw.length, times, timestamp:proof };
}
