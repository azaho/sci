# Implementation and release status

Protocol 0.1 remains a draft. This PR is the initial implementation, not a release
certification or a production cutover.

Implemented: strict byte verification in Python and TypeScript, deterministic ZIPs,
local signing and verification, baseline checks, keys/claims/revocations, local
search cache, index contracts, complete bundle retrieval, GitHub and Hugging Face
archive adapters, FreeTSA acquisition and offline timestamp verification. Tests
cover the shared 594 byte/record/contract cases and additional local workflow and
real FreeTSA regressions. The corpus also contains 15 HTTP/state scenarios; these
are not counted as whole-library conformance here.

Before a stable release:

- Complete live GitHub and Hugging Face hosting/CORS tests, plus second-archive
  retrieval and interrupted-upload recovery against real provider accounts.
- Complete selective file/ZIP-range retrieval and search continuation parity with
  the design; current read/pull verify complete inputs and local search reports
  whether more results exist.
- Complete the browser/backend deployment and all hosted resource-limit tests in
  Sciscape's independent test project, then its production project.
- Review URI/DOI equivalence and uncommon RFC 3339 leap-second handling beyond the
  bundled corpus; the verifier does not claim exhaustive standards certification.
- Finish an independent security audit and keep both repositories' required checks
  enabled before merge. No registry publication or protocol release is automated.

Legacy infrastructure, data, repository name and banner remain unchanged. Campaigns
and repository renames are later plan stages, not implicitly triggered by this PR.
