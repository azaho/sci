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
