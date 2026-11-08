/**
 * SCI §2.1 display files: the streaming PNG structure check required for baseline status.
 * It keeps a few bytes of state and does not verify CRCs or decode the image.
 */

export const DISPLAY_LIMITS = Object.freeze({
  bytes: 2_097_152,
  pixels: 8_000_000,
  dimension: 8_192,
});

const SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const FORBIDDEN = new Set(['acTL', 'fcTL', 'fdAT']);

/** True if the path and size allow a baseline display file. */
export function isDisplayCandidate(path: string, bytes: number): boolean {
  return path.slice(-4).toLowerCase() === '.png' && bytes <= DISPLAY_LIMITS.bytes;
}

type Stage = 'signature' | 'header' | 'chunks' | 'ended';

/** Streaming §2.1 checks: signature, chunk layout, IHDR dimensions, no animation chunks. */
export class PngStructure {
  private stage: Stage = 'signature';
  private readonly pending = new Uint8Array(16);
  private filled = 0;
  private skip = 0;
  private failed = false;

  update(chunk: Uint8Array): void {
    let offset = 0;
    while (offset < chunk.length && !this.failed) {
      if (this.skip > 0) {
        const step = Math.min(this.skip, chunk.length - offset);
        this.skip -= step;
        offset += step;
      } else if (this.stage === 'ended') {
        this.failed = true;
      } else {
        const needed = this.stage === 'header' ? 16 : 8;
        const step = Math.min(needed - this.filled, chunk.length - offset);
        this.pending.set(chunk.subarray(offset, offset + step), this.filled);
        this.filled += step;
        offset += step;
        if (this.filled === needed) {
          this.filled = 0;
          this.consume();
        }
      }
    }
  }

  private consume(): void {
    const bytes = this.pending;
    if (this.stage === 'signature') {
      this.failed = SIGNATURE.some((value, index) => bytes[index] !== value);
      this.stage = 'header';
      return;
    }
    const view = new DataView(bytes.buffer);
    const size = view.getUint32(0);
    const kind = String.fromCharCode(...bytes.subarray(4, 8));
    if (FORBIDDEN.has(kind)) {
      this.failed = true;
    } else if (this.stage === 'header') {
      const width = view.getUint32(8);
      const height = view.getUint32(12);
      const limits = DISPLAY_LIMITS;
      this.failed =
        kind !== 'IHDR' ||
        size !== 13 ||
        width < 1 ||
        height < 1 ||
        width > limits.dimension ||
        height > limits.dimension ||
        width * height > limits.pixels;
      this.stage = 'chunks';
      this.skip = size - 8 + 4;
    } else if (kind === 'IHDR') {
      this.failed = true;
    } else if (kind === 'IEND') {
      this.failed = size !== 0;
      this.stage = 'ended';
      this.skip = 4;
    } else {
      this.skip = size + 4;
    }
  }

  /** True if the bytes received so far are one complete file that passes the checks. */
  get ok(): boolean {
    return !this.failed && this.stage === 'ended' && this.skip === 0 && this.filled === 0;
  }
}
