# Draft decisions and remaining work

Status: review notes for draft 0.1. These notes are not protocol requirements.

## Proposed defaults

| Decision | Draft choice | Reason |
| --- | --- | --- |
| Format version | Separate `sci-publication/0.1` schema. | Preserve the meaning of existing version 1 records. |
| Publication identity | Keep UUIDs; add exact manifest-hash references. | Preserve familiar links and expose conflicting UUID claims. |
| Signature input | Prefix plus exact manifest bytes. | Separate this format from old publications and future action records. |
| File list | One external hashed inventory. | Keep manifests small without requiring a Merkle tree. |
| File storage | Individual objects; an archive can be one opaque object. | Start with ordinary HTTPS hosts. |
| Filenames | Portable ASCII paths with explicit collision rules. | Make Python, browser, and disk behavior agree. |
| License | Signed license file; accepted terms are service policy. | Let independent services choose terms without changing the byte format. |
| Categories | Preserve publisher strings. | Keep one service's taxonomy outside the protocol. |
| DOI matching | Restricted syntax; exact string equality. | Avoid an underspecified normalization algorithm. |
| Verification | Separate manifest, metadata, complete files, and admission. | Do not imply that unavailable attachments were checked. |

## Decisions to review before library APIs settle

1. **License policy.** Confirm whether SCI permits different license terms across services.
   The draft does. SciField can still require one fixed license.
   Its existing license scope names `files.zip`; a new fixed license must cover individual supporting files.
   The worked example has no supporting files and uses the existing license unchanged.
2. **Large publications.** Confirm the inventory bounds and the opaque-archive starting point.
   The draft permits 250,000 entries within 64 MiB, but requires the full inventory for one file lookup.
   Measure representative inventories before adding tree or pack formats.
3. **Path and citation restrictions.** Confirm ASCII filenames and the restricted DOI grammar.
   These are deliberate initial restrictions, not claims about all valid filenames or DOIs.
   Add exact fixtures before broadening either grammar.
4. **Cryptographic acceptance.** Test the strict Ed25519 profile with the proposed Python and JavaScript backends.
   Add small-order, mixed-order, noncanonical point, identity-point, and scalar-boundary vectors.
   Do not assume that two backend functions accept the same malformed signatures.
5. **API size.** Decide whether the first service release needs incremental discovery.
   The draft has a static catalog export, not a complete search or submission API.
   Define these service interfaces before building the indexer and archiver PRs.

## Required extensions before the full public service

- **Key revocation:** exact signed certificate bytes, domain separation, import rules, and known-revoked-key admission behavior.
  Keep cryptographic verification separate from permission to accept new work.
  Author timestamps cannot establish whether a publication preceded a revocation.
  Decide which service receipt or witnessed log establishes that ordering.
- **Profiles and key changes:** signed schemas, replay rules, concurrent updates, and attribution history.
  Decide whether parent links and key rotation belong in the first service release.
  Do not infer profile authority from a publication title or a matching display name.
- **Reports:** target formats, signer requirements, resource limits, and private reason handling.
  Separate integrity observations from content complaints and operator decisions.
- **Legacy import:** old-format location records, import attestations, and original acceptance evidence.
  Preserve original bytes. Do not present an importer as the original author.
- **Service policy:** accepted licenses, quotas, network fetch policy, PNG validation, archive inspection, and moderation behavior.
  Keep these rules in a SciField policy document with explicit policy versions.

These extensions are outside draft 0.1. The current draft does not claim to solve federated identity state.

## Deferred features

Packed range retrieval, Merkle inventories, timestamp proofs, transparency logs, and signed checkpoints can follow the initial workflow.
Each needs a separate format and failure model.
A signed checkpoint alone does not establish trusted time, completeness, freshness, or agreement between observers.
No storage hash guarantees long-term availability.

## Review and PR sequence

1. Review the core draft, exact example, and decisions above.
2. Expand shared fixtures, including malformed inputs and Ed25519 boundary cases.
3. Build the Python library; update the spec and fixtures when ambiguities appear.
4. Build the JavaScript library; run both libraries against the same fixtures and each other's output.
5. Specify service interfaces and the action extensions needed for launch.
6. Build the archiver, then the indexer, with focused service checks.
7. Integrate the website and CLI; demonstrate the complete workflow on two hosts.
8. Require an independent implementation before declaring the protocol stable.

Keep only a few dependent PRs open at a time. Each PR must state its remaining limits.
## Package names

On 2026-11-04, the [npm lookup for `sci.js`](https://registry.npmjs.org/sci.js)
and the [PyPI lookup for `pysci`](https://pypi.org/pypi/pysci/json) both returned HTTP 404.
Neither lookup showed a public package under that name.
This does not prove that a registry will permit registration; a name can be reserved or blocked.
Keep both as working names until registration succeeds. No package was registered by this draft.
