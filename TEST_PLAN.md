# SCI test plan

Use [DESIGN.md](DESIGN.md) as the source of requirements. Complete these checks before importing the existing corpus.

## Run the tests

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[server,test]'
pytest -q
```

Unit tests need nothing else. Integration tests start real services and
skip when a binary is missing: PostgreSQL comes from the `pgserver` wheel;
set `SCI_TEST_RUSTFS` and `SCI_TEST_POSTGREST` to RustFS 1.0.3 and PostgREST
12.2.12 binaries (the CI workflow downloads both).

## Signatures and keys

Check signature interoperability in both directions. SCI must sign a manifest that OpenSSL can verify. OpenSSL must sign the same manifest in a form that SCI can verify.

Check key formats and identities:

- Confirm that PKCS#8 private keys and SubjectPublicKeyInfo public keys load correctly.
- Convert Ed25519 public JWKs to raw keys and back. Confirm that JWK and PEM representations produce the same raw key and fingerprint.
- Reject incorrect `kty` or `crv`, malformed or incorrectly sized `x`, and private `d` fields.
- Reject non-Ed25519 keys.

Check signature and file handling:

- Keep signatures as exactly 64 raw bytes through multipart upload, R2 storage, pull, and mirroring.
- Reject altered signatures, appended newlines, and base64 text supplied as a binary signature.
- Confirm that `sci verify --public-key` fails if the expected publisher key differs.
- Confirm that standalone `verify` fails for missing or changed declared files, including attachments and `LICENSE`.

## Field validation

Run shared fixtures through signing, local verification, server admission, pull verification, and archive recovery. For server and verifier rejection tests, include correctly signed but schema-invalid documents so failures cannot be attributed only to a bad signature. Admission must reject invalid metadata before issuing upload URLs or inserting catalog fields.

- Accept ordinary multilingual text, mathematical symbols, combining sequences, and emoji, including a valid JSON surrogate pair representing one non-BMP character. Preserve original bytes and distinguish canonically equivalent Unicode spellings without normalizing them.
- Reject escaped NUL (`\u0000`), lone surrogates, noncharacters, and forbidden controls, including DEL and C1 controls. Cover nested strings, citation reasons, keywords, and preview paths. Permit TAB/LF/CR in abstracts and reasons; reject them and U+2028/U+2029 in single-line fields. Test valid JSON escapes as well as malformed UTF-8 bytes.
- Reject missing, empty, and whitespace-only titles/abstracts, plus empty or whitespace-only optional keywords, captions, and reasons. Cover non-ASCII whitespace using the shared definition. Accept omitted optional fields and empty optional arrays; reject nulls. Check exact and over-limit lengths for all text fields, including 400-character captions and 1,000-character reasons, counting decoded code points rather than JSON escape lengths or UTF-16 code units.
- Reject unknown fields at every object level, duplicate keys (including escaped spellings of the same key), non-object document roots, BOMs, trailing data, NaN/Infinity, wrong array item types, and type coercions. Reject Boolean/string/fractional/exponent forms for integer fields, negative file sizes, over-limit sizes, invalid hashes, and unsupported schema versions. Require a real Boolean for `truncated`.
- Accept valid UTC timestamps, leap days, and optional one-to-six-digit fractions. Reject impossible dates, year zero, offsets, missing timezones, leap seconds, excessive fractional precision, and surrounding whitespace. Accept valid old or future claimed creation times.
- Check actionable field-specific errors and confirm that rejection never strips characters, truncates values, normalizes text, or rewrites either original JSON document. Verify that accepted metadata and derived citation keys can be inserted into the actual PostgreSQL schema and indexes without storage errors.

## Server configuration

Verify discovery of `sci-server.json` beneath the configured instance URL. Test domain-root and subpath deployments, with and without a trailing slash. Preserve the base path when resolving the filename. Test `archive_url` on a separate origin and under a subpath, with and without a trailing slash. Append only the UUID and filename; do not duplicate `publications/`.

Test saved and missing defaults, offline setting changes, invalid URLs, and preservation of other settings. Verify explicit override precedence, displayed destinations, unchanged defaults after overrides, and corrective errors when no server is set.

## Complete workflow

Sign a folder with text, citation, headline figure, and supporting files. Push through direct upload, view through the separate static site calling PostgREST, then pull from another machine. Check direct `/publication/?id=UUID` links, refreshes, and R2 text/image/download delivery. Test both a domain-root site and a GitHub Pages project subpath. Confirm that shared assets and the instance descriptor resolve correctly from the nested reader page. Show clear errors for missing or invalid UUIDs and publications absent from the catalog. Confirm that newly accepted publications appear without rebuilding the website. Confirm that the feed shows optional headline figures and publication pages display the original headline figure once above the README, below the title and metadata, with its caption when supplied. Confirm that publications without a headline figure have no figure section or image request.

Check dates in browsers using different local time zones, including timestamps near midnight UTC and across daylight-saving changes. Feed acceptance times and publication author/acceptance times must show local dates, hours, and minutes without a time-zone abbreviation; their `datetime` attributes retain the original timestamps.

At mobile widths up to 760 px, feed and publication figures must fill the text container width, preserving the page's side margins, without horizontal scrolling or an expand/collapse control. Check landscape and portrait figures, image loading failures, and tapping the image. On desktop, feed thumbnails must still expand and collapse with mouse and keyboard. Resize between mobile and desktop after expanding a figure; mobile figures must stay aligned with the text container.

Check the search form at widths from 320 to 760 px: the search placeholder and default subject/sort labels must fit, the Search button must have compact padding, the dropdowns and publisher field must be 34 px tall with 7 px vertical padding below the taller search row, and a horizontal divider must separate the publisher field from the dropdown row. Verify search submission, query preservation when resizing, and restoration of the original desktop labels and layout.

Repeat with a README-only publication: acceptance still requires verification, and pull retrieves the complete record. Preserve exact author JSON, including whitespace and omitted fields; create no global store. Verify push rejects missing/changed files without re-signing or repackaging. Reject changed JSON, README, manifest, and mixed-publication files.

Confirm exact originals in R2 and only extracted fields/references in SQL. Normal page views must use catalog metadata without original JSON/signature retrieval or verification. Pull must verify every declared file and fail on unavailable or invalid attachments. Confirm that it constructs UUID-only archive paths without a catalog request. Reject missing or invalid acceptance records, a UUID that differs from the requested UUID, and an acceptance hash that differs from the manifest hash. Exclude `acceptance.json` from the pulled folder.

Push several results privately, then mirror one with pull/push from a machine without a private key. Enable the original author’s key on the destination’s whitelist. Confirm that a different submitting person needs no registered identity. Preserve the UUID, author signature, and bytes. Do not transfer unselected results or citation targets.

## File preview

Test nested and empty directories, empty ZIPs, and prepared ZIPs without source folders. Match generated paths to packaged names; retain deeper contents only in the archive. Process a synthetic million-entry stream with bounded candidates and both preview caps.

Cover long/non-ASCII names, JSON escaping, combined-limit truncation, and metadata too large even without preview paths. Reject oversized signed previews, excessive depth, unsafe paths, and oversized JSON without repair. Preserve the signed preview through public views and recovery. Render markup filenames literally without ZIP requests; retain download access when the preview is absent.

Preview edits must invalidate the signature. A newly signed inaccurate preview can pass: acceptance and recovery must use no ZIP parser or claim independent listing confirmation.

## License and file checks

Verify exact bundled license generation before signing, approved digest/size, and rejection of a different existing license without overwrite. Before upload preparation, reject missing license entries, unapproved hashes, and wrong sizes, including validly signed custom-license manifests.

Issue no client license URL. Completion must read/hash every declared file and reject missing/changed licenses or other changed files. It must never decompress ZIPs or require embedded licenses. Preserve license bytes through pull/mirroring. These checks establish commitment and integrity, not legal provenance.

## Admission and recovery

Reject changed metadata, altered signatures, and changed files. Reject author keys absent from the whitelist or marked as disabled. Reject a stored key that does not match the manifest’s public key. Signing alone must not grant publishing permission.

Alice signs a publication and Bob submits it without a private key. Accept it only where Alice’s key is enabled on the whitelist. Bob’s own key and whitelist status do not affect the request.

Test the same signed publication on two servers. Each server must independently check its own whitelist. Approval on one server must not grant approval on another. Disable a key during a pending upload and confirm that finalization rejects it before commit.

Test concurrent preparation and completion requests for the same UUID and manifest. Confirm one upload attempt and one accepted publication. Submitting an identical accepted publication again must return the existing result without storing another copy. Retries must not extend the upload deadline.

Reject completion with the wrong UUID, attempt ID, or manifest hash. Reject expired and failed attempts. After cleanup of an attempt that failed before finalization committed, test a fresh attempt with a new ID and temporary prefix. Old completion requests must not affect the new attempt. Confirm that committed finalization resumes even after the key is disabled.

Test publication size limits and request rate limits. Reject malformed UUIDs and UUIDs that are not version 4. Accept valid UUIDv4s with many zeros. Reject conflicting UUIDs during concurrent requests and imports from other instances.

Keep temporary uploads private. Require the committed SQL claim before any copy into the `publications/<uuid>/` directory. A losing concurrent claimant must write no published objects. Reject a copy if its source ETag has changed since verification. Partially copied public directories must have no acceptance record and must fail `pull`.

After database restoration, retain citations to absent targets and references to objects. Interrupt processing before and after the finalization transaction, each file copy, acceptance-record write, and visibility update. Recovery must preserve the committed publication and timestamp without creating duplicates. Verify matching destination objects before reusing them under the storage lock. Copy missing files, and reject conflicting destination contents without overwrite. Cleanup must retain temporary sources needed by committed finalization. Missing or changed sources must not release the UUID to a different manifest.

For a file that still needs copying, confirm terminal failure on a missing source or changed verification ETag, with no acceptance record or catalog visibility. Verify that preparation and completion retries return `409 finalization_failed`, including for the identical manifest, and issue no replacement upload URLs. A newly signed publication with a new UUID can proceed normally. Retain failed reservations and remaining objects; manual object cleanup must not release the UUID. Missing temporary sources must not cause failure when all corresponding destination files have already been verified. Timeouts and temporary service errors must remain retryable. Test an uncertain acceptance-record write: a matching durable record must lead to acceptance, never terminal failure. Serialize concurrent completion and recovery workers so none can write an acceptance record after terminal failure.

Concurrent conflicts must never produce two acceptance records for one UUID. Catalog views must exclude incomplete records. Direct pull may succeed after the acceptance record is durable but before the final SQL visibility update.

Rebuild only the derived catalog from R2. Compare metadata, ordered citations, timestamps, and object references. Ignore object prefixes without acceptance records.

Separately test total SQL loss. Restore public reading. Keep writes disabled until operational state, including failed UUID reservations, has been restored and reconciled with R2. Verify that archive-only catalog recovery cannot silently reopen a failed UUID. Do not infer whitelist membership or enabled status from author keys.

## Citations

Test SCI and DOI targets, target-only citations, and each optional field independently. Reject unsupported target formats without rewriting the signed input. Preserve original DOI spelling while matching equivalent forms through `target_key`.

Accept canonical 40-character SCI targets and otherwise valid DOI targets at the 512-ASCII-character boundary; reject 513-character targets during signing, verification, and server validation before issuing upload URLs. Count the scheme prefix and each character of percent encoding, but measure the parsed string rather than JSON escape sequences. Reject raw non-ASCII target characters; use valid percent-encoded DOI fixtures. Check the derived `target_key` limit of 512 UTF-8 bytes, including multibyte cases if the normalizer emits decoded Unicode. Apply the same limits to search and normalization inputs. Oversized values must fail with an actionable error, without truncation or rewriting signed bytes.

Include suffix slashes, literal percent signs, malformed UTF-8/escapes, and query/fragment rejection. Require no DOI registration/resolution. SQL and Python normalizers must match shared fixtures. Equivalent incoming queries must return the same sources, including percent-escape query encoding; distinct DOI names must remain distinct.

Rebuild keys without changing either JSON file; SCI keys equal targets. Test nullable relation/reason, external/unavailable outgoing targets, and lookups limited to accepted local SCI targets. Display correct DOI links without changing signed targets.

## Public catalog

Through anonymous HTTP, test list/detail, full-text/category/publisher filters, incoming/outgoing citations, source embedding, distinct-source counts, and count ordering. Count repeated/equivalent targets once per source. Include only currently accepted publications in views, relationships, and counts. Test keyset pagination during arrivals and complete citation retrieval through bounded pages.

Deny public INSERT/UPDATE/DELETE, private columns/tables, internal functions, arbitrary SQL, and alternate-profile access to operational data. Check page caps, timeouts, rate limits, and representative query plans. Configure CLI/browser through the static descriptor without accounts. Stop Python: browsing must continue while PostgreSQL/PostgREST and R2 remain available. Then stop the catalog and database too: `pull UUID` must still succeed using only the static descriptor and archive. Confirm that pull makes no catalog request and sends no Supabase key to the archive. Restore catalog definitions and grants after archive rebuild.

## Safe rendering and delivery

Verify these implementation settings and browser behaviors:

- Render titles, abstracts, captions, keywords, citation reasons, filenames, and displayed API errors with text nodes or `textContent`. Test markup in each field. It must appear literally and must not enter scripts, styles, or event handlers.
- Build citation and download links from validated identifiers and URLs. Keep labels as text.
- Use `markdown-it` with `html: false` and its built-in `image` rule disabled, followed by DOMPurify with `USE_PROFILES: { html: true }` and `FORBID_TAGS: ['img']`. Only sanitized output can enter the README HTML container. Retain the parser's unsafe-URL checks. Do not transform the sanitized HTML afterward.
- Start without Markdown plugins, custom rendering hooks, or syntax-highlighting extensions. Use the established libraries; do not implement a custom parser or sanitizer.
- Bundle pinned browser distributions of both libraries with the static website. Keep them updated for security fixes. No frontend framework or build step is required.
- Verify normal README headings, lists, tables, code blocks, prose references to figures, and ordinary external links. Test inline and reference-style Markdown images targeting the headline figure, ZIP paths, external URLs, and data URLs. None may produce an image element in the README or trigger an image request. Test embedded HTML, event-handler attributes, and unsafe link and image URLs; these inputs must not execute content. The separately rendered headline figure must still load. Confirm that viewing references to supporting figures never fetches or extracts the ZIP or rewrites bundled-file paths. Image syntax alone must not cause signing or admission rejection, and stored README bytes must remain unchanged.
- If either rendering dependency is unavailable or fails, display the README through `textContent`.
- Confirm that display transformations do not change signed files in storage or downloads.
- Use a restrictive Content Security Policy that permits only required script, connection, and image sources. Disable inline script execution and embedded objects.
- Serialize catalog and publishing API responses, including errors, as JSON with `Content-Type: application/json`. Preserve original strings without HTML pre-escaping.
- Check actual response content types and `X-Content-Type-Options: nosniff` on APIs and public objects. Serve raw README and license files as UTF-8 text, never `text/html`. Serve ZIP files as downloads. Upload request headers alone do not verify these response properties.
- Render `files_tree` as plain text, without Markdown or executable links. Verify both the truncated and unavailable labels. Display must not request or extract the ZIP, offer individual-file downloads, or imply server inspection.
- Check CORS for the descriptor, catalog, and public objects, including profile/key headers and README requests. Confirm that frontend files contain no private credentials.

## Direct-upload size enforcement

Before deployment, test whether R2 rejects PUTs when the actual length differs from the exact presigned length. Test omitted headers, changed headers, and repeated uploads. Record whether storage enforces the declared size before relying on a hard upload cap. Follow the unresolved deployment choice in [DESIGN.md](DESIGN.md#limits-and-the-remaining-r2-check) if enforcement fails.

## Legacy import

After the checks above pass, test corpus migration with a small sample before importing the complete corpus. Preserve valid, nonconflicting UUIDv4s and recorded dates where possible. Verify explicit mappings for other legacy IDs and their citation references.

Convert legacy bare SCI citation UUIDs to `sci:` targets only during import. Never rewrite signed publications. Require the signed import-attestation format to be defined before migrating unsigned historical records.

## Open items

- **R2 upload size enforcement.** Not yet tested against real R2. Presigned
  URLs carry no size-binding header by default; after testing, set
  `SCI_PRESIGN_CONTENT_LENGTH=true` to sign `Content-Length`. The server
  always checks sizes and hashes after upload.
- **Legacy corpus import** waits for the signed import-attestation format.
