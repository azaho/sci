"""Remote behaviour against a local stdlib HTTP server (archives and an index)."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from clitestkit import (  # noqa: F401 - cli is a fixture
    Cli,
    FakeTsa,
    LocalServer,
    Reply,
    Request,
    cli,
    json_reply,
    range_reply,
    trust_config,
    write_draft,
)

from pysci.bundle import verify_directory, verify_zip
from pysci.manifest import SIGNATURE_PATH


@dataclass
class World:
    cli: Cli
    server: LocalServer
    digest: str
    zip_bytes: bytes
    loose: dict[str, bytes]
    sidecar: bytes
    lookup: Any = None
    submit_script: list[Reply] = field(default_factory=list)
    catalog: list[dict[str, Any]] = field(default_factory=list)
    catalog_enabled: bool = True
    tampered: bytes = b""

    @property
    def origin(self) -> str:
        return self.server.origin

    def descriptor(self, name: str) -> str:
        return f"{self.origin}/{name}/archive.json"

    def handle(self, request: Request) -> Reply:
        parsed = urllib.parse.urlsplit(request.path)
        path = parsed.path
        origin = self.origin
        if path == "/.well-known/sci/index.json":
            descriptor: dict[str, str] = {
                "schema": "sci-index/0.1",
                "publication_lookup_url": origin + "/api/publications/{id}/sources",
                "revocation_lookup_url": origin + "/api/keys/{x}/revocation/{file}",
                "timestamp_lookup_url": origin + "/api/publications/{id}/timestamps",
                "submit_url": origin + "/api/submit/publication",
                "api_schema": "sciscape-api/0.1",
                "revocation_url": origin + "/api/submit/key_revocation",
                "timestamp_submit_url": origin + "/api/submit/timestamp",
                "publication_url": origin + "/p/{id}",
            }
            if self.catalog_enabled:
                descriptor["catalog_schema"] = "sciscape_catalog_v1"
                descriptor["catalog_url"] = origin + "/api/catalog"
            return json_reply(200, descriptor)
        if path == f"/api/publications/{self.digest}/sources":
            if self.lookup is None:
                return json_reply(
                    404, {"error": {"code": "not_found", "message": "x", "retryable": False}}
                )
            return json_reply(200, {"descriptors": [self.descriptor(n) for n in self.lookup]})
        if path.startswith("/api/publications/") and path.endswith("/timestamps"):
            return 404, {}, b""
        if path == "/api/submit/publication":
            if self.submit_script:
                return self.submit_script.pop(0)
            body = json.loads(request.body)
            return json_reply(
                201,
                {
                    "id": body["id"],
                    "status": "accepted",
                    "publication_url": f"{origin}/p/{body['id']}",
                    "accepted_at": "2026-11-07T12:00:00Z",
                },
            )
        if path == "/api/submit/key_revocation":
            assert b'name="revocation.json"' in request.body
            record = request.body.split(b'"x":"', 1)[1].split(b'"', 1)[0].decode()
            return json_reply(200, {"x": record, "status": "revoked"})
        if path == "/api/submit/timestamp":
            assert b'name="manifest.json.sig.tsr"' in request.body
            return json_reply(200, {"id": self.digest, "retained_count": 1, "results": []})
        if path == "/api/catalog/publications":
            return self.catalog_reply(request, parsed.query)
        # archives
        name, _, rest = path.lstrip("/").partition("/")
        if rest == "archive.json":
            if name == "dead":
                return 503, {}, b"down"
            routes = {"schema": "sci-archive/0.1"}
            if name in ("a", "bad"):
                routes["entry_url"] = f"{origin}/{name}/entries/{{id}}/{{+path}}"
            if name in ("a", "b", "c", "bad"):
                routes["bundle_url"] = f"{origin}/{name}/bundles/{{id}}.zip"
            return json_reply(200, routes)
        if rest.startswith("entries/"):
            _, digest, member = rest.split("/", 2)
            if digest != self.digest:
                return 404, {}, b""
            if member == "manifest.json.sig.tsr":
                return 200, {}, self.sidecar
            if name == "bad" and member == "files/data.csv":
                return 200, {}, b"tampered"
            data = self.loose.get(member)
            return (200, {}, data) if data is not None else (404, {}, b"")
        if rest == f"bundles/{self.digest}.zip":
            data = self.tampered if name == "bad" else self.zip_bytes
            if name == "c":
                return 200, {"Content-Type": "application/zip"}, data
            return range_reply(data, request.headers.get("range"))
        return 404, {}, b"not found"

    def catalog_reply(self, request: Request, query: str) -> Reply:
        assert request.headers.get("accept-profile") == "sciscape_catalog_v1"
        params = urllib.parse.parse_qs(query)
        rows = sorted(self.catalog, key=lambda r: (r["accepted_at"], r["id"]), reverse=True)
        if "or" in params:
            text = params["or"][0]
            time = text.split("accepted_at.lt.", 1)[1].split(",", 1)[0]
            last = text.split("id.lt.", 1)[1].removesuffix("))")
            rows = [r for r in rows if (r["accepted_at"], r["id"]) < (time, last)]
        if "search_text" in params:
            assert params["search_text"][0].startswith("plfts(simple).")
        limit = int(params["limit"][0])
        assert params["order"] == ["accepted_at.desc,id.desc"]
        return json_reply(200, rows[:limit])


@pytest.fixture
def world(cli: Cli) -> Iterator[World]:
    cli("keygen")
    draft = write_draft(cli.cwd / "draft")
    run = cli("sign", str(draft), "--no-timestamp", "--output", str(cli.cwd / "campaign"), "--json")
    digest = run.result["publication_id"].removeprefix("sha256:")
    zip_bytes = (cli.cwd / "campaign" / f"{digest}.zip").read_bytes()
    loose = {
        p.relative_to(draft).as_posix(): p.read_bytes() for p in draft.rglob("*") if p.is_file()
    }
    tsa = FakeTsa()
    sidecar = tsa.respond(_request_for(loose[SIGNATURE_PATH]))
    trust_config(cli.home, tsa.root_pem)
    tampered = bytearray(zip_bytes)
    tampered[-200] ^= 0xFF
    holder: dict[str, World] = {}
    server = LocalServer(lambda request: holder["w"].handle(request))
    w = World(cli, server, digest, zip_bytes, loose, sidecar, tampered=bytes(tampered))
    holder["w"] = w
    with server:
        yield w


def _request_for(signature: bytes) -> bytes:
    from pysci.timestamps import build_timestamp_request

    return build_timestamp_request(signature).der


def test_pull_via_index_with_second_archive_fallback_and_proofs(world: World) -> None:
    cli = world.cli
    world.lookup = ["dead", "a"]
    out = cli.cwd / "copy"
    run = cli(
        "pull", f"sci:sha256:{world.digest}", "--in", world.origin, "--output", str(out), "--json"
    )
    assert run.code == 0, run.stdout
    result = run.result
    assert result["source"]["descriptor"] == world.descriptor("a")
    assert result["source_failures"][0]["descriptor"] == world.descriptor("dead")
    assert verify_directory(out, expected_id=world.digest).ok
    checks = result["checks"]
    assert checks["bundle"]["status"] == "valid"
    assert checks["timestamps"]["status"] == "valid"
    assert (out / "manifest.json.sig.tsr").read_bytes() == world.sidecar
    assert not [p for p in out.parent.iterdir() if p.name.startswith(".sci-tmp")]
    # an existing complete copy is kept; other nonempty destinations are rejected
    run = cli(
        "pull", f"sha256:{world.digest}", "--in", world.origin, "--output", str(out), "--json"
    )
    assert run.code == 0 and run.result["status"] == "already_present"
    other = cli.cwd / "other"
    other.mkdir()
    (other / "x").write_text("x")
    run = cli("pull", f"sha256:{world.digest}", "--in", world.origin, "--output", str(other))
    assert run.code == 2


def test_pull_zip_and_failures(world: World) -> None:
    cli = world.cli
    run = cli(
        "pull",
        f"sha256:{world.digest}",
        "--archive",
        world.descriptor("b"),
        "--zip",
        "--output",
        str(cli.cwd / "folder"),
        "--json",
    )
    assert run.code == 0, run.stdout
    assert verify_zip(cli.cwd / "folder" / f"{world.digest}.zip", expected_id=world.digest).ok
    # no known sources: 404 and empty list
    world.lookup = None
    cli("index", world.origin)
    run = cli("pull", f"sha256:{world.digest}", "--json")
    assert run.code == 5 and run.json["errors"][0]["code"] == "no_known_sources"
    world.lookup = []
    assert cli("pull", f"sha256:{world.digest}").code == 5
    # every source serves bad content -> 3; unreachable -> 5
    run = cli(
        "pull",
        f"sha256:{world.digest}",
        "--archive",
        world.descriptor("bad"),
        "--output",
        str(cli.cwd / "bad"),
        "--json",
    )
    assert run.code == 3 and not (cli.cwd / "bad").exists()
    run = cli(
        "pull",
        f"sha256:{world.digest}",
        "--archive",
        world.descriptor("dead"),
        "--output",
        str(cli.cwd / "dead"),
        "--json",
    )
    assert run.code == 5
    # missing index configuration
    assert (
        world.cli.__class__(home=cli.home.parent / "fresh", cwd=cli.cwd)(
            "pull", f"sha256:{world.digest}"
        ).code
        == 2
    )


def test_read_by_id_entry_url_then_ranges_then_full_zip(world: World) -> None:
    cli = world.cli
    world.lookup = ["a"]
    run = cli("read", f"sha256:{world.digest}", "--in", world.origin, "--json")
    assert run.code == 0, run.stdout
    assert run.result["metadata"]["title"] == "Neural decoding of spikes"
    assert not any("/bundles/" in r.path for r in world.server.requests)
    # bundle-only archive: ZIP range requests
    world.server.requests.clear()
    run = cli(
        "read",
        f"sha256:{world.digest}",
        "--archive",
        world.descriptor("b"),
        "--file",
        "files/data.csv",
        "--json",
    )
    assert run.code == 0, run.stdout
    assert run.result["text"]["lines"][0] == "a,b"
    bundle_requests = [r for r in world.server.requests if "/bundles/" in r.path]
    assert bundle_requests and all("range" in r.headers for r in bundle_requests)
    # no range support: complete ZIP within the limits
    world.server.requests.clear()
    run = cli(
        "read", f"sha256:{world.digest}", "--archive", world.descriptor("c"), "--list", "--json"
    )
    assert run.code == 0
    assert any("/bundles/" in r.path and "range" not in r.headers for r in world.server.requests)
    # a tampered file from the only source fails the file check
    run = cli(
        "read",
        f"sha256:{world.digest}",
        "--archive",
        world.descriptor("bad"),
        "--file",
        "files/data.csv",
        "--json",
    )
    assert run.code == 3
    # tampered entry at the first source, good second source
    run = cli(
        "read",
        f"sha256:{world.digest}",
        "--archive",
        world.descriptor("bad"),
        "--archive",
        world.descriptor("a"),
        "--file",
        "files/data.csv",
        "--json",
    )
    assert run.code == 0


def test_submit_success_retry_refusal_and_outcome_unknown(world: World) -> None:
    cli = world.cli
    archive = world.descriptor("a")
    run = cli(
        "submit",
        str(cli.cwd / "campaign"),
        "--to",
        world.origin,
        "--archive",
        archive,
        "--account",
        "domain",
        "Lab.Example",
        "--json",
    )
    assert run.code == 0, run.stdout
    submission = run.result["submissions"][0]
    assert submission["status"] == "registered"
    assert submission["response"]["status"] == "accepted"
    request = [r for r in world.server.requests if r.path == "/api/submit/publication"][-1]
    assert json.loads(request.body) == {
        "id": world.digest,
        "descriptors": [archive],
        "account": {"type": "domain", "id": "lab.example"},
    }
    # temporary failure with Retry-After, then success
    world.submit_script = [
        json_reply(
            429,
            {"error": {"code": "busy", "message": "busy", "retryable": True}},
            {"Retry-After": "2"},
        ),
    ]
    cli.sleeps.clear()
    run = cli(
        "submit", f"sha256:{world.digest}", "--to", world.origin, "--archive", archive, "--json"
    )
    assert run.code == 0
    assert run.result["submissions"][0]["attempts"] == 2
    assert any(2 <= s <= 2.25 for s in cli.sleeps)
    # policy refusal: no retry, exit 6
    world.server.requests.clear()
    world.submit_script = [
        json_reply(403, {"error": {"code": "not_baseline", "message": "no", "retryable": False}})
    ] * 3
    run = cli(
        "submit", f"sha256:{world.digest}", "--to", world.origin, "--archive", archive, "--json"
    )
    assert run.code == 6
    assert len([r for r in world.server.requests if r.path == "/api/submit/publication"]) == 1
    assert run.result["submissions"][0]["error"]["code"] == "not_baseline"
    # validation refusal: exit 3
    world.submit_script = [
        json_reply(
            422, {"error": {"code": "invalid_signature", "message": "bad", "retryable": False}}
        )
    ]
    assert (
        cli("submit", f"sha256:{world.digest}", "--to", world.origin, "--archive", archive).code
        == 3
    )
    # lost responses: outcome_unknown
    world.submit_script = [(0, {}, b"")] * 10
    run = cli(
        "submit", f"sha256:{world.digest}", "--to", world.origin, "--archive", archive, "--json"
    )
    assert run.code == 5
    assert run.result["submissions"][0]["status"] == "outcome_unknown"
    assert run.result["submissions"][0]["error"]["retryable"] is True
    world.submit_script = []
    # invalid response contract
    world.submit_script = [json_reply(200, {"id": "0" * 64})]
    assert (
        cli("submit", f"sha256:{world.digest}", "--to", world.origin, "--archive", archive).code
        == 3
    )
    assert cli("submit", f"sha256:{world.digest}", "--to", world.origin).code == 2


def test_remote_search_pagination_and_capabilities(world: World) -> None:
    cli = world.cli
    key = {"kty": "OKP", "crv": "Ed25519", "x": "A" * 43}
    world.catalog = [
        {
            "id": f"{i:064x}",
            "title": f"T{i}",
            "abstract": "A",
            "public_key": key,
            "accepted_at": f"2026-11-0{i}T00:00:00Z",
        }
        for i in range(1, 4)
    ]
    run = cli("search", "neural decoding", "--in", world.origin, "--limit", "2", "--json")
    assert run.code == 0, run.stdout
    group = run.result["groups"][0]
    assert group["kind"] == "index"
    assert "not verified" in group["verification"] or "not retrieved" in group["verification"]
    assert [hit["title"] for hit in group["results"]] == ["T3", "T2"]
    assert group["results"][0]["publication_url"] == f"{world.origin}/p/{3:064x}"
    run = cli("search", "--cursor", run.result["next_cursor"], "--json")
    assert [hit["title"] for hit in run.result["groups"][0]["results"]] == ["T1"]
    assert run.result["next_cursor"] is None
    # local + remote together; a failing remote marks the result partial
    run = cli(
        "search",
        "decoding",
        "--in",
        str(cli.cwd / "campaign"),
        "--in",
        world.origin,
        "--in",
        "https://127.0.0.1:1",
        "--json",
        "--timeout",
        "2",
    )
    assert run.code == 5 and run.json["status"] == "partial"
    assert len(run.result["groups"]) == 3
    assert run.result["groups"][0]["results"]
    # unsupported catalog
    world.catalog_enabled = False
    run = cli("search", "x", "--in", world.origin, "--json")
    assert run.code == 4
    assert run.json["errors"][0]["code"] == "unsupported_capability"
    assert cli("search", "x", "--in", "@index").code == 2


def test_revoke_and_timestamp_submission(world: World) -> None:
    cli = world.cli
    cli("revoke", "--output", str(cli.cwd / "rev"))
    run = cli("revoke", "--submit", str(cli.cwd / "rev"), "--to", world.origin, "--json")
    assert run.code == 0, run.stdout
    assert run.result["status"] == "ok" and run.result["response"]["status"] == "revoked"
    # timestamp --submit requires a sidecar
    draft = cli.cwd / "draft"
    assert cli("timestamp", str(draft), "--submit", "--to", world.origin).code == 2
    (draft / "manifest.json.sig.tsr").write_bytes(world.sidecar)
    run = cli("timestamp", str(draft), "--submit", "--to", world.origin, "--json")
    assert run.code == 0, run.stdout
    assert run.result["response"]["retained_count"] == 1
    assert run.result["local_checks"]["status"] == "valid"
    assert cli("timestamp", str(draft), "--submit", "--tsa", "https://x.example").code == 2


def test_http_rejected_for_non_loopback(cli: Cli) -> None:
    assert cli("pull", "sha256:" + "0" * 64, "--archive", "http://example.org/a.json").code == 2
    assert cli("pull", "sha256:" + "0" * 64, "--in", "http://example.org").code == 2
    assert cli("pull", "sha256:XYZ").code == 2


def test_descriptor_unsupported_schema(world: World, tmp_path: Path) -> None:
    def handler(request: Request) -> Reply:
        return json_reply(200, {"schema": "sci-archive/9.9"})

    with LocalServer(handler) as other:
        run = world.cli(
            "pull",
            f"sha256:{world.digest}",
            "--archive",
            f"{other.origin}/archive.json",
            "--output",
            str(tmp_path / "o"),
            "--json",
        )
    assert run.code == 4


@pytest.mark.parametrize(
    ("repo", "descriptor"),
    [
        (
            "git@github.com:lab/research.git",
            "https://raw.githubusercontent.com/lab/research/main/sci/archive.json",
        ),
        (
            "https://huggingface.co/datasets/lab/data",
            "https://huggingface.co/datasets/lab/data/resolve/main/sci/archive.json",
        ),
    ],
)
def test_submit_repository_address_sends_descriptor(
    world: World, repo: str, descriptor: str
) -> None:
    run = world.cli(
        "submit",
        "sha256:" + world.digest,
        "--to",
        world.origin,
        "--archive",
        repo,
        "--archive",
        descriptor,
        "--account",
        "domain",
        "Lab.Example",
        "--json",
    )
    assert run.code == 0, run.stdout
    assert run.result["descriptors"] == [descriptor]
    posts = [r for r in world.server.requests if r.path == "/api/submit/publication"]
    assert len(posts) == 1
    assert json.loads(posts[0].body) == {
        "id": world.digest,
        "descriptors": [descriptor],
        "account": {"type": "domain", "id": "lab.example"},
    }
