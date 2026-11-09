/** Random-access byte sources so large bundles can be verified with bounded memory. */

export interface ByteSource {
  /** Total size in bytes. */
  readonly size: number;
  /** Read exactly `length` bytes at `offset`. Implementations must reject short reads. */
  read(offset: number, length: number): Promise<Uint8Array>;
}

function checkRange(size: number, offset: number, length: number): void {
  if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0 || offset > size - length) {
    throw new RangeError(`read out of bounds: offset ${offset}, length ${length}, size ${size}`);
  }
}

/** A source over bytes already in memory. */
export function bytesSource(bytes: Uint8Array): ByteSource {
  return {
    size: bytes.length,
    read(offset, length) {
      checkRange(bytes.length, offset, length);
      return Promise.resolve(bytes.subarray(offset, offset + length));
    },
  };
}

/** A source over a Web `Blob`/`File` (e.g. a local ZIP chosen in the browser). */
export function blobSource(blob: Blob): ByteSource {
  return {
    size: blob.size,
    async read(offset, length) {
      checkRange(blob.size, offset, length);
      const buf = new Uint8Array(await blob.slice(offset, offset + length).arrayBuffer());
      if (buf.length !== length) throw new RangeError('short read from Blob');
      return buf;
    },
  };
}

/** Read an entire source (caller must have checked the size against a budget). */
export async function readAll(source: ByteSource): Promise<Uint8Array> {
  return source.read(0, source.size);
}

/** Cache at most two 4 MiB blocks for a sequential full verification pass.
 * The verifier still sees every byte, including ZIP headers and directory data.
 * Single-file readers should use the underlying source without this read-ahead.
 */
export function verificationSource(source: ByteSource): ByteSource {
  const blockBytes = 4 * 1024 * 1024;
  const blocks = new Map<number, Uint8Array>();
  const block = async (start: number): Promise<Uint8Array> => {
    let bytes = blocks.get(start);
    if (!bytes) {
      // Evict before loading, so cached blocks plus the incoming block stay bounded.
      if (blocks.size >= 2) blocks.delete(blocks.keys().next().value as number);
      bytes = await source.read(start, Math.min(blockBytes, source.size - start));
      if (bytes.length !== Math.min(blockBytes, source.size - start)) throw new RangeError('short block read');
    }
    blocks.delete(start);
    blocks.set(start, bytes);
    return bytes;
  };
  return {
    size: source.size,
    async read(offset, length) {
      checkRange(source.size, offset, length);
      if (length === 0) return new Uint8Array(0);
      const out = new Uint8Array(length);
      for (let copied = 0; copied < length; ) {
        const position = offset + copied;
        const start = Math.floor(position / blockBytes) * blockBytes;
        const bytes = await block(start);
        const n = Math.min(length - copied, bytes.length - (position - start));
        out.set(bytes.subarray(position - start, position - start + n), copied);
        copied += n;
      }
      return out;
    },
  };
}
