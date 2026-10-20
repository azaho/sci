# Verification record

The implementation was checked on 2026-10-20. The checks below used local test
data. No hosted release was started.

## Automated checks

| Check | Result |
| --- | --- |
| Python suite with PostgreSQL 18.6 | 160 tests passed; no tests skipped |
| Python statement coverage | 86% across `sci` and `server` |
| Ruff | Passed |
| Source distribution and wheel build | Passed |
| CLI installation from the wheel | Passed |
| Playwright with Chromium | All five tests passed at `/` and `/sci/` |
| Playwright with the publisher stopped | All five tests passed |
| Terraform format and validation | Passed with the pinned provider versions |
| Terraform plans with mock providers | Three tests passed |
| GitHub workflow syntax | Passed with actionlint |
| Docker Compose configuration | Passed |

One dependency warning concerns the future Starlette TestClient HTTP transport.
It does not cause a test failure.

The Python tests include exact-byte signatures, OpenSSL verification, strict JSON
schemas, DOI conformance in Python and SQL, PNG limits, and bounded ZIP previews.
They also check upload rejection, concurrent completion, whitelist races,
interrupted finalization, lost source objects, uncertain acceptance writes,
repeated cleanup, private database grants, and archive catalog recovery.

The browser tests check the feed, search, category filters, publication pages,
direct refresh, mobile layout, and service errors. They check that author text
cannot insert HTML, README images do not load, and inline scripts are blocked.
They check the plain-text fallback when the Markdown library is absent.

## Local service checks

Docker Engine was not available in the implementation workspace. The service
checks used native PostgreSQL 18.6, PostgREST 16.5, SeaweedFS 4.48, nginx 1.28.3,
and the Python publisher. The Compose file uses the same database, catalog,
and storage versions, with nginx 1.30.0. GitHub CI runs the complete Compose
installation on a clean runner.

The real HTTP checks passed for these operations:

1. Enable a generated demonstration key and publish three records.
2. Pull a record and compare every declared file with the original bytes.
3. Query search, category, and publisher filters.
4. Query incoming citations with source publication metadata.
5. Reject private catalog resources and public writes.
6. Check archive content types, CORS, and `nosniff`.
7. Stop the publisher and read the feed and publication pages.
8. Stop the publisher, catalog, and database; pull from the archive.
9. Restart the database and storage with their existing data; repeat the checks.
10. Restore a database backup into a separate database. Compare publication
    manifests, acceptance times, citations, whitelist entries, and upload state.

The local storage probe passed conditional-copy checks. It accepted exact and
repeated presigned PUT requests. It rejected changed sizes, absent content
length, and changed content types when those values were signed. These results
describe SeaweedFS only.

## Screenshots

The four images in `docs/screenshots/` show the tested local website. The
publications contain synthetic data from `examples/create_demo.py`. The images
show the desktop feed, desktop reader, mobile feed, and mobile reader.

## Hosted checks

Real R2 behavior, cloud credential permissions, hosted response headers, hosted
catalog rate limits, and production backup recovery remain release checks.
Follow [HOSTED.md](HOSTED.md) before enabling hosted writes. Mock Terraform plans
check configuration logic; they do not prove that provider credentials or a
remote account will accept an apply.

The [Tests workflow](../.github/workflows/ci.yml) repeats the Python, Compose,
browser, persistence, offline download, and Terraform checks for the pull
request. Its run result is separate evidence from the local checks above.
