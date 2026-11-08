# Combined corpus review

The original corpus targeted draft 0.1 at `ae6d5a362a7183762fc642cd2cac3dfbb9c7b1f5`.
The current spec pin is in [README.md](README.md).

## PNG display profile

- Add 56 signed cases for §4.3.1, including exact limits and values above them.
- Keep all original artifact bytes. Eight cases with non-PNG display files
  become non-baseline. Their publication validity does not change.
- Use one bounded PNG fixture check and Pillow for both audit tools. This is
  not an independent PNG cross-check or a complete PNG validator.
- Read the spec from `protocol/README.md`, its path in this repository.

## Sources

| Source | Revision | Original cases | Integration |
| --- | --- | ---: | --- |
| Codex / original PR #3 | `397a038609acdf801bc3b9055b23a63f5d02e189` | 225 | Existing IDs retained; expectations and descriptor bytes updated. |
| Claude | `a9f733d9fdd33398d72e09e42feebe18fedfc68d` | 189 | All 177 publication/claim/revocation cases retained using the independent generator; 12 timestamp cases mapped to deterministic equivalents. |

[source-map.json](source-map.json) maps every original case to its current case
IDs and records outcome changes. Supplemental IDs start with `supplemental-`;
loose and ZIP inputs have separate IDs. Overlapping cases are intentional: their
independent serializers, signing keys, payloads, and ZIP writers provide useful
cross-checks. Each supplemental artifact was compared byte for byte with its
source commit. The older `Claude outputs/test-vectors.tgz` is superseded by that
commit: its empty-README expectation predates the nonblank requirement.

## Changes required by the current spec

- README must be nonblank. The old Codex `empty-readme` case is now invalid;
  Claude's committed empty and whitespace-only cases already expected rejection.
- RFC 9839 Assignables replaces category and contextual-joiner rules. NBSP,
  private-use characters, formatting characters, and joiners previously rejected
  by both corpora are now valid. Their input bytes are retained. JSON byte-order
  marks and unpaired surrogate escapes remain invalid under §7; a decoded U+FEFF
  inside text or at the start of README is now permitted under §4.2.
- The index descriptor requires `publication_lookup_url`, replacing `lookup_url`.
  Both valid descriptors and their negative mutations were updated. New fixtures
  distinguish a stale descriptor from an allowed extra legacy field.
- §8.1.4 now specifies publication submission. Added request/response cases for
  IDs, distinct nonempty descriptor lists, unsigned account hints, duplicate
  decoded keys, success responses, and error contracts. Retry and mirror
  registration semantics are integration scenarios, not simulated live services.
- §3 explicitly requires 64 raw signature bytes. Added hex, base64, and newline
  cases alongside existing length, signature, and subgroup cases.

## Checker corrections and additional evidence

- Both original checkers parsed `-0` as an acceptable zero byte count. Each now
  retains integer spelling and rejects it; a signed regression bundle tests it.
- The primary checker now enforces nonblank README, RFC 3339 expiry spelling,
  IDNA2008 A-label validation, and the new descriptor/submission fields.
- Both checkers use fixed Assignables predicates; no runtime Unicode category
  tables determine text validity. A separate exhaustive test compares the primary
  exclusion predicate to the RFC's positive intervals across the full codespace.
- The harness reports a supported schema used in the wrong record slot as
  `invalid`, and an unknown schema as `unsupported`. Missing/non-string schema
  values are malformed records. The original corpora disagreed on this label;
  their rejection behavior is preserved and the distinction is now documented.
- The independent checker retains its separate JSON parser hooks, subgroup
  arithmetic, metadata/path validation, and ZIP reconstruction. It runs against
  every current publication, claim, and revocation case, including the original
  Codex corpus. It is not merely run against its own generator's output.
- A downstream JSON-lines adapter runner checks actual implementation results
  without sending expected verdicts to the adapter. Tests ensure it rejects
  missing, duplicate, unknown, and incorrect results, including JSON type errors.

## Timestamp consolidation

Claude's timestamp builder generated fresh keys, certificate dates, and response
times on each run. Its checker used the wall clock and assumed every framed value
had to be a SEQUENCE. The combined corpus instead uses committed public test keys,
fixed dates, explicit trust anchors/CRLs, and general DER tag/length framing.
Under §6.8, a complete nonresponse value fails independently; malformed or
incomplete framing stops parsing without scanning ahead.

The 12 source scenarios map to the corresponding one/two-proof, wrong-imprint,
SHA-1, untrusted, rejection, failed-then-valid, incomplete-tail, no-scan-ahead,
trailing-zero, empty, and revocation cases. These are equivalent coverage, not
copies of the old timestamp bytes or PKI: for example, the untrusted case supplies
no accepted root, and the no-scan-ahead case uses an explicit incomplete length.
SHA-1, failed-then-valid, and trailing-zero cases were added during consolidation.

OpenSSL independently checks positive Ed25519 signatures and each applicable
timestamp proof with the test clock and CRLs. It also confirms two intended
differences from generic crypto verification: the mixed-order key satisfies its
signature equation but SCI rejects its subgroup, and SHA-1/SHA-512 imprints can
be RFC 3161-valid while SCI requires SHA-256.

## Acceptance checks and remaining scope

CI runs the primary audit with OpenSSL, the independent publication/record
checker, full-codespace and adapter-contract regressions, the example adapter,
and byte-for-byte regeneration. Catalog/inventory integrity, case-ID uniqueness,
artifact references, and source-map completeness are checked as well.

The README identifies the remaining URI, PKI, network, rendering, and resource
coverage gaps. Fifteen HTTP/state scenarios are explicitly integration inputs;
the local audits do not claim to have tested a deployed service. Passing a subset
or this corpus alone is not a blanket claim of protocol conformance.
