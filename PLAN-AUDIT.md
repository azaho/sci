# PLAN.md implementation audit

Audited against `azaho/sci-spec/PLAN.md` at source commit
`59afe3ed3605c52a70256d5932a2a63d94b820a1`; the current remote plan was checked and
is unchanged. This is a requirement audit, not a declaration that the full build
and deployment plan is complete. The plan itself stays in sci-spec.

| Plan requirement | State and evidence |
| --- | --- |
| §1 public SCI; independent legacy service | `sci-dev` is public; no legacy service, data, repository name or banner changes. |
| §1 Apache-2.0 and original publication license | Root/package LICENSE and NOTICE; package metadata; baseline license and corpus remain byte-for-byte copies. |
| §2 one module/folder per CLI command | Corrected: 15 modules in `cli/sci/commands/`, each owning its parser and handler. `main.py` only registers and dispatches commands and formats errors. Regression test checks this boundary. |
| §2 clients, storage, config and output | Account normalization, hosting, catalog requests, retrieval and TSA work in `clients/`; keys, atomic files, bundle writes and SQLite cache in `storage/`. |
| §2 protocol rules belong in libraries | Signature, manifest, file, metadata, complete-bundle and record checks are in `pysci`; CLI uses library APIs. Selective read scopes are now implemented in `pysci.reading`. No private Sciscape dependency. |
| §2 package configuration and local builds | Two Python package configurations, uv workspace/lock; strict local TypeScript package/lock. No PyPI or npm publication. |
| §2 README installation and first workflow | Badges, Git installation for both packages, `sci --help`, offline workflow and external hosting instructions. Clean Git installation exercised with isolated tool/state directories. |
| §3 copied documents and provenance | Protocol/corpus now on main by owner request; CLI design and SOURCE.md in this PR. Original source documents retained. Historical README links added in sci-spec commit `98daee915350d4ba3e80260ff25cd4801c0a6830`. |
| §4 strict checks and component tests | Ruff, strict mypy, pytest, strict TypeScript, component-local tests and CI. Main now requires passing `python` and `typescript` checks with an up-to-date branch, including for administrators. No fixture edits to force passing results. |
| §4 shared conformance corpus | Both libraries run the same 594 byte/record/contract cases. The 15 HTTP/state scenarios are NOT claimed as complete library conformance. |
| §5A preparation | Public/private visibility, licenses, source pins, corpus import, package/CI scaffolding and historical README links completed. |
| §5B FreeTSA defaults | CLI design corrected to name FreeTSA and its CA explicitly; bundled CA, no fallback, preserved signed output on TSA failure. Local invocations share a pacing lock; a shared egress across machines/services still requires coordinated operations. |
| §5B1–3 first implementation | Python/CLI and TypeScript libraries implemented; Sciscape consumes a pinned local build. Companion PR has its own audit. |
| §5B4–7 online workflow, release and deployment | Incomplete. Provider interoperability, hosted runtime limits and deployment are not established by local tests. No stable 0.1 release or production deployment is claimed. |
| §5C campaigns | Not started: gated on completion of §5B. |
| §5D final repository names | Not started: requires live services and a separate explicit owner request. |
| §6 completion conditions | Offline installation/workflow and library corpus checks pass. Full online completion remains pending. Legacy service is unchanged. |

## Design deviations corrected in this audit

- Replaced grouped `local.py`, `identity.py`, `remote.py` and centralized command
  argument branching with per-command modules and generated help.
- Added search continuation bound to source/query/filter snapshots, local ID and
  remote admission-time/ID ordering, searched-field labels, result ID formatting,
  and verified metadata keyed by ID with separate file memberships.
- Added selective manifest/file/metadata reading for loose sources, preferred
  before the bounded complete-ZIP fallback. Reading reports its actual scope and
  the checked files, not complete-bundle verification.
- Preserved timestamp sidecars during local and hosted publication, normalized
  equivalent publishing destinations, and deduplicated publication IDs.
- Kept valid submissions processing when a campaign also contains invalid ZIPs;
  shared account normalization with claim creation.
- Enforced requested signer matching when `pull` reuses an existing verified copy;
  kept private keys outside claim outputs and included the key's `x` in keygen output.
- Corrected relative HTTP redirect handling, finite deadline validation and
  Retry-After handling; removed retries for validation/policy refusals.

## Requirements still incomplete

These are implementation requirements, not optional polish. They remain visible
in the draft PR rather than being removed or weakened in the design:

- Selected ZIP-range reads (loose selective reads and full-ZIP fallback work).
- Pull's optional proof retrieval from archive/index routes and complete proof
  status reporting; broader structured error detail/response-schema coverage.
- Real GitHub and Hugging Face hosting/CORS verification, second-archive retrieval,
  automatic GitHub concurrent-addition recovery and interrupted provider uploads.
  Provider API subprocesses still need the exact per-publication operation deadline.
- Complete CLI error field/requirement/observed-value diagnostics.
- A test driver for all 15 HTTP/state corpus scenarios, beyond the service
  transaction regressions already present in Sciscape.
- Sciscape hosted-resource tests, full online workflow, deployment and the release
  decisions explicitly gated on those results.
