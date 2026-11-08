# pysci

Typed Python SCI verification, deterministic packing, draft preparation, discovery
contracts and timestamp proof verification. No private Sciscape dependency.

```python
from pathlib import Path
from pysci import load_bundle

publication = load_bundle(Path("publication.zip"))
print(publication.id, publication.baseline, publication.metadata["title"])
```

Verification reads original bytes before decoding metadata. It rejects noncanonical
ZIP headers, duplicate JSON keys, path conflicts and non-prime-order Ed25519 points.
`Invalid`, `Unsupported`, and `Incomplete` remain distinct. `Limits` bounds input
work independently from baseline eligibility. The default complete-bundle budget
is 128 MiB and 4,096 files; baseline requires at most 64 MiB and 1,000 manifest files.

`prepare(entries, private_key)` returns a verified in-memory `Bundle`; callers own
persistence. `Bundle.zip()` creates exact protocol bytes. Timestamp verification
uses explicit anchors, CRLs and evaluation time with OpenSSL, never system trust
or implicit network access. Verify the target signature first. Missing revocation
information cannot establish a trusted timestamp.

`pysci.reading.read_verified` takes a bounded byte reader and verifies manifest,
selected-file or metadata scope. `read_local` provides the same scopes for local
directories and ZIPs. Results list exactly the retained verified bytes; they never
claim complete verification of unrequested files.
