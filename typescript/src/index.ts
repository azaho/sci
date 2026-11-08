/**
 * @sci/core — runtime-agnostic SCI protocol 0.1 library.
 *
 * Uses only Web-standard APIs (Uint8Array, TextDecoder, crypto.subtle, fetch, Blob) and runs in
 * browsers, Deno, Cloudflare Workers, and Node 22+.
 */
export { type Budget, DEFAULT_BUDGET, resolveBudget } from './budget.ts';
export {
  APPROVED_LICENSE_SHA256,
  BASELINE_LICENSE_SHA256,
  BASELINE_LIMITS,
  type BaselineReport,
  type BundleOptions,
  checkLooseBundle,
  checkZipBundle,
  evaluateBaseline,
  type LooseFiles,
  type ManifestScopeReport,
  type VerifiedBundle,
  verifyLooseBundle,
  verifyManifestScope,
  verifyZipBundle,
} from './bundle.ts';
export { base64UrlDecodeStrict, base64UrlEncode, equalBytes, fromHex, toHex, utf8DecodeStrict } from './bytes.ts';
export {
  type CitationTargetKind,
  citationTargetKey,
  doiKey,
  isAbsoluteHttpUrl,
  isHttpUri,
  type ParsedCitationTarget,
  parseCitationTarget,
  parseDoiUri,
} from './citation.ts';
export {
  type Account,
  type AccountMethod,
  type Claim,
  type ClaimCheckOptions,
  type ClaimLocation,
  checkAccountMethod,
  claimLocations,
  parseAccount,
  parseClaim,
  type Revocation,
  verifyClaim,
  verifyRevocation,
} from './claims.ts';
export {
  type ArchiveDescriptor,
  descriptorLocations,
  type IndexDescriptor,
  interpretPublicationLookup,
  isJsonContentType,
  parseArchiveDescriptor,
  parseIndexDescriptor,
  parseSubmissionRequest,
  routes,
  type SubmissionRequest,
  type SubmissionResponse,
  validateArchiveDescriptor,
  validateIndexDescriptor,
  validateSubmissionRequest,
  validateSubmissionResponse,
} from './descriptors.ts';
export {
  decodePublicKeyX,
  isCanonicalPublicKeyX,
  type PublicJwk,
  parsePublicJwk,
  publicJwkFromSeed,
  signEd25519,
  verifyEd25519,
} from './ed25519.ts';
export {
  type CheckResult,
  capture,
  captureAsync,
  type Outcome,
  SciError,
  type SciIssue,
  toIssue,
  unwrap,
} from './errors.ts';
export { crc32, createSha256, type IncrementalHash, sha256, sha256Hex } from './hash.ts';
export { decodeALabel, isValidClaimDomain, punycodeDecode, punycodeEncode } from './idna.ts';
export { checkFields, checkRecordSchema, digestToId, type IdForm, isDigest, isPublicationId, parsePublicationRef, SCHEMAS } from './ids.ts';
export {
  isArray,
  isObject,
  JsonNumber,
  type JsonObject,
  type JsonParseOptions,
  type JsonValue,
  parseJsonBytes,
  parseJsonText,
  toPlain,
} from './json.ts';
export { interpretRevocationLookup, interpretTimestampLookup, type RevocationLookupResult, type TimestampLookupResult } from './lookups.ts';
export {
  type Manifest,
  type ManifestEntry,
  type ManifestOptions,
  parseManifest,
  publicationIdOf,
  verifyFileBytes,
  verifyManifest,
  zipSizeOf,
} from './manifest.ts';
export {
  type Category,
  CITATION_RELATIONS,
  type Citation,
  type CitationRelation,
  type DisplayItem,
  type Metadata,
  parseMetadata,
  validateMetadata,
} from './metadata.ts';
export { checkManifestPaths, checkPathSyntax, isValidManifestPath } from './paths.ts';
export {
  type AdmissionDecision,
  applyIndexEvent,
  type CatalogEntry,
  decideAdmission,
  EMPTY_INDEX_STATE,
  type EventResult,
  type IndexEvent,
  type IndexPolicyState,
  isAssociationVerified,
  keyStatus,
  locationCount,
  timestampStatus,
} from './policy.ts';
export {
  type AssociationCheckOptions,
  checkKeyAssociation,
  type FetchLike,
  fetchArchiveDescriptor,
  fetchIndexDescriptor,
  fetchPublicationTimestamps,
  fetchRevocation,
  httpRangeSource,
  lookupPublicationSources,
  type MetadataView,
  openPublication,
  type RemotePublication,
  type RetrievalAttempt,
  type RetrievalOptions,
  tryOpenPublication,
} from './retrieval.ts';
export { type ByteSource, blobSource, bytesSource, readAll } from './source.ts';
export { expandTemplate, parseTemplate, type TemplatePart } from './template.ts';
export { checkText, checkTextFile, codePointLength, isUnicodeAssignable, type TextRule } from './text.ts';
export { formatUtc, type Instant, parseRfc3339 } from './time.ts';
export {
  earliestVerifiedTime,
  frameTimestampFile,
  type ProofResult,
  type PublicationTimestampOptions,
  type RevocationPolicy,
  type TimestampFileResult,
  type TimestampOutcome,
  type TimestampProof,
  type TimestampTrustOptions,
  verifyPublicationTimestamps,
  verifyRevocationTimestamps,
  verifyTimestampFile,
} from './timestamp.ts';
export {
  buildCanonicalZip,
  canonicalZipChunks,
  canonicalZipEntries,
  checkZipInventory,
  locateZipEntry,
  readZipDirectory,
  readZipEntriesSpan,
  readZipEntry,
  streamZipEntry,
  type ZipDirectory,
  type ZipEntry,
  type ZipEntryRange,
} from './zip.ts';
export const PROTOCOL_VERSION = '0.1';
