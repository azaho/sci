# 1,000-file soak protocol

Test the candidate 1,000-listed-file, 50,000,000-byte policy with 1,000 first
attempts. This measures the existing complete-bundle verifier; it does not change
its verification rules or implement the rest of the indexer.

Twenty valid synthetic bundles have distinct signing keys, metadata, file hashes,
size distributions, and IDs. Five profiles each have four variants:

- Ordinary metadata and varied supporting-file sizes.
- Unicode-heavy metadata: 1,000 citations, Unicode reasons, and 100 long captions.
- ASCII-heavy metadata: 1,000 citations and 997 maximum-length captions.
- Long portable paths, with the ASCII-heavy metadata.
- One large supporting file plus many small/empty supporting files.

Heavy profiles reach the configured limits for manifest (1 MiB), metadata (4 MB),
README (1 MB), and LICENSE (1 MB), within the same 50 MB listed payload. Text varies
between ASCII and multibyte Unicode. Sixteen of twenty bundles include timestamps.
All fixtures are verified locally before deployment, and OpenSSL independently
checks a generated timestamp. Four temporary deployments each run 250 requests,
with natural worker reuse within each deployment. Each wave removes its own
function, private bucket, and fixture objects.

The total is 500 sequential attempts, 250 with at most two requests in flight,
and 250 with at most four. Each wave uses all three concurrency levels; request
order is deterministically shuffled and each fixture is used 50 times overall.
The region is pinned to `us-east-1`. There are no automatic retries. Source URLs
point to complete ZIPs in the temporary private Storage bucket, with short-lived
signed URLs. Total listed-content retrieval is about 50 GB plus ZIP overhead;
four fixture uploads total about 4 GB. No production records are changed.

The deployment adds request IDs, worker invocation counters, and worker IDs to
responses. `request_index == 1` identifies the first handled request in that worker;
it is a useful grouping, not proof of an infrastructure-level cold start. Successful
results are retained by the harness instead of logged again by the function, to
reduce log-rate pressure. CPU and shutdown evidence is collected in minute windows
to avoid the query's 1,000-row result cap. Immediate collection can still miss late
shutdown events; coverage must be checked before interpreting missing events.

Keep busy (HTTP 429), resource-limit (HTTP 546), source, transport, platform, and
verification failures separate. Raw results include every attempt, timings, profile,
wave, region, worker reuse, and sampled memory. Latency distributions for successes
must be labelled as such and accompanied by the failure counts. Worker CPU totals
must not be presented as per-request CPU when a worker handled multiple requests.

Repeated fixtures and reused workers make these trials dependent. An observed
failure proportion describes this workload and period; it does not establish a
universal production failure probability. This is a bounded load experiment, not
a test of arbitrary hosts, regional outages, hours of sustained load, or malicious
inputs beyond the prior conformance fixtures.
