from __future__ import annotations

import base64
import io
import json
import urllib.parse
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest
from clitestkit import Cli, cli  # noqa: F401

from pysci._cli.clients.http import HttpRequest, RawResponse, TransportError
from pysci._cli.clients.votes import Vote, verify_vote

TARGET = "sha256:" + "a" * 64
REVIEW = "sha256:" + "b" * 64
ORIGIN = "https://index.example"


class VoteIndex:
    def __init__(self) -> None:
        self.current: Vote | None = None
        self.requests: list[HttpRequest] = []
        self.enabled = True
        self.post_error: str | None = None
        self.bad_lookup = False
        self.bad_confirmation = False
        self.hints: list[dict[str, str]] = []

    def send(self, request: HttpRequest, timeout: float) -> RawResponse:
        del timeout
        self.requests.append(request)
        path = urllib.parse.urlsplit(request.url).path
        status = 200
        body: Any
        if path == "/.well-known/sci/index.json":
            body = {
                "schema": "sci-index/0.1",
                "publication_lookup_url": ORIGIN + "/api/publications/{id}/sources",
                "revocation_lookup_url": ORIGIN + "/api/keys/{x}/revocation/{file}",
                "timestamp_lookup_url": ORIGIN + "/api/publications/{id}/timestamps",
                "api_schema": "sciscape-api/0.1",
                "catalog_schema": "sciscape_catalog_v1",
                "catalog_url": ORIGIN + "/api/catalog",
            }
            if self.enabled:
                body["vote_url"] = ORIGIN + "/api/submit/vote"
        elif path == "/api/catalog/votes":
            if self.bad_lookup:
                body = [{"id": "bad", "record_base64": "bad!", "signature_base64": ""}]
            else:
                body = (
                    []
                    if self.current is None
                    else [
                        {
                            "id": self.current.id,
                            "record_base64": base64.b64encode(self.current.record).decode(),
                            "signature_base64": base64.b64encode(self.current.signature).decode(),
                        }
                    ]
                )
        elif path == "/api/submit/vote":
            if self.post_error == "lost":
                raise TransportError("response lost", sent=True)
            if self.post_error == "conflict":
                return self.reply(
                    409,
                    {
                        "error": {
                            "code": "vote_conflict",
                            "message": "Newer vote exists",
                            "retryable": False,
                        }
                    },
                )
            if self.post_error == "refused":
                return self.reply(
                    403,
                    {"error": {"code": "policy_refused", "message": "Blocked", "retryable": False}},
                )
            mime = BytesParser(policy=policy.default).parsebytes(
                (
                    f"Content-Type: {request.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n"
                ).encode()
                + request.body_bytes()
            )
            parts = {
                p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                for p in mime.iter_parts()
            }
            record, sig = parts["vote.json"], parts["vote.json.sig"]
            assert isinstance(record, bytes) and isinstance(sig, bytes)
            vote = verify_vote(record, sig)
            assert "domain" not in json.loads(record)
            hint = parts.get("account.json")
            if isinstance(hint, bytes):
                self.hints.append(json.loads(hint))
            created = self.current is None or vote.id != self.current.id
            self.current = vote
            status = 201 if created else 200
            body = {"id": vote.id, "revision": vote.revision, "created": created}
            if self.bad_confirmation:
                body["id"] = "f" * 64
        else:
            raise AssertionError(request.url)
        return self.reply(status, body)

    @staticmethod
    def reply(status: int, body: Any) -> RawResponse:
        return RawResponse(
            status, {"content-type": "application/json"}, io.BytesIO(json.dumps(body).encode())
        )


def test_vote_online_changes_withdrawal_and_retries(cli: Cli) -> None:
    assert cli("keygen").code == 0
    index = VoteIndex()
    cli.transport = index
    common = ("--to", ORIGIN, "--json")
    up = cli(
        "vote",
        TARGET,
        "--up",
        "--reason",
        "Reproduced ✓",
        "--review",
        REVIEW,
        "--account",
        "domain",
        "Reviewer.Example",
        *common,
    )
    assert up.code == 0, up.stdout
    assert up.result["submitted"] is True and up.result["revision"] == 1
    assert index.hints == [{"type": "domain", "id": "reviewer.example"}]
    assert Path(up.result["path"], "vote.json").is_file()
    repeated = cli("vote", TARGET, "--up", "--reason", "Reproduced ✓", "--review", REVIEW, *common)
    assert repeated.code == 0 and repeated.result["id"] == up.result["id"]
    assert repeated.result["revision"] == 1
    down = cli("vote", TARGET, "--down", *common)
    assert down.code == 0 and down.result["revision"] == 2
    withdrawn = cli("vote", TARGET, "--withdraw", *common)
    assert withdrawn.code == 0 and withdrawn.result["revision"] == 3
    assert index.current is not None and index.current.value == 0
    private = (cli.home / "key.pem").read_bytes()
    assert all(private not in r.body_bytes() for r in index.requests)


