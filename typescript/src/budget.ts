/**
 * Local resource budgets (SCI §6: "limit input sizes and verification work").
 *
 * Exceeding a budget yields `incomplete`, never `invalid`. Defaults are generous enough that
 * every baseline publication (§2.1) completes; callers may tighten or loosen them.
 */
export interface Budget {
  /** Maximum total input bytes examined for one bundle (ZIP size or sum of loose files). */
  readonly maxTotalBytes: number;
  /** Maximum `manifest.json` bytes parsed. */
  readonly maxManifestBytes: number;
  /** Maximum `sci.json` bytes parsed. */
  readonly maxMetadataBytes: number;
  /** Maximum `README.md` / `LICENSE` bytes decoded. */
  readonly maxTextBytes: number;
  /** Maximum generic JSON document bytes (descriptors, claims, revocations, responses). */
  readonly maxJsonBytes: number;
  /** Maximum JSON nesting depth. */
  readonly maxJsonDepth: number;
  /** Maximum entries listed in a manifest / ZIP central directory. */
  readonly maxEntries: number;
  /** Maximum timestamp file bytes. */
  readonly maxTimestampBytes: number;
  /** Maximum number of timestamp proofs checked in one file. */
  readonly maxTimestampProofs: number;
  /** Maximum certificates considered per timestamp token. */
  readonly maxCertificates: number;
  /** Chunk size for streaming reads. */
  readonly chunkBytes: number;
}

export const DEFAULT_BUDGET: Budget = Object.freeze({
  maxTotalBytes: 4 * 1024 * 1024 * 1024,
  maxManifestBytes: 16 * 1024 * 1024,
  maxMetadataBytes: 16 * 1024 * 1024,
  maxTextBytes: 64 * 1024 * 1024,
  maxJsonBytes: 1024 * 1024,
  maxJsonDepth: 128,
  maxEntries: 65_535,
  maxTimestampBytes: 4 * 1024 * 1024,
  maxTimestampProofs: 64,
  maxCertificates: 16,
  chunkBytes: 1024 * 1024,
});

export function resolveBudget(partial?: Partial<Budget>): Budget {
  return partial ? { ...DEFAULT_BUDGET, ...partial } : DEFAULT_BUDGET;
}
