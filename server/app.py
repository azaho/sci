"""SCI publishing service.

Endpoints:
    POST /publications                  submit sci.json, manifest.json, signature
    POST /publications/{uuid}/complete  verify uploaded files and accept
    GET  /health                        readiness (database and storage)

Commands:
    python -m server.app serve
    python -m server.app rebuild-catalog [--dry-run]
    python -m server.app init-local      (local installations: buckets + descriptor)

The service verifies every publication before acceptance using the shared
``sci.publication`` code. Finalization follows DESIGN.md "Push: what happens"
and the recovery table in "SQL and object storage".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import psycopg
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.routing import Route

from sci import __version__, keys
from sci import publication as pub

from .database import Database
from .errors import ApiError, json_response
from .publications import Publisher
from .recovery import rebuild_catalog
from .request_io import RateLimiter, client_id, parse_multipart, read_capped
from .settings import Settings

log = logging.getLogger("sci.server")

# Multipart overhead allowance on top of the 64 KiB JSON limit and signature.
MAX_SUBMISSION_BODY = pub.MAX_JSON_BYTES + keys.SIGNATURE_BYTES + 16_384
MAX_COMPLETE_BODY = 4_096



def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> Starlette:
    settings = settings or Settings.from_env()
    state: dict = {}
    limiter = RateLimiter(settings.rate_limit_per_minute)

    def publisher() -> Publisher:
        return state["publisher"]

    def guard(request: Request) -> None:
        if not settings.writes_enabled:
            raise ApiError(503, "writes_disabled", "Publishing is temporarily disabled on this server.")
        ok, retry = limiter.allow(client_id(request, settings.client_ip_header))
        if not ok:
            raise ApiError(429, "rate_limited", "Too many publishing requests; retry later.", headers={"Retry-After": str(retry)})

    async def submit(request: Request):
        try:
            guard(request)
            body = await read_capped(request, MAX_SUBMISSION_BODY)
            parts = parse_multipart(body, request.headers.get("content-type", ""))
            if len(parts["sci"]) + len(parts["manifest"]) > pub.MAX_JSON_BYTES:
                raise ApiError(413, "too_large", f"sci.json and manifest.json together exceed {pub.MAX_JSON_BYTES} bytes.")
            status, result = await run_in_threadpool(publisher().prepare, parts["sci"], parts["manifest"], parts["signature"])
            return json_response(result, status)
        except ApiError as exc:
            return exc.response()

    async def complete(request: Request):
        try:
            guard(request)
            uid = request.path_params["uuid"]
            if not pub.is_uuid4(uid):
                raise ApiError(400, "invalid_uuid", "The publication ID must be a canonical lowercase UUIDv4.")
            body = await read_capped(request, MAX_COMPLETE_BODY)
            try:
                doc = pub.parse_strict_json(body, "request")
            except pub.PublicationError as exc:
                raise ApiError(400, "invalid_request", str(exc)) from exc
            if set(doc) != {"upload_id", "manifest_sha256"}:
                raise ApiError(400, "invalid_request", "Send a JSON object with exactly upload_id and manifest_sha256.")
            upload_id, mhash = doc["upload_id"], doc["manifest_sha256"]
            if not isinstance(upload_id, str) or not pub.is_uuid4(upload_id):
                raise ApiError(400, "invalid_request", "upload_id must be the ID returned by POST /publications.")
            if not isinstance(mhash, str) or not pub._HASH_RE.fullmatch(mhash):
                raise ApiError(400, "invalid_request", "manifest_sha256 must be 64 lowercase hexadecimal characters.")
            status, result = await run_in_threadpool(publisher().complete, uid, upload_id, mhash)
            return json_response(result, status)
        except ApiError as exc:
            return exc.response()

    async def health(request: Request):
        """200 while the process and database work. Storage status is reported
        per bucket but does not fail the check, so an R2 problem cannot cause
        restart loops; publishing requests then fail with 503 storage_unavailable."""
        try:
            await run_in_threadpool(publisher().db.ping)
        except Exception as exc:  # noqa: BLE001 - readiness reports any failure
            log.warning("health check failed: database: %s", exc)
            return json_response({"status": "unavailable", "database": "error"}, 503)
        buckets = await run_in_threadpool(publisher().storage.bucket_status)
        degraded = any(v != "ok" for v in buckets.values())
        if degraded:
            log.warning("storage check failed: %s", buckets)
        return json_response({
            "status": "degraded" if degraded else "ok",
            "version": __version__,
            "writes_enabled": settings.writes_enabled,
            "database": "ok",
            "storage": buckets,
        })

    async def root(request: Request):
        return json_response({"service": "sci-publisher", "version": __version__})

    async def not_found(request: Request, exc):
        return json_response({"error": {"code": "not_found", "message": "Not found."}}, 404)

    async def method_not_allowed(request: Request, exc):
        status = getattr(exc, "status_code", 500)
        code = {405: "method_not_allowed"}.get(status, "error")
        return json_response({"error": {"code": code, "message": getattr(exc, "detail", "Error.")}}, status)

    async def server_error(request: Request, exc):
        log.exception("unhandled error", exc_info=exc)
        return json_response({"error": {"code": "internal_error", "message": "Internal server error."}}, 500)

    async def worker_loop():
        while True:
            try:
                await run_in_threadpool(publisher().run_maintenance_once)
            except Exception:  # noqa: BLE001
                log.exception("maintenance run failed")
            await asyncio.sleep(settings.worker_interval_seconds)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        db = Database(settings.database_url)
        storage = settings.storage()
        state["publisher"] = Publisher(settings, db, storage)
        try:
            db.save_settings(settings.instance_url, settings.archive_url)
        except psycopg.Error as exc:
            log.warning("could not save instance settings: %s", exc)
        task = asyncio.create_task(worker_loop()) if start_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            db.close()

    app = Starlette(
        routes=[
            Route("/", root, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/publications", submit, methods=["POST"]),
            Route("/publications/{uuid}/complete", complete, methods=["POST"]),
        ],
        exception_handlers={404: not_found, 405: method_not_allowed, 500: server_error},
        lifespan=lifespan,
    )
    app.state.sci = state
    return app



def descriptor(settings: Settings, *, catalog_url: str, publishing_api_url: str, publishable_key: str | None = None) -> dict:
    d = {
        "schema_version": 1,
        "publishing_api_url": publishing_api_url,
        "catalog_url": catalog_url,
        "catalog_schema": "catalog_v1",
        "archive_url": settings.archive_url,
    }
    if publishable_key:
        d["supabase_publishable_key"] = publishable_key
    return d



def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("SCI_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m server.app", description="SCI publishing service")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the HTTP service")
    s.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    r = sub.add_parser("rebuild-catalog", help="rebuild the catalog from archive acceptance records")
    r.add_argument("--database-url", default=os.environ.get("SCI_ADMIN_DATABASE_URL"))
    r.add_argument("--dry-run", action="store_true")
    i = sub.add_parser("init-local", help="create local buckets and write sci-server.json")
    i.add_argument("--descriptor", required=True, help="output path for sci-server.json")
    i.add_argument("--catalog-url", required=True)
    i.add_argument("--publishing-api-url", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=False, access_log=True)
        return 0
    settings = Settings.from_env()
    if args.cmd == "rebuild-catalog":
        if not args.database_url:
            print("error: set SCI_ADMIN_DATABASE_URL or pass --database-url", file=sys.stderr)
            return 2
        return 1 if rebuild_catalog(settings, args.database_url, dry_run=args.dry_run) else 0
    if args.cmd == "init-local":
        for attempt in range(60):  # storage may still be starting
            try:
                settings.storage().ensure_buckets()
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 59:
                    raise
                log.info("waiting for storage: %s", exc)
                time.sleep(2)
        d = descriptor(settings, catalog_url=args.catalog_url, publishing_api_url=args.publishing_api_url)
        path = Path(args.descriptor)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
        return 0
    return 2



if __name__ == "__main__":
    sys.exit(main())
