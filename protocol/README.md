# SCI

SCI is an open, decentralized research publication system. Anyone can publish signed research, host copies, or build tools to find and verify publications. A shared protocol lets these independent services work together.

# SCI Protocol Specification

Protocol version: **0.1**. This draft may change before release.

`MUST` states a requirement. `MUST NOT` states a prohibition.
`SHOULD` states a recommendation. `MAY` states an option.
These words have the meanings in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119.html).

## 1. Roles

| Role | Meaning |
| --- | --- |
| Publisher | The party that signs a publication. |
| Reader | A program that retrieves and verifies publication files. |
| Archiver | A service that stores and serves verified publication objects. |
| Indexer | A service that derives a searchable catalog from verified records. |

One program can perform more than one role.
A reader MUST be able to verify local files without an indexer or archiver.

## 2. Publication bundle

A publication is identified by the SHA-256 hash of its exact manifest bytes.
The identifier is `sha256:` followed by 64 lowercase hexadecimal characters.

Corrections MUST be new publications with new manifest hashes.
A correction MAY cite the original with `supersedes` in its `relations` array.

Paths are relative to the bundle root.

| Path | Required? | Purpose |
| --- | --- | --- |
| `manifest.json` | Yes | Publisher’s public key and file list. |
| `manifest.json.sig` | Yes | Publisher’s signature, defined in [section 3](#3-manifest). |
| `sci.json` | Yes | Publication metadata defined in section 4, including any display items. |
| `README.md` | Yes | Main publication text as UTF-8 Markdown, following the text character rules in section 4. |
| `LICENSE` | For baseline | License terms supplied by the publisher as UTF-8 text, following the text character rules in section 4. |
| `files/<path>` | No | Supporting files, such as `files/results.png` or `files/data.csv`. |

`README.md` MUST be nonblank under [section 4.2](#42-text-characters).

Publishing tools MUST NOT include the publication's signing private key in the bundle.
Publishing tools SHOULD offer timestamping by default and allow the publisher to skip it or choose a timestamp authority.

A publication may be distributed as individual files at the paths above or as a single uncompressed ZIP. A publisher or archiver may assemble the ZIP from the signed manifest, its signature, and the listed files.

An optional `manifest.json.sig.tsr` sidecar contains timestamp proofs under [section 6.8](#68-timestamp-verification) for the SHA-256 of the exact bytes of `manifest.json.sig`. It MAY accompany the loose files but is not part of the bundle and MUST NOT be included as a ZIP entry. When distributed alongside a named ZIP, the sidecar SHOULD use `<digest>.tsr`, with the same digest as `<digest>.zip`. Archives serve the sidecar through `entry_url` as specified in [section 8.2](#82-archive-descriptor).

### 2.1 Baseline publications

The baseline defines a shared compatibility guarantee. SCI indexers and archivers MUST NOT reject publications meeting its requirements because of per-publication resource limits or the approved license terms, while other admission policies remain applicable under [section 6.9](#69-service-policy). Publications outside the baseline remain valid if they satisfy the other format rules; services MAY decline them under their published policy.

A valid publication is baseline if it satisfies every limit below and includes `LICENSE` with the exact bytes of one of the approved licenses in this table. Hashes are SHA-256 over the exact license file bytes.

| Approved license | Short description | SHA-256 |
| --- | --- | --- |
| [SCI baseline license 0.1](sci-baseline-LICENSE.txt) | CC BY 4.0 for text, figures, and data; MIT for code. | `8ccfd5fdaa93e5e71dc8812bf11e0a7198bd0a534fa64b8970fd2de58e6c2478` |

| Quantity | Maximum |
| --- | --- |
| Complete ZIP under section 2.2 | 64 MiB (67,108,864 bytes) |
| Entries in the manifest's `files` object | 1,000 |
| Items in `sci.json`'s `display` array, if present | 3 |
| `manifest.json` | 256 KiB (262,144 bytes) |
| `sci.json` | 64 KiB (65,536 bytes) |
| `README.md` | 1 MiB (1,048,576 bytes) |

Display files in baseline publications MUST meet these requirements:

| Extension | Maximum file size | Requirements |
| --- | --- | --- |
| `.png` | 2 MiB (2,097,152 bytes) | At most 8,000,000 pixels; width and height each from 1 to 8,192 pixels.<br><br>For baseline checks, use the signature, chunk layout, and `IHDR` dimensions defined in [PNG, Third Edition](https://www.w3.org/TR/2025/REC-png-3-20250624/). The file MUST start with the PNG signature, followed by one `IHDR` chunk with 13 data bytes. Complete chunks MUST account for all bytes after the signature and end with one empty `IEND` chunk. No other `IHDR` or `IEND` chunk may occur. The chunks `acTL`, `fcTL`, and `fdAT` MUST NOT occur. Baseline checks do not require CRC verification, image decoding, or other PNG conformance checks. |

Compare the extension without regard to ASCII case.

For a publication distributed as loose files, use the size of the exact ZIP defined in section 2.2. Tools MAY calculate this size without constructing the ZIP:
```text
zip_bytes = 22 + sum(file_bytes + 76 + 2 * path_bytes)
```
The sum covers every ZIP entry, including `manifest.json` and `manifest.json.sig`. `file_bytes` is the entry's content length in bytes; `path_bytes` is its full relative ASCII path length in bytes.
Indexers and archivers MUST be able to complete the bundle checks in section 6.4 for baseline publications. They MUST NOT impose smaller limits that exclude otherwise valid baseline publications.

Publishing tools MUST check the baseline requirements before signing. They SHOULD require an explicit publisher override before signing a publication that does not meet them.

### 2.2 Single-file bundle (ZIP)

When distributing a ZIP as a named file, use `<digest>.zip`, where `<digest>` MUST be the 64 lowercase hexadecimal characters of the manifest’s SHA-256 hash.
SCI readers MUST support this ZIP representation and be able to verify its contents even if the file has been renamed.

To save an individual file, a reader MUST verify the manifest and its signature, then check the file’s byte count and SHA-256 hash against the requested path in the manifest.
Before unpacking the whole bundle, a reader MUST verify the complete bundle as defined in [section 6.4](#64-complete-bundle-checks), including all of the ZIP requirements below.
The record layouts are defined in the [ZIP specification, sections 4.3–4.4](https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT).

- Starting at byte zero, store consecutive local file headers and their data, then the corresponding central directory file headers in the same entry order, then one end-of-central-directory record ending at the last byte. These records and file data MUST account for every byte.
- The first two entries MUST be `manifest.json` and `manifest.json.sig`, in that order. Next, include each file listed in the manifest exactly once, in ascending lexicographic order of its full ASCII path bytes. Omit enclosing directories, explicit directory entries, and other files.
- For each file listed in the manifest, the filename bytes in both ZIP headers MUST exactly match the ASCII bytes of its decoded `files` key.
- Use classic ZIP in a single part. All disk-number fields MUST be zero. Use method 0 (stored) and zero general-purpose flags. Extra fields and comments MUST be empty. ZIP64 records, ZIP64 sentinel values, and data descriptors are prohibited in this version of the schema.
- Set the complete version-made-by field to `0x000A` (MS-DOS host, version 1.0), the version needed to extract to 1.0 (10), and both internal and external file attributes to zero. Every entry represents an ordinary file.
- In both headers, set the last-modification date to `0x0021` and time to `0x0000` (1980-01-01 00:00:00 in DOS format).
- Each central entry MUST point to the start of its corresponding local header. All fields shared by the two headers MUST agree byte for byte. Both sizes MUST equal the actual data length, and the CRC-32 MUST match the data. The end-of-central-directory record MUST give the actual entry count in both count fields, the byte offset where the central directory starts, and its length in bytes.

These rules determine the exact ZIP bytes from the exact manifest bytes, signature bytes, and listed file contents. The publication identifier remains the hash of the manifest, not the ZIP.

## 3. Manifest

The publisher signs `manifest.json`, a [JSON](#7-json-encoding-rules) object with exactly these required fields:

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | String | Exactly `sci-publication/0.1`. |
| `public_key` | Object | Ed25519 public JWK as defined in [section 5.1](#51-keys-and-signatures). |
| `files` | Object | Maps each listed path to its SHA-256 hash and byte count. |

`manifest.json.sig` contains the signature over the exact bytes of `manifest.json`, using the key in `public_key`.
`manifest.json.sig` MUST contain exactly the raw 64 signature bytes, without text encoding or surrounding bytes.
The signature follows [section 5.1](#51-keys-and-signatures).

Publishers SHOULD write the members of `files` in ascending lexicographic order by the ASCII bytes of the full path.
Each path in `files` maps to a JSON object with exactly two fields:

- `sha256` is the SHA-256 hash of the file’s exact bytes, written as a string of 64 lowercase hexadecimal characters.
- `bytes` is the file’s length in bytes, written as a decimal integer from 0 through `9007199254740991`, using digits only and no leading zeros.

The manifest’s `files` field MUST list `sci.json` and `README.md`. It MUST also list `LICENSE` if supplied. All other entries MUST be paths under `files/`.
A complete bundle MUST contain exactly the listed files plus the manifest and its signature.

### 3.1 File paths

Paths MUST be relative to the bundle root. Use `/` to separate components.
The full path relative to the bundle root MUST be at most 255 ASCII bytes, including `/` separators.
Each component MUST contain one or more characters from the POSIX portable filename character set: ASCII letters, digits, period (`.`), underscore (`_`), and hyphen (`-`).

Reject `.`, `..`, and components ending in a period.
Reject [Windows device names](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file) in any component: `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, and `LPT1`–`LPT9`, including names followed by an extension. Compare these names without regard to ASCII case.

Compare paths without regard to ASCII case when checking for conflicts. Two file paths MUST NOT be equal, and a file path MUST NOT also name a directory used by another entry. For example, `files/a` conflicts with both `files/A` and `files/A/b.txt`.
Every occurrence of the same directory path MUST use the same capitalization. For example, `files/Data/a.csv` and `files/data/b.csv` conflict.

## 4. Publication metadata

`sci.json` is a JSON object that permits only these fields:

| Field | Required | Type and rule |
| --- | --- | --- |
| `title` | Yes | Nonblank string, at most 200 Unicode code points. |
| `abstract` | Yes | Nonblank string, at most 2,500 code points. |
| `categories` | No | An array of at most three category objects, defined below. |
| `keywords` | No | An array of at most 20 nonblank strings, each at most 64 code points. |
| `display` | No | Ordered array of files the publisher wants readers to show first. |
| `citations` | No | An array of at most 1,000 citation objects, defined below. |

### 4.1 Categories

Each category object contains exactly two required string fields:

| Field | Rule |
| --- | --- |
| `scheme` | Classification scheme identifier matching `[a-z][a-z0-9.-]{0,63}` in its entirety. |
| `code` | Nonblank category code, at most 128 Unicode code points. |

Scheme identifiers and category codes are compared exactly. Duplicate `(scheme, code)` pairs are prohibited.
Within each scheme, entries are ordered by publisher-assigned relevance.

Publishers SHOULD use the following identifiers when referring to these classification schemes:

| Identifier | Classification scheme |
| --- | --- |
| `arxiv` | [arXiv Category Taxonomy](https://arxiv.org/category_taxonomy) |
| `anzsrc-for-2020` | [ANZSRC 2020 Fields of Research](https://www.abs.gov.au/statistics/classifications/australian-and-new-zealand-standard-research-classification-anzsrc/2020) |

### 4.2 Text characters

All decoded strings in `sci.json`, including object keys, and the decoded contents of `README.md` and `LICENSE` MUST contain only Unicode Assignables as defined in [RFC 9839 §4.3](https://www.rfc-editor.org/rfc/rfc9839.html#section-4.3).

TAB (U+0009), LF (U+000A), and CR (U+000D) are permitted only in `abstract`, citation `reason`, `README.md`, and `LICENSE`.

Implementations MUST reject text that violates these restrictions.

A nonblank string MUST contain at least one permitted character other than SPACE, TAB, LF, or CR.

### 4.3 Display items

Each `display` item is an object with a required `path` and an optional `caption`. Only these fields are permitted.
Each path MUST exactly match a distinct entry under `files/` in the signed manifest.
A caption MUST be a nonblank string of at most 2,000 Unicode code points.

### 4.4 Citations

A publication can cite other SCI publications by their publication identifiers or external work by DOI or URL.
Publishers SHOULD prefer SCI identifiers or DOIs over URLs when available, to keep citations independent of hosting locations.

A citation is a JSON object with required `target` and optional `relations` and `reason` fields. No other fields are permitted.
`relations`, when present, MUST be an array of distinct strings from the table below. Multiple relations may apply; their order has no meaning.
Each relation is a publisher claim about the citing publication's relationship to the cited work.

| Relation | Meaning |
| --- | --- |
| `uses` | Uses material, methods, or results from the cited work. |
| `reviews` | Substantively evaluates the cited work, without implying agreement or disagreement. |
| `supports` | Provides evidence or reasoning supporting a claim in the cited work. |
| `challenges` | Provides evidence or reasoning questioning a claim in the cited work. |
| `supersedes` | Is intended to replace the cited publication. |

An absent or empty `relations` array indicates an ordinary citation without a more specific relationship claim.
`reason`, when present, is nonblank text of at most 1,000 code points.

`target` MUST use one of these exact forms:

- `sci:sha256:<hash>`: the publication identifier from [section 2](#2-publication-bundle), prefixed with `sci:`.
- `doi:<name>`: a DOI URI conforming to the [DOI URI Scheme specification](https://doi.org/10.1000/292).
- An HTTP or HTTPS URI under [RFC 9110 section 4.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-4.2), with an optional fragment under [RFC 3986 section 3.5](https://www.rfc-editor.org/rfc/rfc3986.html#section-3.5).

DOI targets MUST use that specification's encoding and equivalence rules.

## 5. Publisher identity

A publisher is identified by its Ed25519 public key.
A signature establishes control of a key, subject to the security of that key and algorithm.
It does not establish correctness of the published claims, content safety, or license ownership.

**Warning:** Publishers MUST protect their private keys from unauthorized access. Anyone with a private key can sign publications and revoke that key.

### 5.1 Keys and signatures

Public keys MUST use the JWK representation defined in [RFC 8037](https://www.rfc-editor.org/rfc/rfc8037.html).
Only `kty`, `crv`, and `x` are permitted in a public key object.
Verifiers MUST reject noncanonical base64url encodings of `x`.

Signatures MUST use the 64-byte pure Ed25519 format defined in [RFC 8032](https://www.rfc-editor.org/rfc/rfc8032.html).
Signers MUST use the deterministic signing procedure in [RFC 8032 §5.1.6](https://www.rfc-editor.org/rfc/rfc8032.html#section-5.1.6).
Verifiers MUST check signatures according to [RFC 8032 §5.1.7](https://www.rfc-editor.org/rfc/rfc8032.html#section-5.1.7).
The public key `A` and signature point `R` MUST also be nonidentity members of the prime-order subgroup.

Each signed record type MUST use distinct `schema` values.
Before accepting a record, verifiers MUST check that its `schema` identifies the expected record type and a supported version.

### 5.2 External identity claims

A public key can claim affiliation with a domain or an external account.
Claims are optional. Each account can list many keys, and each key can name many accounts.
To verify the affiliation, the domain or account hosts a claim signed by the key that names that domain or account.
The key’s signature and the account’s hosted file show agreement from both sides.

Publishers and indexers can supply account hints for lookup. Hints do not prove an association.
Readers and indexers MUST complete the checks in section 6.6 before displaying an association as verified.
**Warning:** If a domain or account changes owners, the new owner can reuse the previous owner’s hosted claim until it expires.

#### 5.2.1 Signed claim

The account hosts the claim in a `.json` file and its signature in a matching `.json.sig` file.
The claim is a JSON object with only these fields:

| Field | Required? | Type and rule |
| --- | --- | --- |
| `schema` | Yes | String, exactly `sci-key-claim/0.1`. |
| `public_key` | Yes | Public JWK under section 5.1. |
| `account` | Yes | Object with exactly two string fields: `type` and `id`. The account method defines their values. |
| `name` | No | Publisher display name. Nonblank string, at most 80 Unicode code points. Uses the `title` character rules in section 4.2. |
| `note` | No | Nonblank string, at most 2,500 Unicode code points. Uses the `abstract` character rules in section 4.2. |
| `expires` | Yes | String, an expiry time in [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339.html#section-5.6) format. |

Publishers SHOULD set `expires` no more than 90 days after signing.
Expiry ends a claim's current status. A hosted claim still covers publications timestamped before `expires`, under section 6.6, so a retired key needs no renewal.
Readers and indexers MAY impose a shorter remaining validity period as local policy.
Readers and indexers MAY reuse a successful association check until the claim expires.

The claim JSON MUST follow section 7.
The `.json.sig` file contains the raw 64-byte signature defined in section 5.1.
The claim's key signs the exact `.json` bytes.
Readers MAY use the account's avatar or domain icon as an unsigned display image.

#### 5.2.2 Domain method

`account.type` MUST be `domain`.
`account.id` MUST contain only a lowercase ASCII DNS host name, without a final dot.
Internationalized labels MUST use valid [IDNA2008 A-labels](https://www.rfc-editor.org/rfc/rfc5890.html#section-2.3.2.1). IP addresses are not permitted.

The domain MUST host the claim and signature for each key at:

```text
https://<account.id>/.well-known/sci/keys/<public_key.x>.json
https://<account.id>/.well-known/sci/keys/<public_key.x>.json.sig
```

#### 5.2.3 GitHub method

`account.type` MUST be `github`.
`account.id` MUST be the GitHub account's login in lowercase, for a user or organization.
The account MUST host the files in either its public `<account.id>/<account.id>` or `<account.id>/.github` repository, on the `main` branch.
Both locations are permitted for users and organizations:
```text
https://raw.githubusercontent.com/<account.id>/<account.id>/main/sci/keys/<public_key.x>.json
https://raw.githubusercontent.com/<account.id>/<account.id>/main/sci/keys/<public_key.x>.json.sig
```
or:
```text
https://raw.githubusercontent.com/<account.id>/.github/main/sci/keys/<public_key.x>.json
https://raw.githubusercontent.com/<account.id>/.github/main/sci/keys/<public_key.x>.json.sig
```
Readers and indexers MAY also use the [GitHub API](https://docs.github.com/en/rest/repos/repos#get-a-repository) to check that the account still owns the repository.

#### 5.2.4 Hugging Face method

`account.type` MUST be `huggingface`.
`account.id` MUST be the user or organization name, with the spelling returned by the Hub API.
The account MUST host the files in its public `<account.id>/README` Space, on the `main` branch:

```text
https://huggingface.co/spaces/<account.id>/README/resolve/main/sci/keys/<public_key.x>.json
https://huggingface.co/spaces/<account.id>/README/resolve/main/sci/keys/<public_key.x>.json.sig
```

Readers and indexers MAY also use the Hub API to check that the account still owns the Space.

### 5.3 Key revocation

A key holder can revoke a key with a signed record.
Revocation is permanent.
A revocation consists of two required files and one optional file:

| File | Required? | Contents |
| --- | --- | --- |
| `revocation.json` | Yes | Revocation record. |
| `revocation.json.sig` | Yes | Signature of the record. |
| `revocation.json.sig.tsr` | No | Timestamp proofs for the signature under section 6.8. |

`revocation.json` MUST follow section 7 and contain exactly these fields:

| Field | Type and rule |
| --- | --- |
| `schema` | String, exactly `sci-key-revocation/0.1`. |
| `public_key` | The public JWK of the key to revoke, under section 5.1. |

`revocation.json.sig` MUST contain that key's signature over the exact bytes of `revocation.json`, under section 5.1.
An optional `revocation.json.sig.tsr` file contains timestamp proofs under section 6.8 for the SHA-256 of the signature file.

A verified key revocation MUST end the verified status of all domain and account associations for that key.
Readers MUST mark a publication's signing key as revoked when they have a verified revocation for that key.
A missing revocation record is not proof that a key has never been revoked.

Once an indexer verifies a key revocation, it MUST reject new catalog admissions for that key.
Publication dates and timestamp proofs MUST NOT override this rule.
Revocation alone MUST NOT remove publications already admitted to its searchable catalog. The indexer MUST mark their signing key as revoked.
An operator block MUST NOT be presented as a publisher's key revocation.

**Warning:** A key holder can create the signed record in advance. Keep it private until revocation is intended. Anyone with the record and signature can submit them.

## 6. Verification

Verification lets readers and services check files and signed records without trusting the party that supplies them.

Implementations MUST distinguish failed checks from checks they cannot complete.
They MUST report an unknown `schema` value as unsupported.
They MUST NOT report a publication as invalid only because data is unavailable or a local limit prevents a check.

Assume that a publisher can deliberately construct and sign a malicious bundle.
Verification MUST NOT execute content, open external applications, or expand nested archives. Read ZIP entries as bytes without extracting them. Use the bytes that passed verification for later rendering or extraction.
Implementations MUST check input bounds and arithmetic for overflow before reading, seeking, or allocating memory. They MUST limit input sizes and verification work. Stop if a declared size or local limit is exceeded.

### 6.1 Manifest checks

Before using a manifest to verify publication files, readers, archivers, and indexers MUST:

- Check the JSON encoding against [section 7](#7-json-encoding-rules).
- Check the fields, types, and values against [section 3](#3-manifest).
- Check the file paths and path conflicts against [section 3.1](#31-file-paths).
- Calculate the publication identifier from the original manifest bytes, as specified in [section 2](#2-publication-bundle).
- If an expected publication identifier was supplied, check that the calculated identifier matches it.
- Check the public key and manifest signature against [section 5.1](#51-keys-and-signatures), using the key and original manifest bytes specified in [section 3](#3-manifest).

### 6.2 File checks

Before using a file as part of a publication, readers, archivers, and indexers MUST:

- Complete the [manifest checks](#61-manifest-checks).
- Check that the requested path is a key in the manifest's `files` object.
- Check that the retrieved byte count equals that entry's `bytes` value.
- Check that the SHA-256 of the retrieved bytes equals that entry's `sha256` value.

Complete these checks before decoding or rendering the file.
Publishing tools SHOULD check that display images decode. Readers SHOULD limit memory and processing time when decoding display images.

### 6.3 Metadata checks

Before presenting publication metadata as verified, readers, archivers, and indexers MUST:

- Complete the [file checks](#62-file-checks) for `sci.json`, `README.md`, and any declared `LICENSE`.
- Check the JSON encoding of `sci.json` against [section 7](#7-json-encoding-rules).
- Check its fields, types, and values against [section 4](#4-publication-metadata), including its subsections.
- Check `README.md` and any declared `LICENSE` against the text requirements in [section 2](#2-publication-bundle) and [section 4.2](#42-text-characters).

### 6.4 Complete bundle checks

Before unpacking or accepting a complete bundle, readers MUST:

- For a ZIP, check the entire archive against [section 2.2](#22-single-file-bundle-zip).
- Complete the [metadata checks](#63-metadata-checks).
- Complete the [file checks](#62-file-checks) for every path in the manifest's `files` object.
- Check that the bundle contains exactly the files permitted by [section 3](#3-manifest). For loose files, exclude the optional `manifest.json.sig.tsr` sidecar from this inventory; timestamp verification is separate under section 6.8.

Indexers and archivers SHOULD complete these checks before accepting or serving a complete bundle.

When extracting files, a reader MUST write only within the selected destination.

### 6.5 Catalog admission

Before adding a publication to its searchable catalog, an indexer MUST:

- Complete the [metadata checks](#63-metadata-checks).
- Apply the revocation rule in [section 5.3](#53-key-revocation).
- Apply its published admission policy, including its limits, under [section 6.9](#69-service-policy).

An indexer SHOULD complete the [complete bundle checks](#64-complete-bundle-checks) before adding a publication to its searchable catalog.

After catalog admission, indexers SHOULD asynchronously obtain and retain a timestamp proof verified under their trust policy if no such proof is retained. Failure MUST NOT block or reverse catalog admission.

### 6.6 Key association checks

Before reporting a key's external association as verified, readers, archivers, and indexers MUST:

- Retrieve the claim and signature using the account method in [section 5.2](#52-external-identity-claims).
- Check the claim against section 5.2.1 and section 7.
- Check that the claim's decoded public key bytes equal the expected public key bytes.
- Check that `account.type` and `account.id` equal the requested account type and identifier.
- Check the claim signature under section 5.1.
- Check that the current time is before `expires`. Alternatively, to report that a publication falls within a hosted claim's validity, check that a timestamp verified under section 6.8 for that publication has a `genTime` before `expires`.
- Check that the key has no known verified revocation.

A successful check for the same key and account MAY be reused under section 5.2.1.
Expiry and known revocations MUST still be checked.

### 6.7 Revocation checks

Before recording a key as revoked, readers, archivers, and indexers MUST:

- Check the record against section 5.3 and section 7.
- Check the public key and signature under section 5.1, using the key and original record bytes specified in section 5.3.

Retain the verified record and signature. Keep the revoked status even if the source removes the files.

### 6.8 Timestamp verification

SCI timestamp files MUST contain one or more concatenated DER-encoded `TimeStampResp` values, with no other bytes. Each value MUST follow [RFC 3161](https://www.rfc-editor.org/rfc/rfc3161.html), as updated by [RFC 5816](https://www.rfc-editor.org/rfc/rfc5816.html), and MUST be checked independently when verifying the file.

Readers MUST delimit values by their DER tag and length, within the bounds in section 6. If the remaining bytes do not begin with a complete DER value, readers MUST stop parsing and MUST NOT scan ahead; earlier individually verified proofs remain usable even though the file is malformed. A complete value that fails verification does not prevent checking later values.

Readers MAY verify timestamp proofs.
Before reporting a verified timestamp, readers MUST:

- Complete the [manifest checks](#61-manifest-checks) or [revocation checks](#67-revocation-checks), as applicable.
- Check that the response status is `granted` or `grantedWithMods` and a `TimeStampToken` is present.
- Check that the token's `messageImprint` uses SHA-256 and matches the SHA-256 of the verified signature bytes.
- Verify the token under RFC 3161 and RFC 5816, including its signature, signing certificate, certificate chain, and revocation status, using local trust policy.
- Report the token's `genTime`, any stated accuracy, and the timestamp authority.

Readers and services choose which timestamp authorities they trust.
A failed timestamp check MUST NOT invalidate otherwise valid publication signatures or file hashes.
Anyone MAY distribute timestamp proofs as a separate sidecar. Adding, replacing, or removing the sidecar changes neither the publication identifier nor the ZIP bytes.

### 6.9 Service policy

Services MAY set admission requirements stricter than the format rules, subject to the baseline requirements in [section 2.1](#21-baseline-publications).
They MUST publish their admission limits and license requirements.
The baseline does not require a service to accept every publication or provide unlimited storage or throughput.

Any additional policies MUST NOT be used to impose smaller per-publication size, file-count, or structural limits on baseline publications. Indexers and archivers MUST NOT reject baseline publications because of their approved license terms or require additional licensing grants as a condition of admission. Services MAY accept other licenses under their published policy.

A service MUST distinguish an admission-policy refusal or a temporary capacity failure from a failed verification check.

## 7. JSON encoding rules

JSON documents MUST follow [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html), with these additional restrictions:

- Use UTF-8 without a byte-order mark.
- Reject duplicate object keys after escape decoding, at every depth.
- Reject unpaired UTF-16 surrogate escapes.

Field types and permitted values are defined by each record's schema.
Authors can choose whitespace, field order, and escape spelling within these rules.
Hashes and signatures MUST be checked against the original JSON bytes.

## 8. Discovery and retrieval

These interfaces let readers find and retrieve publications without custom code for each service.
A reader can ask an index for publication sources or use an archive descriptor supplied directly.
Services that implement these interfaces MUST follow this section.

Descriptors and publication lookup responses under section 8.1.1 MUST be JSON objects under section 7.
Descriptor URLs and expanded templates MUST be absolute URLs.
Templates use [RFC 6570](https://www.rfc-editor.org/rfc/rfc6570.html) with only the expressions listed for each field.

Responses intended for browser readers SHOULD send `Access-Control-Allow-Origin: *`.
For bundle range responses, they SHOULD also send `Access-Control-Expose-Headers: Content-Range`.

### 8.1 Index descriptor

An index that supports this interface MUST publish its descriptor at:

```text
https://<index>/.well-known/sci/index.json
```

The following fields are strings:

| Field | Required? | Purpose | Required expressions |
| --- | --- | --- | --- |
| `schema` | Yes | Exactly `sci-index/0.1`. | None |
| `publication_lookup_url` | Yes | Find archive descriptors for a publication under section 8.1.1. | `{id}` |
| `revocation_lookup_url` | Yes | Retrieve revocation files under section 8.1.2. | `{x}`, `{file}` |
| `timestamp_lookup_url` | Yes | Retrieve retained publication timestamp proofs under section 8.1.3. | `{id}` |
| `submit_url` | No | Submit publication locations under section 8.1.4. | None |
| `policy_url` | No | Service policy, including admission limits and license requirements under section 6.9. | None |
| `docs_url` | No | Documentation for the index's API and services. | None |

Additional fields MAY describe other services, such as revocation or timestamp submission.

#### 8.1.1 Publication lookup

`id` is the manifest's SHA-256 hash in 64 lowercase hexadecimal characters, without the `sha256:` prefix.
A successful GET to the expanded `publication_lookup_url` returns `200` with a `descriptors` array of archive descriptor URL strings:

```json
{"descriptors": ["https://archive.example/.well-known/sci/archive.json"]}
```

Each descriptor identifies a source that may provide the requested publication.
If the index has no sources to return, it MAY return `{"descriptors":[]}` or `404`.
Readers MUST verify retrieved files under section 6, using the requested publication ID.

#### 8.1.2 Revocation lookup

For each key whose revocation it verifies, an index MUST retain at least one verified record and its matching signature.
It MUST serve that pair through its revocation lookup interface.

`x` is the key's canonical JWK `x` value. `file` is one of the three filenames in section 5.3.
A GET to the expanded `revocation_lookup_url` returns the original file bytes with `200`, or `404` if the index has no such file.
Readers MUST check that the record's `public_key.x` equals the requested key and verify the record and signature under section 6.7.
A missing record does not prove that the key has never been revoked.

#### 8.1.3 Timestamp lookup

`id` uses section 8.1.1. A GET to the expanded `timestamp_lookup_url` returns `200` with the retained proofs for that publication's manifest signature, encoded as a timestamp file under section 6.8, or `404` if none are retained. Servers SHOULD use content type `application/octet-stream` for timestamp files. Readers MUST verify proofs under section 6.8 using the requested publication ID before reporting a verified timestamp.

Indexers MAY retain a bounded selection of proofs under their published trust and resource policies. A `404` response does not prove that no timestamp exists.

#### 8.1.4 Publication submission

An index MAY accept submissions through `submit_url`. If advertised, the endpoint MUST implement this section. Publishers, archivers, and other callers use the same interface; the caller need not control the signing key.

Send a POST with `Content-Type: application/json` and an object under section 7 containing only these fields:

| Field | Required? | Rule |
| --- | --- | --- |
| `id` | Yes | Publication digest under section 8.1.1. |
| `descriptors` | Yes | Nonempty array of distinct absolute archive descriptor URLs under section 8.2. |
| `account` | No | Account object under section 5.2.1, supplied as an unsigned lookup hint. |

Apply section 6 and the service policy under section 6.9. Success confirms registration of at least one supplied location for publication lookup under section 8.1.1. Repeat submissions MUST NOT create duplicate registrations.

Return `200` or `201` on success. The response MUST use `application/json` and be an object under section 7 containing `id`, the requested publication digest. Services MAY return additional fields.

Errors MUST use a `4xx` or `5xx` response with `application/json` and an `error` object containing string fields `code` and `message`, and Boolean `retryable`. Codes MUST be stable.

### 8.2 Archive descriptor

An archive descriptor gives retrieval routes for publication files.
The following fields are strings:

| Field | Required? | Meaning | Permitted expressions |
| --- | --- | --- | --- |
| `schema` | Yes | Exactly `sci-archive/0.1`. | None |
| `bundle_url` | No | Complete publication ZIP under section 2.2. | `{id}`, `{id2}` |
| `entry_url` | No | Individual file, including the manifest, signature, and optional timestamp sidecar. | `{id}`, `{id2}`, `{+path}` |
| `blob_url` | No | Individual file addressed by its SHA-256 hash. | `{sha256}` |
| `list_url` | No | Current list of advertised publication IDs. | None |

`id` uses section 8.1.1. `id2` is its first two characters.
`path` is the exact entry path relative to the bundle root. `sha256` is the file's SHA-256 hash in 64 lowercase hexadecimal characters.
The archiver MUST serve the manifest and signature through `bundle_url` or `entry_url` for each advertised publication.
Listed files MAY be available through any combination of routes.
An archiver serving publication timestamp proofs MUST provide `entry_url` and serve them at `path = manifest.json.sig.tsr`. Readers MAY retrieve this sidecar separately and verify it under section 6.8.
`bundle_url` SHOULD support single-range requests with status `206` when the range is satisfiable.

`list_url` returns UTF-8 text with one publication ID per LF-terminated line, using the form in section 8.1.1.

#### 8.2.1 Descriptor discovery

For discovery from a domain or repository, an archiver MUST publish the descriptor at the corresponding location:

| Source | Descriptor URL |
| --- | --- |
| Domain | `https://<domain>/.well-known/sci/archive.json` |
| GitHub repository | `https://raw.githubusercontent.com/<owner>/<repo>/main/sci/archive.json` |
| Hugging Face repository | `<repository-url>/resolve/main/sci/archive.json` |

For Hugging Face, `repository-url` is the full model, dataset, or Space repository URL, without a trailing slash.

A descriptor MAY also be supplied directly or hosted at another URL.

## Acknowledgements

SCI draws on ideas explored by [Nanopublications](https://nanopub.net/) for decentralized scientific publishing, [Nostr](https://github.com/nostr-protocol/nips/blob/master/01.md) for signed records and public-key identity, and [IPFS](https://docs.ipfs.tech/concepts/immutability/) for content addressing.
Related precedents include [Keybase’s signed identity proofs](https://book.keybase.io/docs/server), [OpenPGP revocation certificates](https://www.gnupg.org/faq/gnupg-faq.html), and [OpenID Connect Discovery’s service discovery documents](https://openid.net/specs/openid-connect-discovery-1_0.html).

## Changelog

### 0.1 (draft) — 2026-11-06

Initial protocol draft. All schema versions start at `0.1`.
