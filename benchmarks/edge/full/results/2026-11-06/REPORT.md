# Complete bundle checks on Supabase Edge

**Use an initial limit of 1,000 listed files and 50,000,000 listed-content bytes.**
The ordinary and demanding 1,000-file fixtures each passed five hosted requests.
The 5,000-file diagnostic failed once with Supabase's CPU resource limit. Keep the
separate metadata, text, manifest, and parser-work limits described below.

[Hosted run](https://github.com/azaho/sci/actions/runs/42165099598), measured commit
`ad0596e0975dbf677180ca247634571cef4d1dbc`. Region: `us-east-2`; Supabase Edge Runtime
1.77.9, compatible with Deno 2.1.4. The workflow is marked failed because one stress
request failed. That result is retained, not filtered out.

## Measurements

Each valid case was submitted five times, sequentially, with only an expected
publication ID and a bundle URL in the POST. Times below are hosted verification
wall times; they are not CPU measurements.

| 50 MB fixture | Successful requests | Median verification | Verification range | Median download + verification |
| --- | ---: | ---: | ---: | ---: |
| 1,000 listed files; small metadata; no timestamp | 5/5 | 300 ms | 288–317 ms | 553 ms |
| 1,000 listed files; demanding metadata and timestamp | 5/5 | 457 ms | 435–567 ms | 799 ms |
| 2,000 listed files; demanding metadata and timestamp | 5/5 | 598 ms | 540–624 ms | 880 ms |
| 5,000 listed files; demanding metadata and timestamp | 4/5 | 905 ms* | 851–1,024 ms* | 1,267 ms* |

\* Successful requests only. The remaining 5,000-file request returned HTTP 546
`WORKER_RESOURCE_LIMIT`. Runtime telemetry records a `CPUTime` shutdown with
2,087 ms of worker CPU use. This total belongs to a reused worker, not a measured
single-request CPU duration. A different worker serving two successful 5,000-file
requests used 1,934 ms. These observations argue against a 5,000-file starting cap.

A worker that handled only one demanding 1,000-file request recorded 541 ms of
CPU for its entire lifetime. This is a directly attributable sample, including
work outside the verifier; other workers served multiple requests. It supports
headroom for this case, not a worst-case CPU guarantee.

The demanding fixture has exactly 50,000,000 bytes across 1,000 listed files:
`sci.json` is 4,000,000 bytes, with 1,000 citations, 1,000-character Unicode reasons,
100 display captions of 2,000 characters, and whitespace padding. README and LICENSE
are each 1,000,000 bytes. The remaining 997 files hold the remaining payload. An
RFC 3161 timestamp proof is included outside that listed payload. The diagnostic
2,000- and 5,000-file fixtures raise only the file-count allowance.

In the demanding 1,000-file requests, metadata/text checking took 98–107 ms and
full timestamp verification took 36–48 ms. Download times are specific to these
hosts and region; they do not predict arbitrary publisher hosts or slow networks.
The GitHub retrieval case is a small fixed fixture, not a 50 MB GitHub benchmark.

## What passed

All 57 rejection cases produced the expected refusal. These include a 1,001-file
publication under the 1,000-file policy, invalid metadata/references, hidden and
forbidden Unicode, ambiguous JSON, unsafe/conflicting paths, invalid keys and
signatures, corrupt file bytes, invalid ZIP structures, size/work limits, malformed
timestamps, incorrect timestamp imprints/signatures/certificate bindings, revoked
TSA certificates, stale CRLs, and untrusted timestamp roots.

The URL flow succeeded with whole ZIPs at temporary signed Supabase Storage URLs
and a synthetic ZIP at raw.githubusercontent.com (five requests). The source
receives no Supabase credentials. A wrong supplied publication hash was rejected,
and a request for a private IP URL was refused before retrieval. The deployed
function accepts only configured fixture locations and rejects redirects; it is
not an unrestricted public URL-fetch service.

The verifier now implements the bundle checks previously missing from the first
experiment: all metadata schemas, Unicode 18 character rules, and RFC 3161 proof
verification with CMS, ESS binding, timestamp EKU, certificate chain, and signed
revocation evidence. See [the coverage and policy document](../../COVERAGE.md) for
exact scope, restrictions, and links to the frozen draft. Python independently
creates the fixtures, and OpenSSL verifies the positive timestamp before testing.

## Proposed service limits

- **1,000 files listed in the manifest**, counting sci.json, README.md, and LICENSE.
  With LICENSE, this leaves 997 supporting files. The manifest, signature, and
  optional timestamp have separate limits and do not count toward the 1,000.
- **50 MB total listed content**, including metadata, README, and LICENSE.
- **1 MiB manifest**, **4 MB sci.json**, **1 MB each for README and LICENSE**, and
  **256 KiB timestamp proof**. The complete ZIP download cap is 64 MB.
- Enforce the parser, path, certificate-count, and request limits documented in
  [COVERAGE.md](../../COVERAGE.md), rather than relying only on a file-count cap.

These are indexer admission choices, not new SCI format requirements. Five samples
per case and a selected adversarial corpus do not prove a worst-case bound. The
results support this conservative starting policy, not a latency guarantee.

## Memory, runtime compatibility, and cleanup

Across the 1,000-file requests, the largest sampled V8 heap-used value was 22.45 MB
and the largest external allocation sample was 112.59 MB. These occurred in
potentially different requests/times and are not a total peak measurement. RSS is
unavailable in this runtime. No memory-limit failure was observed.

An initial attempt ([run 42164725299](https://github.com/azaho/sci/actions/runs/42164725299))
failed at worker initialization because upstream PKI.js selected a Node-specific
engine store when Supabase's process shim exposed an undefined pid. No verifier
measurements came from that attempt; its temporary resources were removed. The
measured version uses the ES module with a documented two-guard compatibility
patch, preserving the cryptographic implementation. A deployed preflight now
checks handler startup before uploading fixtures.

`results.json` preserves all 87 submitted requests, including the resource-limit
failure. `cases.json` records fixture sizes and expectations; `deployment.json`
identifies the exact function and code; `runtime-logs.json` records function-scoped
telemetry. A later [read-only log collection](https://github.com/azaho/sci/actions/runs/42165490953)
produced `runtime-logs-final.json`, covering all 15 worker shutdowns: 14 `EarlyDrop`
and one `CPUTime`. `cleanup.json` confirms removal of the temporary function, fixture
objects, and bucket. No existing publication, database table, or production
function was changed.

This is experimental verifier code, not an audited release. The timestamp trust
policy uses disposable test certificates and operator-supplied CRLs; this does not
establish a production TSA trust store. Affiliation/revocation lookup, database
admission, concurrent submissions, sustainable throughput, rendering, and disk
extraction are outside this benchmark.

A post-run review tightened DOI prefix validation so a percent-encoded slash
cannot be hidden in the prefix. The local corpus and a targeted regression check
pass with this change. The hosted measurements above remain tied to the recorded
commit; the fix changes rejection of that malformed DOI, not the measured valid
fixtures. It has not had a separate hosted performance run.
