# Per-worker concurrency comparison

[Hosted results and failure diagnosis](results/2026-11-06/REPORT.md).

Compare the existing rejection guard with the same handler accepting overlapping
requests. Verification rules, input caps, fixture bytes, region, and request
schedule remain the same. Both modes add identical activity instrumentation.

Each mode receives 200 first attempts: 20 sequential, then 60 each with at most
2, 4, and 8 requests in flight. Twenty distinct 1,000-file, 50 MB publications each
appear ten times per mode. There are no retries. The guarded deployment runs first;
the unguarded deployment runs second. This order and changing platform conditions
are confounders, so this is a diagnostic comparison, not a causal capacity guarantee.

Both modes use the soak generator and full verifier. Each creates its own temporary
function and private fixture bucket, then removes them. At most 8 requests are in
flight. Listed-content retrieval is at most 20 GB, plus ZIP overhead; fixture uploads
are about 2 GB. No production records or functions change. Authentication and the
fixture URL allowlist remain in force.

Responses record worker identity, invocation counter, active requests at entry,
and maximum simultaneous active requests during that request. A small start event
also logs request and worker IDs, so resource-limit responses that lack an execution
ID can be linked to worker shutdowns. URLs and credentials are not logged. Sampling
reports worker memory, not per-request allocations or guaranteed peaks.

Preserve all HTTP 429, 546, transport, source, and verification outcomes. Compare
success counts before interpreting latency percentiles: timeouts or worker deaths
must not disappear into a successful-request timing distribution. Only workload
attempts are included in the denominator; deployment preflights are identified
separately in runtime telemetry.

To reproduce the analysis, place the two result artifacts under `<results>/wave-0`
and `<results>/wave-1`, then copy the telemetry artifact's matching folders over them.
With `matplotlib==3.10.7` installed, run:

```sh
python benchmarks/edge/concurrency/analyze.py <results>
```

The script writes a CSV of all attempts, a JSON summary with failure-to-worker
links, and PNG/SVG outcome charts. Successful-request quantiles use linear
interpolation between ordered observations (type 7). The benchmark workflow is
manual-only after the recorded run.
