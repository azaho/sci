# SCI protocol specification

Status: draft 0.1. Date: 2026-11-04.

This draft defines signed publications, file retrieval, and catalog exchange.
It is a proposal. The current SCI software does not implement this format.
The existing format uses `schema_version: 1`; this draft uses a different schema name.
Do not use draft records as the only copy of a permanent publication.

`MUST` states a requirement. `MUST NOT` states a prohibition.
`SHOULD` states a recommendation. `MAY` states an option.
These words have the meanings in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119.html).
Other uppercase text, such as a filename, is not a requirement word.

## 1. Scope and roles

SCI lets a publisher sign a publication without permission from a central service.
Other parties can store, find, and verify that publication.

| Term | Meaning |
| --- | --- |
| Publisher | The party that signs a publication. |
| Reader | A program that retrieves and verifies publication files. |
| Archiver | A service that stores and serves verified publication objects. |
| Indexer | A service that derives a searchable catalog from verified records. |
| Object | An exact sequence of bytes, identified by its SHA-256 hash. |
| Inventory | The complete list of logical file paths, hashes, and byte counts. |
| Manifest | The signed record that identifies the publisher and inventory. |
| Location record | Untrusted information about where objects can be retrieved. |

One program can perform more than one role.
A reader MUST be able to verify local files without an indexer or archiver.
The Python and JavaScript libraries implement the same byte formats and verification rules.
Database schemas, deployment tools, and library function names are outside this specification.

Service policy is separate from format validity.
An operator can restrict storage, accepted licenses, content, or publishing rate.
An operator can also choose which publications to list or retain.
A service MUST NOT describe a policy refusal as a failed signature or hash check.
Curation and scientific ranking are outside this specification.

## 2. Identity and trust

The publisher identity is `ed25519:` followed by the lowercase SHA-256 hash of the raw public key.
The public key has 32 bytes. Do not hash its JSON or PEM representation.
A name or biography does not establish control of a key.
A signature establishes control of a key, subject to the security of that key and algorithm.
It does not establish authorship, scientific correctness, content safety, or license ownership.

A publication has two identifiers:

- `uuid`: a publisher-selected UUIDv4, for links and citations.
- Content identifier: `sha256:` followed by the lowercase SHA-256 hash of the exact manifest bytes.

A new publisher-generated UUID MUST use a secure random source.
Its text MUST use lowercase hexadecimal in the standard `8-4-4-4-12` form.
The version bits MUST be 4. The variant bits MUST be binary `10`.
A verifier checks the format and bits; it cannot check how the random value was obtained.

Different manifest hashes mean different records, even when their UUIDs match.
An indexer MUST retain evidence of an observed UUID conflict.
It MUST NOT merge the records or choose a winner from an author-supplied date.
A UUID-only lookup MUST report ambiguity when that indexer knows multiple valid records.
An exact content identifier selects one record without resolving the UUID conflict.
The same verified manifest hash is an idempotent submission.

Corrections MUST be new publications with new UUIDs.
A mirror MUST preserve the original manifest, signature, and file bytes.
It MUST NOT rename a publication by changing its signed UUID.

## 3. Shared encoding rules

### 3.1 JSON

JSON documents MUST follow [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html), with these additional restrictions:

- Use UTF-8 without a byte-order mark. The root MUST be an object.
- Reject duplicate object keys after escape decoding, at every depth.
- Reject trailing non-whitespace data, invalid UTF-8, and unpaired UTF-16 surrogate escapes.
- Reject `null`, floating-point numbers, and unknown fields.
- Integer tokens MUST match `0|[1-9][0-9]*`. Their values MUST NOT exceed `9007199254740991`.
- Reject decoded string code points U+0000–U+001F, except TAB, LF, and CR where explicitly permitted.
- Reject U+007F–U+009F and Unicode noncharacters U+FDD0–U+FDEF and U+nFFFE/U+nFFFF, for n=0 through 16.
- Object and array nesting MUST NOT exceed eight containers, including the root container.

Each section lists all permitted fields and types. Required fields cannot be omitted.
An optional field MUST be omitted when absent. Empty arrays are permitted unless stated otherwise.
Boolean values are not integers. Array order is preserved.
JSON object order and legal JSON whitespace have no semantic meaning.
However, all original bytes remain significant to signatures and hashes.

A verifier MUST NOT trim, normalize, repair, or reserialize bytes before checking them.
A creator MAY choose any serialization that meets these rules.
The example uses sorted keys and compact JSON for reproducibility; this is not a canonicalization requirement.

