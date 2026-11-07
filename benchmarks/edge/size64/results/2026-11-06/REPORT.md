# Exact 64 MiB: isolated worker CPU measurements

**64 MiB passes the three bounded-text shapes tested, but does not provide a blanket current-spec baseline or consistent 2× CPU headroom.** A valid large-README fixture failed twice with CPU-limit shutdowns. A large binary file passed twice but used more than one second of worker CPU.

[Hosted run](https://github.com/azaho/sci/actions/runs/42346076341), measured commit `d815799`. Region: `us-east-1`. Each of eight fresh deployments received one measured verification POST. The logs confirm eight distinct measured workers and exactly one verification start per deployment. This is a quick smoke test with two trials per shape, not a reliability distribution.

## Current spec and the meaning of 64 MiB

The [pinned current spec](https://github.com/azaho/sci-spec/blob/ca1f76b6820922683cc1b42e2000d0deb3373e25/sci-spec.md) uses schema `sci-publication/0.1`, requires canonical ZIP dates of 1980-01-01, and keeps RFC 3161 timestamps outside the ZIP. It contains neither a 64 MiB baseline nor the quoted “MUST NOT refuse a baseline publication for size” rule. Section 6.9 permits stricter service admission limits.

For this experiment, **64 MiB means exactly 67,108,864 bytes for the complete ZIP**, including manifest, signature, local headers, and central directory. Each ZIP contains 1,000 listed files plus the manifest and signature. The listed content therefore occupies slightly less than 64 MiB. Timestamp sidecars are omitted and their verification is not measured.

| Shape | Listed-content bytes | Manifest bytes | README bytes |
| --- | ---: | ---: | ---: |
| Ordinary metadata | 66,883,644 | 112,982 | 27 |
| Demanding metadata | 65,948,050 | 1,048,576 | 1,000,000 |
| One large supporting file | 66,887,510 | 109,116 | 27 |
| Large README | 66,887,511 | 109,115 | 66,886,834 |

The demanding case also contains 4,000,000 bytes of metadata, 1,000 citations, 997 display items, and a 1,000,000-byte LICENSE. The large supporting file has 66,886,806 bytes. The two large-file cases use many empty supporting files; empty files remain hashed and CRC-checked.

## Measurements

CPU below is **platform-reported worker lifetime CPU**, including startup, the download path, and any readiness GET handled by that worker. It is not inferred from elapsed time. Verification wall time is measured separately and is unavailable when the platform terminates the request.

| Shape | Passed | Worker CPU, two trials | Verification wall time, two trials | Shutdown reason |
| --- | ---: | --- | --- | --- |
| Ordinary metadata | 2/2 | 587, 639 ms | 310, 414 ms | EarlyDrop |
| Demanding metadata | 2/2 | 967, 954 ms | 682, 568 ms | EarlyDrop |
| One large supporting file | 2/2 | 1,150, 1,112 ms | 743, 744 ms | EarlyDrop |
| Large README | 0/2 | 2,993, 3,007 ms | Not returned | CPUTime |

Both large-README requests returned HTTP 546 `WORKER_RESOURCE_LIMIT`. Their shutdown memory samples were about 149.5–149.9 MB; the reported termination reason was CPU, not memory. The approximately three-second values are measured worker lifetime totals, not evidence of a guaranteed three-second request allowance. No other verification ran on either worker.

All ten worker boots, including readiness-only workers, have shutdown records: eight EarlyDrop and two CPUTime. Collection had no errors. CRC-32 is computed over every ZIP entry, SHA-256 over all listed files and the manifest, with signature, ZIP layout, paths, metadata, and text checks also included.

## Storage fixture details

**8 MiB is an arbitrary test chunk size, not a Supabase or SCI size limit.** Earlier test buckets were configured with a 50 MiB per-file limit. This test avoids relying on the project's global upload cap, which was not checked or changed: it uploads each ZIP as eight private 8 MiB objects, fetches them with signed URLs, and assembles one 64 MiB buffer before verification.

Thus this is not a single-URL download benchmark. The extra fetch and buffer-fill work is included in worker CPU, making that measurement conservative for this retrieval path. The verification timer begins only after the full ZIP is assembled. A different download path can change CPU and memory costs; no extrapolation to every host is claimed.

## What this supports

A 64 MiB total cap can be feasible with separate text, metadata, manifest, and work limits. It is not enough by itself to guarantee acceptance of everything allowed by the current spec: that spec has no README byte cap, and the large-README fixture is a concrete counterexample for this implementation. The large-binary results also miss the proposed “under one second of total CPU” target. Two passing trials cannot establish a worst-case bound.

If defining a common baseline, define its other resource dimensions as well. If applying a local indexer policy, current section 6.9 already allows stricter limits. These results do not establish that 32 MiB universally works either; that size was not tested here. The spec has not been edited.

A queue with one item per invocation does not by itself guarantee one verification per worker. The prior [concurrency experiment](../../../concurrency/results/2026-11-06/REPORT.md) observed overlapping invocations on the same worker. This test obtained isolation through separate function deployments per measured request; a production queue would need its own concurrency control.

## Scope, validation, and cleanup

The adapter changes the prior benchmark's schema value, canonical ZIP date/version checks, timestamp-entry exclusion, and relevant size caps. It retains a 1 MiB manifest cap, 4 MB metadata cap, and older path/parser work limits. It does not implement the current spec's new URL citation support or contextual joiner rules; the fixtures use neither. This is a current-spec-compatible fixture test, **not a full current-spec conformance audit**. A snapshot and exact adapter are preserved in the [test directory](../../README.md).

Both the handler and local verifier passed Deno type checking. All four fixtures verified locally; an incorrect ZIP date was rejected for each. Python independently generated signatures and hashes, and its ZIP implementation checked every CRC. Every cloud fixture was exactly 64 MiB. All eight functions, private buckets, and fixture sets were removed with no cleanup errors. No existing project setting or production data changed. The hosted workflow completed all eight attempts and records failure because the two resource-limit results are retained.

[All measurements as CSV](measurements.csv), [CPU-to-worker summary](summary.json), and the eight `wave-*` folders contain the original responses, deployment IDs, complete runtime telemetry, and cleanup receipts. The workflow is manual-only after this run.