def test_vote_offline_and_submit_without_private_key(cli: Cli) -> None:
    assert cli("keygen").code == 0
    index = VoteIndex()
    cli.transport = index
    made = cli(
        "vote",
        TARGET,
        "--down",
        "--revision",
        "7",
        "--reason",
        "Methods issue",
        "--output",
        "opinion",
        "--json",
    )
    assert made.code == 0 and made.result["submitted"] is False
    assert not index.requests
    key = cli.home / "key.pem"
    key.unlink()
    sent = cli("vote", "--submit", "opinion", "--to", ORIGIN, "--json")
    assert sent.code == 0 and sent.result["revision"] == 7
    assert not any("/api/catalog/votes" in r.url for r in index.requests)
    assert index.current is not None and index.current.value == -1


@pytest.mark.parametrize(("error", "code"), [("conflict", 5), ("lost", 5), ("refused", 6)])
def test_vote_errors_preserve_exact_retry_bytes(cli: Cli, error: str, code: int) -> None:
    cli("keygen")
    index = VoteIndex()
    index.post_error = error
    cli.transport = index
    result = cli("vote", TARGET, "--up", "--to", ORIGIN, "--json")
    assert result.code == code, result.stdout
    saved = Path(result.result["path"])
    vote = verify_vote((saved / "vote.json").read_bytes(), (saved / "vote.json.sig").read_bytes())
    assert vote.revision == 1
    posts = [r.body_bytes() for r in index.requests if r.method == "POST"]
    assert posts and len(set(posts)) == 1
    if error == "conflict":
        assert len(posts) == 1  # never automatically override another opinion
    index.post_error = None
    retried = cli("vote", "--submit", str(saved), "--to", ORIGIN, "--json")
    assert retried.code == 0 and retried.result["id"] == vote.id


def test_vote_capability_and_untrusted_responses(cli: Cli) -> None:
    cli("keygen")
    index = VoteIndex()
    cli.transport = index
    index.enabled = False
    assert cli("vote", TARGET, "--up", "--to", ORIGIN, "--json").code == 4
    assert not any(r.method == "POST" for r in index.requests)
    index.enabled = True
    index.bad_lookup = True
    assert cli("vote", TARGET, "--up", "--to", ORIGIN, "--json").code == 3
    assert not any(r.method == "POST" for r in index.requests)
    index.bad_lookup = False
    index.bad_confirmation = True
    response = cli("vote", TARGET, "--up", "--to", ORIGIN, "--json")
    assert response.code == 3 and response.result["submitted"] is False


@pytest.mark.parametrize(
    "args",
    [
        (),
        (TARGET,),
        (TARGET, "--up", "--down"),
        (TARGET, "--up", "--refresh", "--revision", "2"),
        ("--submit", "opinion", "--refresh"),
        (TARGET, "--up", "--output", "out"),
        (TARGET, "--up", "--revision", "0"),
        (TARGET, "--up", "--revision", "2147483648"),
        (TARGET, "--up", "--revision", "1", "--account", "github", "someone"),
    ],
)
def test_vote_usage_errors(cli: Cli, args: tuple[str, ...]) -> None:
    assert cli("vote", *args, "--json").code == 2


def test_vote_signed_field_validation_and_no_clobber(cli: Cli) -> None:
    cli("keygen")
    for reason in (" ", "x" * 1001, "\x00", "\udcff"):
        result = cli(
            "vote",
            TARGET,
            "--up",
            "--reason",
            reason,
            "--revision",
            "1",
            "--output",
            "bad",
            "--json",
        )
        assert result.code == 3
    assert cli("vote", TARGET, "--up", "--revision", "1", "--output", "out", "--json").code == 0
    assert cli("vote", TARGET, "--down", "--revision", "2", "--output", "out", "--json").code == 3
    record_path = cli.cwd / "out" / "vote.json"
    assert json.loads(record_path.read_bytes())["value"] == 1
    record_path.write_bytes(record_path.read_bytes().replace(b'"value":1', b'"value":-1'))
    index = VoteIndex()
    cli.transport = index
    assert cli("vote", "--submit", "out", "--to", ORIGIN, "--json").code == 3
    assert not index.requests


def test_vote_refresh_issues_new_revision_for_unchanged_opinion(cli: Cli) -> None:
    cli("keygen")
    index = VoteIndex()
    cli.transport = index
    common = ("vote", TARGET, "--up", "--reason", "Reproduced", "--to", ORIGIN, "--json")
    original = cli(*common)
    assert original.code == 0 and original.result["revision"] == 1
    refreshed = cli(*common, "--refresh", "--account", "domain", "new.example")
    assert refreshed.code == 0 and refreshed.result["revision"] == 2
    assert refreshed.result["id"] != original.result["id"]
    assert index.hints == [{"type": "domain", "id": "new.example"}]
    retry = cli("vote", "--submit", refreshed.result["path"], "--to", ORIGIN, "--json")
    assert retry.code == 0 and retry.result["id"] == refreshed.result["id"]
