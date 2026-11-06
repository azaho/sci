# Scope and service policy

This experiment implements the bundle-verification requirements in sections 2–4,
5.1, 6.1–6.4, 6.8, and 11 of the [frozen draft](spec-snapshot.md). It tests HTTP
submissions containing only `id` and `url`. It is experimental code, not an audited
library or the complete indexer. Previous partial-verifier results remain under
`../results/` and must not be attributed to this implementation.

| Draft requirement | Implementation | Representative fixtures |
| --- | --- | --- |
| Exact manifest hash and original signed bytes | `verify.ts` | wrong submitted ID, bad signature, duplicate escaped keys |
| Ed25519/JWK including subgroup and scalar checks | `verify.ts`, WebCrypto, pinned noble curves | identity key, noncanonical JWK, invalid scalar |
| Listed files, hashes, sizes, paths and conflicts | `verify.ts` | wrong file hash, traversal, case conflicts, directory conflicts |
| Complete stored ZIP, ordering, headers, directory, CRCs | `verify.ts` | directory mismatch, CRC mismatch, extra fields, compression, attributes, disk number, trailing bytes |
| Every metadata field and reference | `metadata.ts` | categories, keywords, displays, citations, unknown fields, duplicate relations |
| DOI URI encoding and equivalence | `metadata.ts` | encoded slash, invalid UTF-8/control, extra literal slash, ASCII-only case folding |
| Unicode 18 text repertoire and ignorable exclusions | generated `unicode18.ts`, `metadata.ts` | hidden characters, bidi control, noncharacter, BOM, nonblank and code-point length |
| Timestamp response, imprint, CMS, ESS certificate binding, EKU, chain, revocation | `timestamp.ts`, pinned PKI.js/ASN1.js | bad DER/imprint/signature/ESS, revoked certificate, stale CRL, untrusted authority |
| Work and allocation bounds | parser, verifier, HTTP handler | JSON depth/node limits, manifest/metadata/text limits, 1,001-file rejection |

The fixture generator uses Python cryptography and asn1crypto, independently of
the TypeScript verifier. OpenSSL checks the positive generated timestamp before
any benchmarks run. Fixture keys and certificates are disposable. The certificate
authority is trusted only by this benchmark; no public TSA trust store is implied.
Unicode tables come from Unicode's 18.0.0 UCD, with source hashes recorded in the
generated module. Character checks do not use the runtime's Unicode database.

## Candidate admission limits

These are service policy, not changes to SCI:

- 50,000,000 bytes across all files listed in the manifest.
- 1,000 listed files in total, including `sci.json`, `README.md`, and `LICENSE`.
- 1 MiB manifest, 4,000,000-byte `sci.json`, and 1,000,000 bytes each for README and LICENSE.
- 256 KiB timestamp proof; 64,000,000-byte downloaded ZIP limit including overhead.
- Path length 240 ASCII bytes, component length 64; citation target 4,096 code points.
- JSON depth below 32, at most 20,000 JSON values per document.
- Timestamp ASN.1 depth at most 24 and 10,000 nodes; at most eight embedded certificates.
- One active request per isolate, a 30-second source-fetch deadline, and a 4 KiB request body.

The diagnostic 2,000- and 5,000-file fixtures explicitly raise only the file-count
limit. They do not change the candidate 1,000-file admission policy. The timestamp
proof, manifest and signature are outside the listed-file count and have separate
limits. Total listed payload is 50 MB even in the heavy-metadata fixtures.

## Timestamp trust policy

The verifier requires a trusted timestamp when a proof is supplied for this
experiment. Trust roots and fresh CRLs are supplied by the operator, not the
publication. It performs CMS and certificate-chain verification with PKI.js and
also checks ESS v1/v2 certificate binding, exclusive critical timestamp EKU,
message imprint, time, accuracy, and current signed CRL evidence. It does not
fetch URLs embedded in the proof. Missing or stale revocation evidence fails
closed. Known revocation rejects the timestamp, including historic timestamps.

This bounded policy supports issuer-and-serial-number signer identifiers, one
signer, one ESS certificate identifier per binding, no unsigned CMS attributes,
and no unknown critical TSTInfo extensions. Other choices require a policy or
implementation extension; this is not a claim to accept every RFC 3161 token
from every authority. Timestamp trust does not establish scientific validity.

## URL flow and exclusions

The POST contains only the expected manifest ID and a URL. The function downloads
one ZIP through that URL, checks actual byte counts while reading, and verifies
it. The sources are whole objects served by temporary signed Supabase Storage
URLs and a fixed synthetic ZIP served by raw.githubusercontent.com. No source
credentials are forwarded, and input URLs are not logged.

The benchmark deliberately permits only exact fixture paths on those HTTPS
origins and refuses redirects. This exercises remote URL retrieval, including a
non-Supabase host, without exposing a general-purpose fetch endpoint. Supporting
arbitrary publisher domains and GitHub Release redirects still needs a chosen,
tested destination policy. CORS at the source is not required for these server
requests.

Affiliation and revocation-record retrieval, index admission policy, database
writes, moderation, rendering, and disk extraction are separate from the bundle
verifier and are not implemented here. There is no malware scan. The benchmarks
are sequential; they do not establish sustainable throughput or concurrency
limits. Adversarial fixtures are evidence, not a proof that no parser bug exists.