### 3.2 Descriptors and dates

An object descriptor has exactly these required fields:

| Field | Type | Rule |
| --- | --- | --- |
| `sha256` | String | Exactly 64 lowercase hexadecimal characters. |
| `bytes` | Integer | Number of bytes in the object, including any final newline. |

Hash raw object bytes, before text decoding or image rendering.
Two descriptors with the same hash but different sizes are inconsistent and MUST be rejected within one record.

`created_at` MUST be a real Gregorian date and UTC time in `YYYY-MM-DDTHH:MM:SSZ` form.
Years are 0001–9999. Hours are 00–23. Minutes and seconds are 00–59.
Fractions, timezone offsets, and leap seconds are not permitted in this draft.
The date is an author claim. An old or future date does not invalidate a publication.

## 4. Manifest and signature

The manifest has exactly these required fields:

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | String | Exactly `sci-publication/0.1`. |
| `uuid` | String | UUIDv4 under section 2. |
| `public_key` | Object | Public JWK defined below. |
| `created_at` | String | Date under section 3.2. |
| `inventory` | Object | Descriptor of the inventory bytes. |

The manifest MUST NOT exceed 65,536 bytes.
The inventory descriptor is the root of the file hash chain.
No host URL, acceptance time, rank, or moderation status belongs in the manifest.

