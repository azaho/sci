# SCI test vectors

673 offline cases for [SCI protocol draft 0.1](../README.md), pinned to
[commit d17af535ab1e50850fedcddba57a719cf8659e86](https://github.com/azaho/sci-dev/blob/d17af535ab1e50850fedcddba57a719cf8659e86/protocol/README.md).
The catalog records the exact specification SHA-256. These fixtures combine
both original corpora, update their expectations for the current draft, and add
regressions found during review. [REVIEW.md](REVIEW.md) explains the changes;
[source-map.json](source-map.json) accounts for all 225 Codex and 189 Claude cases.

This is an interoperability corpus, not a production verifier or an exhaustive
certification suite. The protocol remains authoritative. Passing the bundled
audits validates the corpus, not a future implementation.

All artifacts are committed directly in Git. No release download, Git LFS,
network service, production key, or public TSA is required. The corpus occupies
about 148 MiB after checkout, chiefly two real ZIPs of 67,108,864 and 67,108,865
bytes. These repetitive boundary fixtures are not performance benchmarks.

## Catalog and operations

Start with [`0.1/catalog.json`](0.1/catalog.json). Every case has a unique `id`,
`kind`, protocol `sections`, description, named `artifacts`, and `expected`
results; some also specify `context` and `provenance`. Artifact paths are relative
to `0.1/`. Read original bytes without newline conversion or normalization. Do
not execute content or extract ZIPs before verification.

[`0.1/inventory.json`](0.1/inventory.json) records lengths and SHA-256 hashes of
all corpus files except itself. The ZIP's hash in this inventory is not the
publication ID: that is the hash of the original manifest bytes.

| Kind | Cases | Operation |
| --- | ---: | --- |
| `publication` | 540 | Verify a ZIP, loose bundle, or manifest-only input; classify baseline separately. |
| `claim` | 33 | Verify a signed key association using the supplied account, retrieval evidence, clock, and revocation state. |
| `revocation` | 12 | Verify a revocation record against the expected key. |
| `timestamp` | 29 | Frame DER values, verify each proof with the supplied clock, trust roots, and CRLs, and preserve usable earlier proofs. |
| `discovery` | 18 | Validate an index or archive descriptor. |
| `submission` | 25 | Validate the request or response contract in §8.1.4. |
| `template` | 1 | Expand the templates to the exact expected URLs. |
| `http` | 5 | Interpret an offline response through an implementation adapter. |
| `scenario` | 10 | Execute the ordered events through an implementation adapter. |

For publications, an artifact named `bundle.zip` selects ZIP verification.
Otherwise, artifact keys are the exact loose bundle paths, including
`manifest.json` and `manifest.json.sig`. `context.scope` defaults to `complete`;
`manifest` means only §6.1 and any stated ZIP-size calculation are requested.
A missing payload in a manifest-only case is intentional. It cannot establish
complete verification or baseline eligibility, so `baseline` is null.

`context.publication_id`, when supplied, is the expected manifest identifier.
`transport: unavailable` and `verification_budget_bytes` model inability to
complete verification, not bad publication bytes. The existing ZIP fixtures
are intentionally named `bundle.zip`: renaming a ZIP must not prevent reading.

For complete loose inputs, compare the exact inventory while excluding the
optional `manifest.json.sig.tsr` sidecar. When `expected.zip_sha256` is present,
construct the canonical ZIP from the verified bytes and compare that hash.
`expected.zip_bytes` uses the §2.1 formula; manifest-only size cases also report
`within_baseline_zip_limit`, without claiming the payloads exist or are valid.
The supplemental cases retain independently generated signed bytes and ZIPs.
Each loose/ZIP representation is a separate catalog case linked by `provenance`.

## Expected results

These result labels are the test harness's vocabulary, not new SCI wire fields:

- `valid`: the requested checks succeeded. No claim of scientific correctness,
  safe content, license ownership, or catalog admission is implied.
- `invalid`: a required format, signature, binding, or association check failed.
- `unsupported`: the schema string identifies no supported schema. A known
  supported schema used for the wrong record type is `invalid`.
- `incomplete`: unavailable evidence or a local resource budget prevented checks.
- `partial` (timestamps): at least one proof verifies, but another proof or tail
  is unusable.
- `untrusted` (timestamps): the proof is cryptographically valid but no accepted
  trust anchor is configured for the case.

For publications, `baseline` is boolean only after complete successful bundle
verification. It is null for invalid, unsupported, incomplete, and manifest-only
results. A valid non-baseline publication must not be called invalid merely
because a service would decline it. Negative cases can violate several rules;
error messages and the order in which failures are detected are not prescribed.

Claims use `retrieved_from_account_location` as supplied evidence of the hosting
check, and `known_revocation` as already-verified revocation knowledge. A local
signature alone does not establish a hosted association. Submission success
confirms location registration; it does not promise catalog admission. The
submission fixtures exercise the protocol contract, not Sciscape-specific policy.

## Test a future implementation

Implement a JSON-lines adapter, in any language. For every input line on stdin:

```json
{"adapter_version":"1","corpus":"/absolute/path/to/0.1","case":{"id":"minimal-baseline","kind":"publication","artifacts":{"bundle.zip":"publication/minimal-baseline/bundle.zip"},"context":{"publication_id":"sha256:..."}}}
```

The real request also contains the case's description and sections. The runner
omits `expected`; the adapter must inspect the artifact bytes and perform the
requested checks. Write one result per case on stdout, using exactly the same
ID; put logs on stderr:

```json
{"id":"minimal-baseline","result":{"outcome":"valid","baseline":true}}
```

Every field named in the catalog's `expected` object must be returned with its
specified value and JSON type. Extra diagnostic fields are allowed. Missing,
duplicate, unknown, malformed, or incorrect results fail the run. Arrays retain
their specified order. The process must exit successfully within the deadline.

```sh
python3 test-vectors/tools/run_adapter.py -- /path/to/your-adapter
```

During implementation, select supported operation kinds explicitly. A subset
pass must be reported as a subset pass, not whole-protocol conformance:

```sh
python3 test-vectors/tools/run_adapter.py \
  --kind publication --kind claim --kind revocation -- /path/to/your-adapter
```

The runner itself uses only Python's standard library. An example adapter wraps
the independent checker for those three kinds; it is not a production library:

```sh
/tmp/sci-vectors-venv/bin/python test-vectors/tools/run_adapter.py \
  --kind publication --kind claim --kind revocation -- \
  /tmp/sci-vectors-venv/bin/python test-vectors/tools/reference_adapter.py
```

## Regenerate and audit

Consuming fixtures needs no Python packages. The generation and audit tools
require Python 3.11+ and the pinned dependencies:

```sh
python3 -m venv /tmp/sci-vectors-venv
/tmp/sci-vectors-venv/bin/python -m pip install -r test-vectors/requirements.txt
/tmp/sci-vectors-venv/bin/python test-vectors/tools/audit.py --openssl
/tmp/sci-vectors-venv/bin/python test-vectors/tools/independent.py
/tmp/sci-vectors-venv/bin/python test-vectors/tools/test_tools.py
/tmp/sci-vectors-venv/bin/python test-vectors/tools/check_reproducible.py
```

`--openssl` requires OpenSSL 3.x or 4.x. It independently verifies positive
Ed25519 signatures, the mixed-order-key signature equation, and timestamp
signatures/chains/CRLs. A mixed-order public key deliberately satisfies OpenSSL's
ordinary equation but fails SCI's additional prime-subgroup requirement.
SHA-1 and SHA-512 timestamp imprints pass RFC 3161 but fail SCI's SHA-256 rule.

The two Python checkers share no JSON, Unicode, ZIP, path, or Ed25519 subgroup
validation code. They both use `cryptography` for the final Ed25519 equation and
`idna` for IDNA2008 domain validation. One ZIP audit checks headers and Python's
`zipfile`; the other reconstructs the canonical ZIP and compares all bytes.
The text regression test checks every one of the 1,114,112 Unicode code points
against RFC 9839's positive intervals. Neither text checker uses runtime Unicode
category data.

Both checkers use the same baseline display structure check. This check is not
independently implemented. The 64 display cases cover size and dimension limits,
chunk boundaries, animation chunks, extension case, and unselected files.
Nine existing cases accept PNG faults outside the required baseline checks.
Eight original cases with non-PNG display files become non-baseline.
Existing signed bytes and publication validity results do not change.

The 15 HTTP/state scenarios are declarative adapter inputs: the corpus audit
checks their structure, not a live service. The other 658 expected results are
executed by the audit. CI repeats the audits, adapter example, and regeneration
on Ubuntu/Python 3.12; local validation also uses Python 3.14/OpenSSL 4.

To regenerate intentionally after reviewing a draft change:

```sh
/tmp/sci-vectors-venv/bin/python test-vectors/tools/generate.py
```

Generation refuses a changed spec hash. Update the explicit commit/hash pins,
review expected results and the source map, regenerate, and audit the diff.
Keep case IDs attached to the same inputs and operation; draft spec changes may
change the expected result. Do not edit signed JSON or binary fixtures by hand.
The regeneration check builds in a temporary directory and compares every byte.

## Timestamp environment and limitations

All PKI is synthetic. Never install the test root or use the public test-only
keys in production. Evaluation time is fixed at **2026-11-07T12:00:00Z**. Use each
case's explicit trust anchors and current CRLs, not the wall clock or network.
The catalog records every complete DER value's offset, length, result, and
verified time/accuracy. `stop_offset` identifies the first malformed/incomplete
tail or EOF. A complete DER value that is not a TimeStampResp is an invalid
proof; it does not prevent checking the next complete value. Do not scan ahead
past incomplete framing. Timestamp failure never invalidates publication bytes.

The corpus does not exhaust DOI equivalence, arbitrary URI/IDNA syntax,
RFC 3339 leap seconds, all Ed25519 encodings, arbitrary certificate chains and
revocation policies, deep parser/resource attacks, live retrieval/CORS, browser
rendering, concurrency, or storage/throughput behavior. Display checks cover
baseline structure, not PNG conformance, decoding, or every resource attack.
Unselected supporting files are opaque bytes; no fixture authorizes executing them.
Independent implementations should add coverage for these documented gaps.
