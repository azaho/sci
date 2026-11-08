/** Hash and checksum primitives. SHA-256 is incremental so large files use bounded memory. */
import { sha1 as nobleSha1 } from '@noble/hashes/legacy.js';
import { sha256 as nobleSha256, sha384 as nobleSha384, sha512 as nobleSha512 } from '@noble/hashes/sha2.js';
import { toHex } from './bytes.ts';

export function sha256(data: Uint8Array): Uint8Array {
  return nobleSha256(data);
}

export function sha256Hex(data: Uint8Array): string {
  return toHex(nobleSha256(data));
}

export function sha1(data: Uint8Array): Uint8Array {
  return nobleSha1(data);
}

export function sha384(data: Uint8Array): Uint8Array {
  return nobleSha384(data);
}

export function sha512(data: Uint8Array): Uint8Array {
  return nobleSha512(data);
}

/** Incremental hasher interface shared by SHA-256 and CRC-32 helpers. */
export interface IncrementalHash {
  update(chunk: Uint8Array): void;
  digest(): Uint8Array;
}

export function createSha256(): IncrementalHash {
  const h = nobleSha256.create();
  return {
    update(chunk) {
      h.update(chunk);
    },
    digest() {
      return h.digest();
    },
  };
}

let CRC_TABLE: Uint32Array | undefined;

function crcTable(): Uint32Array {
  if (CRC_TABLE) return CRC_TABLE;
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  CRC_TABLE = table;
  return table;
}

/** Incremental CRC-32 (ISO-HDLC, as used by ZIP). */
export class Crc32 {
  #crc = 0xffffffff;
  update(chunk: Uint8Array): void {
    const table = crcTable();
    let c = this.#crc;
    for (let i = 0; i < chunk.length; i++) c = (table[(c ^ (chunk[i] ?? 0)) & 0xff] ?? 0) ^ (c >>> 8);
    this.#crc = c;
  }
  value(): number {
    return (this.#crc ^ 0xffffffff) >>> 0;
  }
}

export function crc32(data: Uint8Array): number {
  const c = new Crc32();
  c.update(data);
  return c.value();
}
