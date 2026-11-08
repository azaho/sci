"""Timestamping with a fake TSA (test-only corpus PKI) and an opt-in live FreeTSA test."""

from __future__ import annotations

import os

import pytest
from clitestkit import Cli, FakeTsa, LocalServer, Reply, Request, cli, trust_config, write_draft  # noqa: F401 - cli is a fixture

from pysci.der import frame_values


def test_sign_with_timestamp_and_verify(cli: Cli) -> None:
    tsa = FakeTsa()
    trust_config(cli.home, tsa.root_pem)
    cli("keygen")
    draft = write_draft(cli.cwd / "draft")
    with LocalServer(tsa.handler) as server:
        run = cli(
            "sign",
            str(draft),
            "--tsa",
            f"{server.origin}/tsr",
            "--output",
            str(cli.cwd / "c"),
            "--json",
        )
        assert run.code == 0, run.stdout
        assert run.result["timestamp"]["status"] == "appended"
        assert run.result["timestamp"]["proof"]["result"] == "verified"
        digest = run.result["publication_id"].removeprefix("sha256:")
        assert (cli.cwd / "c" / f"{digest}.tsr").read_bytes() == (
            draft / "manifest.json.sig.tsr"
        ).read_bytes()
        # unchanged re-sign keeps the existing sidecar without a new request
        before = len(server.requests)
        run = cli("sign", str(draft), "--tsa", f"{server.origin}/tsr", "--json")
        assert run.result["timestamp"]["status"] == "existing"
        assert len(server.requests) == before
        # explicit timestamp appends a second proof
        run = cli(
            "timestamp",
            str(cli.cwd / "c" / f"{digest}.zip"),
            "--tsa",
            f"{server.origin}/tsr",
            "--json",
        )
        assert run.code == 0
    run = cli("verify", str(cli.cwd / "c" / f"{digest}.zip"), "--json")
    assert run.code == 0
    timestamps = run.result["checks"]["timestamps"]
    assert timestamps["status"] == "valid" and len(timestamps["proofs"]) == 2
    assert all(p["result"] == "verified" for p in timestamps["proofs"])
    # without the test anchor, the same proofs are untrusted, not invalid
    trust_config(cli.home, tsa.root_pem, freetsa=True)
    (cli.home / "config.json").write_text('{"timestamp_trust": {"freetsa": true}}')
    run = cli("verify", str(draft), "--json")
    assert run.code == 0
    assert run.result["checks"]["timestamps"]["status"] == "untrusted"


def test_timestamp_failure_is_partial_and_keeps_signature(cli: Cli) -> None:
    cli("keygen")
    draft = write_draft(cli.cwd / "draft")

    def failing(request: Request) -> Reply:
        return 503, {}, b"busy"

    with LocalServer(failing) as server:
        run = cli(
            "sign",
            str(draft),
            "--tsa",
            f"{server.origin}/tsr",
            "--output",
            str(cli.cwd / "c"),
            "--json",
            "--timeout",
            "5",
        )
    assert run.code == 5 and run.json["status"] == "partial"
    assert run.result["publication_id"].startswith("sha256:")
    assert run.result["timestamp"]["status"] == "failed"
    assert run.result["output"]["status"] == "stored"
    assert (draft / "manifest.json.sig").exists()
    assert cli("verify", str(draft)).code == 0


def test_bad_tsa_reply_and_malformed_sidecar(cli: Cli) -> None:
    tsa = FakeTsa()
    trust_config(cli.home, tsa.root_pem)
    cli("keygen")
    draft = write_draft(cli.cwd / "draft")
    cli("sign", str(draft), "--no-timestamp")

    def wrong_imprint(request: Request) -> Reply:
        from pysci.timestamps import build_timestamp_request

        other = build_timestamp_request(b"other signature").der
        return 200, {}, tsa.respond(other)

    with LocalServer(wrong_imprint) as server:
        run = cli("timestamp", str(draft), "--tsa", f"{server.origin}/tsr", "--json")
    assert run.code == 5
    assert run.result["timestamp"]["error"]["code"] == "timestamp_response_rejected"
    assert not (draft / "manifest.json.sig.tsr").exists()

    (draft / "manifest.json.sig.tsr").write_bytes(b"\x30\x82\xff")
    with LocalServer(tsa.handler) as server:
        run = cli("timestamp", str(draft), "--tsa", f"{server.origin}/tsr", "--json")
    assert run.code == 3
    assert (draft / "manifest.json.sig.tsr").read_bytes() == b"\x30\x82\xff"


def test_revocation_timestamp(cli: Cli) -> None:
    tsa = FakeTsa()
    trust_config(cli.home, tsa.root_pem)
    cli("keygen")
    cli("revoke", "--output", str(cli.cwd / "rev"))
    with LocalServer(tsa.handler) as server:
        run = cli("timestamp", str(cli.cwd / "rev"), "--tsa", f"{server.origin}/tsr", "--json")
    assert run.code == 0 and run.result["target"] == "revocation"
    data = (cli.cwd / "rev" / "revocation.json.sig.tsr").read_bytes()
    assert len(frame_values(data).values) == 1


def test_tsa_requests_are_paced(cli: Cli) -> None:
    cli.pace_interval = 1.0
    tsa = FakeTsa()
    trust_config(cli.home, tsa.root_pem)
    cli("keygen")
    draft = write_draft(cli.cwd / "draft")
    cli("sign", str(draft), "--no-timestamp")
    with LocalServer(tsa.handler) as server:
        for _ in range(2):
            assert cli("timestamp", str(draft), "--tsa", f"{server.origin}/tsr").code == 0
    assert any(0 < s <= 1.0 for s in cli.sleeps)
    assert (cli.home / "pace").exists()


@pytest.mark.skipif(os.environ.get("SCI_NETWORK_TESTS") != "1", reason="network test")
def test_live_freetsa_timestamp(cli: Cli) -> None:
    cli.sleeps = []
    cli("keygen")
    draft = write_draft(cli.cwd / "draft")
    run = cli("sign", str(draft), "--json", "--timeout", "60")
    assert run.code == 0, run.stdout
    assert run.result["timestamp"]["tsa"] == "https://freetsa.org/tsr"
    assert run.result["timestamp"]["proof"]["result"] == "verified"
    run = cli("verify", str(draft), "--json")
    assert run.result["checks"]["timestamps"]["status"] == "valid"
