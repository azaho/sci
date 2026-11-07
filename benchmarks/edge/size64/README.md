# Exact 64 MiB single-verification smoke test

[Hosted results and direct CPU measurements](results/2026-11-06/REPORT.md).

Read against [sci-spec commit ca1f76b](https://github.com/azaho/sci-spec/blob/ca1f76b6820922683cc1b42e2000d0deb3373e25/sci-spec.md), preserved in `spec-snapshot.md`.
That spec has no baseline size or mandatory minimum acceptance rule. For this test,
64 MiB means the **entire canonical ZIP**, exactly 67,108,864 bytes, including the
manifest, signature, ZIP headers, and central directory. Timestamp sidecars are
outside the ZIP and are not measured.

Four valid fixture shapes each have 1,000 listed files: ordinary metadata;
1 MiB manifest / 4 MB metadata / 1 MB each README and LICENSE; one large supporting
file with many empty supporting files; and a large ASCII README with empty
supporting files. Each is measured twice, in a fresh function deployment per
measurement. There is only one verification POST per deployment. A readiness GET
may share its worker; worker lifetime CPU therefore conservatively includes
startup, readiness, download, and verification work. There are no measured retries
or concurrent verification requests.

This is a quick headroom experiment, not a reliability estimate or a full current
spec conformance audit. The prior verifier is adapted to schema 0.1, exact ZIP date
and version fields, and the exclusion of timestamp entries. Local policy permits
64 MiB ZIP/payload/text, while retaining 1 MiB manifest, 4 MB metadata, and the old
path/parser work caps. New URL citations and contextual joiners are not implemented
by this adapter and are not used by the fixtures. These fixture results cannot
justify mandatory acceptance of every current-spec publication below 64 MiB:
other legal manifest, text, path, and metadata shapes remain untested.

Fixtures are stored privately as eight 8 MiB pieces to avoid relying on the
project-wide Storage upload cap. The earlier benchmark bucket's 50 MiB per-file
setting was a harness choice; the project's actual global limit is not inferred
from it. The handler fetches these pieces with short-lived signed URLs and fills
one preallocated 64 MiB ZIP buffer, then runs all bundle checks, including CRC-32
and SHA-256. This is not a single-URL download latency measurement. Download CPU
remains included in worker lifetime telemetry.

The test uploads/downloads at most 512 MiB of fixture content. Every function,
private bucket, and fixture object is removed in a `finally` block. No existing
project settings or production data change. Credentials and signed URLs are not
logged or saved as artifacts. Shutdown logs, including CPU or memory failures,
are preserved for every deployment and matched to its single verification start.

To reproduce the analysis, download `edge-size64-all` into a results directory,
then overlay the matching `wave-*` folders from `edge-size64-telemetry`. Run
`python benchmarks/edge/size64/analyze.py <results>` to regenerate the CSV and JSON
summary. Each CPU value is joined to the worker that logged that request's single
verification start, including requests terminated before a response was returned.
