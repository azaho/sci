# Hosted Supabase verification results

A 50 MB publication fits this hosted Edge Function verification workload. All 25
valid requests succeeded, and all six malformed fixtures were rejected for the
expected reason. No request hit a platform resource limit. This supports proceeding
with a bounded Edge verifier; it does not establish production readiness.

[Benchmark run](https://github.com/azaho/sci/actions/runs/42162669172),
[runtime telemetry collection](https://github.com/azaho/sci/actions/runs/42163151717).
Measured code: `07dd5b90fee9105b1554676f6feccccdfeffd5a5`.
Region: `us-west-1`. Runtime: Supabase Edge Runtime 1.77.9, compatible with Deno 2.1.4.

## Timings

Five sequential requests per valid fixture. File counts include `README.md` and
`sci.json`, but exclude the manifest and its signature. Payload size is the sum of
listed file sizes: 50 MB means 50,000,000 bytes, plus ZIP and manifest overhead.
The large manifest contains whitespace padding; it is not a worst-case JSON fixture.

| Payload | Listed files | Median verification | Verification range | Median download + verification |
| --- | ---: | ---: | ---: | ---: |
| 1 MB baseline | 3 | 8 ms | 7–11 ms | 0.26 s |
| 50 MB | 3 | 191 ms | 187–464 ms | 2.69 s |
| 50 MB | 1,002 | 311 ms | 288–516 ms | 2.74 s |
| 50 MB | 5,002 | 712 ms | 692–989 ms | 3.29 s |
| 50 MB; 1 MiB manifest | 3 | 232 ms | 227–238 ms | 2.40 s |

These are wall times measured inside the hosted function, not local timings or CPU
times. Download dominates elapsed time. The first request for each 50 MB fixture
took roughly 9.6–10.7 seconds including verification; later requests took roughly
2.3–3.8 seconds. The experiment does not isolate the cause of this difference.
Files were fetched from private Supabase Storage in sequential 8 MB parts, so these
download times do not predict R2, GitHub, or individual-file retrieval latency.

## CPU and memory

The telemetry response contains all 31 request log messages, ten worker starts,
and ten worker shutdowns. Every shutdown reason is `EarlyDrop`; there are no
CPU, memory, or wall-time shutdowns. Supabase describes `EarlyDrop` as completion
without exhausting the worker's limits. Its shutdown CPU field is milliseconds,
and shutdown memory is a snapshot, not a peak.
[Telemetry definitions](https://supabase.com/kb/troubleshooting/edge-function-shutdown-reasons-explained).

Workers handled two to five requests each. Their CPU totals range from 60 to
1,531 ms, including work outside the verifier. Two workers that handled only the
5,002-file fixture used 1,445 and 1,503 ms for two requests each. These totals cannot
provide exact per-request CPU measurements. Supabase documents a 2-second CPU
limit per request and 256 MB of memory.
[Hosted limits](https://supabase.com/docs/guides/functions/limits).

The largest sampled V8 heap-used value was 13.35 MB; the largest sampled external
allocation was 122.70 MB. These maxima can occur at different times. The largest
platform memory snapshot at shutdown was 113.00 MB. RSS was reported as zero and
is unavailable, not a measurement of zero memory use. Neither sampled counters
nor shutdown snapshots establish the true peak.

## Coverage and limits

The workload checks manifest hashing and parsing, Ed25519 signature and subgroup
rules, every listed file's SHA-256 and size, every ZIP entry's CRC-32, ZIP layout
and directory consistency, and basic metadata/text validity. Negative fixtures
cover a bad final file hash with a correct ZIP CRC, a bad signature, duplicate JSON
keys, conflicting central-directory names, trailing bytes, and an oversized manifest.

This is a partial verifier for the draft, not a conformance library. It excludes
full metadata/Unicode validation, timestamp proofs, affiliations, revocation
lookup, database writes, and moderation. Synthetic payloads, shallow paths, and
small metadata do not establish worst-case cost for hostile inputs. Concurrent
submissions and sustained load were not tested.

Use 50 MB as a candidate service limit, with separate limits for listed file count,
manifest size, fetched bytes, and concurrent work. A conservative initial file
limit around 1,000 leaves more room for checks not included here; 5,002 passed this
experiment but is not yet a recommended production ceiling. Recheck resource use
with the complete verifier and admission flow before release. These are indexer
policy choices, not proposed SCI format limits.

## Evidence and cleanup

`results.json` contains every request. `deployment.json` identifies the tested
code and temporary function. `runtime-logs.json` contains function-scoped telemetry.
`runtime-logs-status.txt` preserves the initial telemetry request error: Python
produced timestamps with an offset that the API rejected. A later read-only query
using UTC `Z` timestamps retrieved the logs above; it did not rerun the workload.

`cleanup.json` confirms deletion of the temporary function, private fixture
objects, and bucket. The benchmark did not change existing publications, tables,
functions, or project settings. The seven-day benchmark token and GitHub secret
remain available for follow-up work.