The public JWK has exactly three required string fields: `kty`, `crv`, and `x`, as described in
[RFC 8037](https://www.rfc-editor.org/rfc/rfc8037.html).
`kty` MUST equal `OKP`. `crv` MUST equal `Ed25519`.
`x` MUST encode the raw 32-byte public key as base64url, without padding or whitespace.
Decode and re-encode `x`; reject the value if the text changes.
Private key fields are forbidden.

Use pure Ed25519 from [RFC 8032](https://www.rfc-editor.org/rfc/rfc8032.html).
The signed message is this prefix followed by the exact manifest bytes:

```text
SCI-PUBLICATION\0sci-publication/0.1\0
```

Each `\0` denotes one zero byte. All other prefix characters are ASCII bytes.
Do not add a newline, prehash the message, or use Ed25519ph or Ed25519ctx.
The detached signature is exactly 64 raw bytes.

The verification profile requires canonical point encodings for the public key A and signature point R.
Both points MUST be nonidentity members of the prime-order subgroup.
The signature scalar S MUST be less than the subgroup order L.
Check the equation `[S]B = R + [k]A` from RFC 8032 section 5.1.7.
These restrictions select one acceptance rule for both libraries.
Library default behavior alone is not a conformance test.
Boundary fixtures for this profile are required before the libraries are released.

## 5. Inventory and logical files

The inventory has exactly two required fields:

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | String | Exactly `sci-inventory/0.1`. |
| `files` | Object | Logical paths mapped to object descriptors. |

The inventory MUST NOT exceed 67,108,864 bytes or 250,000 file entries.
These are parser bounds, not a promise that a service accepts this much data.
The sum of all declared file sizes MUST NOT exceed `9007199254740991`.
Count each logical entry, including entries that share identical bytes.

The required paths are `sci.json`, `README.md`, and `LICENSE`.
`headline_figure.png` is optional. Other files MUST be under `files/`.
Do not list the manifest, its signature, or the inventory itself.
Do not include directory entries, links, file modes, or execution flags.
Empty files are permitted only under `files/`.

### 5.1 Paths

Logical paths MUST meet all these rules:

- Use ASCII. Use `/` between components.
- Use only letters, digits, `.`, `_`, and `-` inside components.
- Use 1–64 bytes per component and at most 240 bytes for the complete path.
- Reject empty components, `.` and `..`, and components that end with a dot.
- Reject absolute paths, backslashes, and a final slash.
- Reject Windows device names, without regard to ASCII letter case.
  These are `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, and `LPT1`–`LPT9`, including names followed by an extension.

After ASCII lowercase conversion, no two paths may be equal.
No file path may be a directory prefix of another path, with the same comparison.
For example, `files/a` and `files/A/b.txt` conflict.

A disk writer MUST stay within its selected destination directory.
It MUST refuse existing symbolic links and conflicting files in the destination path.
The writer MUST prevent path replacement races, or require an isolated destination under its exclusive control.
A creator MUST reject symbolic links and nonregular input files.
No verification operation may execute publication content.

### 5.2 Core file contents

`sci.json` contains publication metadata under section 6.
Its size MUST NOT exceed 65,536 bytes.

`README.md` contains the main publication text in UTF-8 Markdown.
`LICENSE` contains the license text and its scope in UTF-8 text.
Each MUST contain a non-whitespace character and MUST NOT exceed 1,000,000 bytes.
Neither may start with a byte-order mark.
Both follow the string code point restrictions in section 3.1, with TAB, LF, and CR permitted.

The license MUST state terms that cover all author-supplied publication content.
It MUST identify any different terms and the files or material to which they apply.
Keep required attribution and third-party notices with the publication.
A verifier can check the license bytes and encoding. It cannot prove ownership or completeness of the rights claim.
An indexer MAY restrict admission to particular license bytes or terms.
Those restrictions MUST be documented as service policy.

`headline_figure.png`, when present, MUST be a static PNG.
It MUST NOT exceed 2,000,000 bytes or 1,600 pixels on either axis.
Each axis MUST have at least one pixel. Animated PNG is not permitted.
PNG structural validation and safe decoding belong to the rendering or admission profile.
Hash verification alone MUST NOT be reported as successful image validation.

### 5.3 Large file sets and archives

The inventory can contain many files without increasing the manifest size.
An inventory is one hashed object in this draft; it is not a Merkle tree.
A reader MUST retrieve and verify the complete inventory before it uses an entry.

A publisher MAY include a ZIP or another archive as an ordinary file under `files/`.
The descriptor authenticates the archive bytes, not individual members.
Readers and archivers MUST NOT extract such an archive as part of protocol verification.
A service can require uncompressed ZIP files and additional inspection as an admission policy.
Those checks do not establish that the contents are safe to execute.

This draft does not define packed transport for individually listed files.
Byte offsets, range retrieval, compression rules, and per-member proofs require a separate extension.

## 6. Publication metadata

`sci.json` has these fields:

| Field | Required | Type and rule |
| --- | --- | --- |
| `title` | Yes | Nonblank string, at most 200 Unicode code points. |
| `abstract` | Yes | Nonblank string, at most 2,500 code points. |
| `categories` | No | At most three distinct, nonblank strings, each at most 64 code points. |
| `keywords` | No | At most 20 nonblank strings, each at most 64 code points. |
| `headline_figure_caption` | No | Nonblank string, at most 400 code points; requires the figure entry. |
| `citations` | No | At most 1,000 citation objects, defined below. |

TAB, LF, and CR are permitted only in `abstract` and citation `reason`.
Other metadata strings MUST also reject U+2028 and U+2029.
Count decoded Unicode code points, not UTF-16 units or rendered characters.
Do not apply Unicode normalization.
Category equality uses exact decoded strings. Preserve category order.
An unfamiliar category is valid protocol data; a service may apply its own taxonomy.

For nonblank checks, whitespace is U+0009–U+000D, U+0020, U+0085, U+00A0, U+1680,
U+2000–U+200A, U+2028, U+2029, U+202F, U+205F, and U+3000.
The code point restrictions still apply.

A citation has required `target` and optional `relation` and `reason` fields.
`relation`, when present, MUST be `uses`, `supports`, `reproduces`, `challenges`, or `supersedes`.
An absent relation makes no claim about the type of use.
`reason`, when present, is nonblank text of at most 1,000 code points.

`target` MUST use one of these exact forms:

- `sci:<uuid>`: a UUID under section 2. This reference can be ambiguous.
- `sci:sha256:<hash>`: a manifest hash under section 3.2. This selects exact publication bytes.
- `doi:<name>`: the restricted DOI form below.

For this draft, the complete `<name>` MUST match `10\.[0-9]{4,9}/[A-Za-z0-9._;()/:+%-]+`.
Each `%` MUST be followed by two hexadecimal digits.
The entire target MUST NOT exceed 512 ASCII bytes.
This is a restricted transport syntax, not proof of DOI registration or a complete DOI syntax definition.
Do not resolve a DOI during verification.
For this draft, citation equality uses the exact decoded target string.
DOI normalization, aliases, and registration checks are indexer features, not signature semantics.

## 7. Verification results

A verifier MUST identify the checks that it completed.
It MUST NOT use one Boolean result to imply that unavailable files were checked.

| Result | Required checks |
| --- | --- |
| `manifest_verified` | Manifest encoding, schema, signature, and any caller-supplied expected manifest hash. |
| `metadata_verified` | Manifest; full inventory hash and schema; metadata, README, and license hashes and content rules. |
| `complete_verified` | All metadata checks and the size and hash of every declared file. |

Each result MUST include the content identifier and publisher fingerprint.
Metadata and complete results MUST also identify the logical paths whose bytes were verified.
Content checks, such as PNG validation, MUST be reported separately.
Admission status, availability, revocation knowledge, and moderation status are also separate results.

Use this dependency order:

1. Apply local resource limits. Parse and validate the bounded manifest.
2. Check the expected content identifier, if supplied, and the signature.
3. Retrieve the inventory. Check its byte count and hash before parsing it.
4. Validate the inventory schema, paths, counts, and descriptor consistency.
5. Retrieve core files. Check each byte count and hash before decoding and validating its contents.
6. For each additional requested file, check its byte count and hash.
7. Report exactly which checks passed and which files remain unchecked.

For remote content, stop reading when a byte count exceeds its descriptor or a local limit.
A short read fails the byte count check. Do not trust HTTP `Content-Length` alone.
Large objects SHOULD be hashed in bounded chunks.
An implementation MUST NOT allocate memory from an unchecked size claim.

The common result categories are:

| Category | Meaning |
| --- | --- |
| `unsupported_schema` | The schema name is not supported. |
| `invalid_encoding` | JSON, UTF-8, or key encoding violates its rules. |
| `invalid_structure` | Fields, values, paths, or protocol bounds violate this draft. |
| `invalid_signature` | Signature encoding, point checks, or signature equation fails. |
| `size_mismatch` | Retrieved byte count differs from its descriptor. |
| `hash_mismatch` | Retrieved bytes differ from the expected hash. |
| `unavailable` | Required bytes could not be retrieved. |
| `limit_exceeded` | A local resource limit prevented a check. |
| `policy_rejected` | A service declined otherwise verifiable input under its policy. |
| `uuid_conflict` | Distinct verified manifests claim one UUID. |

Errors MUST identify the affected field or object when known.
When several rules fail, implementations MAY report any applicable failure at the earliest failed dependency stage.
Conformance fixtures MUST specify the operation and expected result, not just “valid” or “invalid”.

## 8. Locations and HTTP retrieval

Object identity is independent of location.
An ordinary HTTPS host can serve the bytes. No upload API is required for that host.
Git repositories, object stores, and dedicated archivers can use different upload methods.

A location record has exactly these required fields:

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | String | Exactly `sci-location/0.1`. |
| `manifest` | Object | Descriptor of the manifest. |
| `signature_urls` | Array | One or more URL strings for the detached signature. |
| `objects` | Array | Source objects defined below. |

Each source object has exactly three required fields: `sha256`, `bytes`, and `urls`.
The first two fields form a descriptor. `urls` is a nonempty array of distinct URL strings.
There MUST be one source object for the manifest descriptor.
Other source objects can describe the inventory or any declared file.
A hash MUST appear at most once in `objects`.
A record can omit unavailable objects; it does not prove complete storage.

URLs MUST be absolute HTTPS URLs without user information or fragments.
Each URL MUST NOT exceed 4,096 UTF-8 bytes.
HTTP is permitted only through an explicit local-development option, outside the public exchange format.

These records are untrusted retrieval hints. They are not publisher statements.
Any party MAY publish or update them without the publisher's private key.
For inventory and file retrieval, the signed hash chain supplies the expected descriptor.
A differing location descriptor MUST NOT replace it.
An untrusted location record alone cannot establish which publication a user intended to retrieve.
A reader SHOULD use a previously known manifest hash or an explicit indexer trust choice for that selection.

Use a complete HTTP GET response for each object. A successful response has status 200.
Hash the response body after HTTP transfer and content decoding, without further transformation.
Content type, filename headers, ETags, and TLS do not replace the hash check.
This draft does not use partial responses or range requests.

On an unavailable or incorrect source, a reader MAY try another URL for the same expected object.
Limit retries, transfer size, and elapsed time.
If redirects are followed, apply the same URL and network policy at every hop.
Server-side fetchers MUST block access to private, loopback, link-local, and reserved network destinations by default.
They MUST enforce this at connection time, including after DNS resolution and redirects.
Local network use requires an explicit administrator configuration.
Do not send ambient credentials to a mirror.

Browser-readable mirrors MUST permit cross-origin GET requests from the reader through CORS.
A CORS failure means that browser retrieval failed; it does not prove incorrect object bytes.
A reader MUST NOT use an opaque response as verified content.

## 9. Archiver and indexer behavior

### 9.1 Archiver

An archiver MUST verify every object before it advertises that object as verified.
It MUST publish an object only after the complete expected byte count and hash pass.
If staging and serving use different copies, the archiver MUST verify the final bytes or use an immutable, verified source version.
An ETag comparison alone does not meet this requirement.
Interrupted writes MUST NOT expose incomplete objects under a verified object location.
Repeated retrieval or submission of the same object MUST NOT change its bytes.

An archiver can retain only selected files. It MUST identify that scope.
It can report complete storage only when it holds the manifest, signature, inventory, and every declared file.
It MUST also complete metadata validation before it reports a complete verified publication.
Deleting a local copy does not revoke the publication or delete other copies.
A retention promise is an operator policy, not a consequence of a hash.

### 9.2 Indexer

An indexer MUST complete `metadata_verified` before it presents author metadata as verified.
It can index a publication whose supporting files are unavailable.
It MUST distinguish that state from complete verification and complete archival storage.
Derive author fields only from the verified `sci.json` bytes.
Store original signed records separately from derived database fields.
An indexer MUST NOT rewrite a signed record to apply a correction or moderation decision.

No indexer can claim knowledge of every publication or every UUID conflict.
Reports of incorrect mirrors are hints. They MUST NOT be treated as proof of incorrect bytes.
Moderation decisions and scientific rankings MUST be labeled as service decisions.
An indexer MAY enforce an operator block without claiming that a publisher revoked a key.

### 9.3 Catalog exchange

A public catalog export has exactly two required fields: `schema` and `publications`.
`schema` MUST equal `sci-catalog/0.1`.
`publications` is an array of location records, with at most one record per manifest hash.
For an indexer's export, each entry MUST have passed that indexer's metadata verification.
An importer MUST still verify the records independently.

An export is a discovery snapshot. It does not carry authoritative admission, revocation, or deletion state.
Rebuilding this draft's public metadata index requires the export and the referenced core objects.
An export whose referenced objects are missing is not a complete backup.
Private reports, credentials, upload tokens, and operational tables MUST NOT appear in this export.

Pagination, live search, incremental synchronization, signed checkpoints, and transparency proofs are deferred.
An export date or an author signature does not establish a trusted publication time.
An old but valid export does not prove that it is the newest export.

## 10. Reader display

A reader MUST verify a README or figure against the signed hash chain before it displays that file.
It MUST also apply its content validation and rendering policy.
Render Markdown with HTML disabled or sanitized. Do not execute embedded scripts.
Do not load external images or other embedded resources automatically from publication text.
Treat titles, captions, and link labels as text.
The reader's URL policy MUST reject executable link schemes.

Do not describe a verified hash as proof that content is safe.
Extra retrieval for volunteer verification requires explicit user opt-in.
Such retrieval MUST have stated resource limits and MUST NOT execute or extract content.

## 11. Existing SCI publications

The existing format uses `schema_version: 1`, an inline file map, and an unprefixed manifest signature.
Its fixed publication license and other original validation rules still apply to those records.
This draft does not change those rules.

Select the format from the parsed version or schema field.
Do not try another signature format after verification fails.
Reject a record that mixes the old version field with the new schema field.
Libraries that support both formats MUST use distinct verification paths.

Mirror old records with their original UUIDs, bytes, signatures, and license.
Use their original manifest hashes as content identifiers.
Do not convert an old record by rewriting and signing it as its original author.
An author can issue a new publication that cites the old one.

Unsigned historical records remain unsigned.
An importer's signature can attest to an import, but cannot become the original author's signature.
An import-attestation format is not defined in this draft.
Old-format catalog transport and such attestations require a migration extension.

## 12. Conformance and release

The [worked example](spec/examples/README.md) contains exact bytes and expected hashes.
It uses a public test key. Never use that key for real publications.
The fixture checker validates the example's hash chain and signature; it is not a protocol library.

The Python and JavaScript libraries MUST pass the same versioned conformance fixtures.
They MUST verify each other's generated publications.
Fixtures MUST cover valid alternative JSON layouts and rejected ambiguous encodings.
They MUST also cover signature boundaries, path conflicts, missing files, altered files, and local resource limits.
Service checks MUST cover interrupted storage, mirror fallback, catalog reconstruction, and UUID conflicts.

A change to accepted bytes or their meaning requires a new draft schema name and updated fixtures.
Editorial changes do not require a new schema name.
Before a stable release, an independent implementation MUST demonstrate the documented workflow from the specification and fixtures.
The [decision list](spec/decisions.md) records proposals and work still required.
