"""GitHub publishing: shared releases, request counts, batches, reruns, and rate limits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from clitestkit import Cli, cli, write_draft  # noqa: F401 - cli is a fixture
from fakehosts import FakeGitHub

from sci.clients import github
from sci.clients.http import HttpRequest

REMOTE = "https://github.com/lab/research"


def campaign(cli: Cli, count: int) -> tuple[Path, list[str]]:
    cli("keygen")
    folder = cli.cwd / "campaign"
    digests = []
    for number in range(count):
        draft = write_draft(cli.cwd / f"d{number}", title=f"Paper {number}")
        run = cli("sign", str(draft), "--no-timestamp", "--output", str(folder), "--json")
        digests.append(run.result["publication_id"].removeprefix("sha256:"))
    return folder, sorted(digests)


def prepared(cli: Cli, fake: FakeGitHub) -> None:
    fake.create()
    cli.transport = fake
    cli.env["GH_TOKEN"] = "t"


def publish(cli: Cli, folder: Path) -> Any:
    return cli("publish", str(folder), "--to", REMOTE, "--json")


def api_calls(fake: FakeGitHub) -> list[tuple[str, str]]:
    hosts = ("api.github.com", "uploads.github.com")
    return [call for call in fake.log if any(host in call[1] for host in hosts)]


def test_one_bundle_costs_the_same_in_a_large_archive(cli: Cli) -> None:
    folder, _ = campaign(cli, 1)
    small, large = FakeGitHub(), FakeGitHub()
    large.releases = [{"id": n + 10, "tag_name": f"old-{n}", "assets": []} for n in range(6000)]
    for fake in (small, large):
        fake.create()
        fake.log.clear()
        cli.transport, cli.env["GH_TOKEN"] = fake, "t"
        assert publish(cli, folder).code == 0
    assert len(api_calls(small)) == len(api_calls(large))
    assert not [url for _, url in api_calls(large) if url.endswith("/releases?per_page=100&page=1")]


def test_writes_per_bundle(cli: Cli) -> None:
    folder, _ = campaign(cli, 1)
    fake = FakeGitHub()
    prepared(cli, fake)
    fake.log.clear()
    assert publish(cli, folder).code == 0
    writes = [(m, u) for m, u in api_calls(fake) if m in ("POST", "PATCH")]
    assert len([u for _, u in writes if u.endswith("/git/blobs")]) == 2  # signature and PNG
    assert len(writes) == 7  # 2 blobs, tree, commit, ref, release, asset


def test_bundles_share_a_release_and_download_individually(cli: Cli) -> None:
    folder, digests = campaign(cli, 1)
    fake = FakeGitHub()
    prepared(cli, fake)
    fake.releases.append(
        {
            "id": 7,
            "tag_name": f"sci-{digests[0][:2]}",
            "draft": False,
            "assets": [{"name": "other.zip", "state": "uploaded", "id": 1}],
            "upload_url": "https://uploads.github.com/repos/lab/research/releases/7/assets{?name}",
        }
    )
    assert publish(cli, folder).code == 0
    assert len(fake.releases) == 1
    assert [a["name"] for a in fake.releases[0]["assets"]] == ["other.zip", f"{digests[0]}.zip"]


@pytest.mark.parametrize(
    "step",
    [
        ("POST", "/releases"),
        ("POST", "uploads.github.com"),
        ("POST", "/git/blobs"),
        ("POST", "/git/trees"),
        ("POST", "/git/commits"),
    ],
)
def test_interrupted_publish_can_be_rerun(cli: Cli, step: tuple[str, str]) -> None:
    folder, digests = campaign(cli, 2)
    fake = FakeGitHub()
    prepared(cli, fake)
    fake.replies.append((step[0], step[1], 500, {}, b"{}"))
    assert publish(cli, folder).code != 0
    run = publish(cli, folder)
    assert run.code == 0, run.stdout
    assert (fake.read("sci/publications.txt") or b"").decode().split() == digests
    names = [a["name"] for release in fake.releases for a in release["assets"]]
    assert sorted(names) == [f"{digest}.zip" for digest in digests]


def test_commits_in_batches(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "BATCH_SIZE", 2)
    folder, digests = campaign(cli, 5)
    fake = FakeGitHub()
    prepared(cli, fake)
    before = len(fake.commits)
    run = publish(cli, folder)
    assert run.code == 0, run.stdout
    assert len(fake.commits) - before == 3
    assert (fake.read("sci/publications.txt") or b"").decode().split() == digests


def test_failed_batch_keeps_earlier_batches(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "BATCH_SIZE", 2)
    folder, digests = campaign(cli, 5)
    fake = FakeGitHub()
    prepared(cli, fake)
    calls = {"commits": 0}

    def fail_second_commit(request: HttpRequest) -> None:
        if request.method == "POST" and request.url.endswith("/git/commits"):
            calls["commits"] += 1
            if calls["commits"] == 2:
                fake.replies.append(("POST", "/git/commits", 500, {}, b"{}"))

    fake.hook = fail_second_commit
    run = publish(cli, folder)
    assert run.code == 5 and run.json["status"] == "partial"
    statuses = [result["status"] for result in run.result["results"]]
    assert statuses.count("hosted") == 2 and statuses.count("failed") == 3
    assert len((fake.read("sci/publications.txt") or b"").decode().split()) == 2
    fake.hook = None
    assert publish(cli, folder).code == 0
    assert (fake.read("sci/publications.txt") or b"").decode().split() == digests


def test_waits_on_rate_limits_and_reports_them(cli: Cli) -> None:
    folder, _ = campaign(cli, 1)
    fake = FakeGitHub()
    prepared(cli, fake)
    fake.replies.append(("POST", "/git/commits", 403, {"retry-after": "30"}, b"{}"))
    fake.replies.append(
        ("POST", "/git/trees", 403, {}, b'{"message":"You have exceeded a secondary rate limit"}')
    )
    cli.sleeps.clear()
    run = publish(cli, folder)
    assert run.code == 0, run.stdout
    assert cli.sleeps == [60.0, 30.0]
    assert "waiting 30s" in run.stderr


def test_a_wait_beyond_the_limit_is_a_retryable_error(cli: Cli) -> None:
    folder, _ = campaign(cli, 1)
    fake = FakeGitHub()
    prepared(cli, fake)
    fake.replies.append(("POST", "/git/commits", 403, {"retry-after": "99999"}, b"{}"))
    run = publish(cli, folder)
    assert run.code == 5
    error = run.json["errors"][0]
    assert error["code"] == "rate_limited" and error["retryable"] is True


def test_changed_inline_text_stops_before_main_moves(cli: Cli) -> None:
    folder, _ = campaign(cli, 1)
    fake = FakeGitHub()
    prepared(cli, fake)
    head = fake.main
    fake.mangle_inline = True
    run = publish(cli, folder)
    assert run.code == 5 and run.json["errors"][0]["code"] == "upload_corrupted"
    assert fake.main == head


def test_another_publisher_creating_the_release_or_asset_first(cli: Cli) -> None:
    folder, digests = campaign(cli, 1)
    fake = FakeGitHub()
    prepared(cli, fake)
    data = (folder / f"{digests[0]}.zip").read_bytes()
    tag = f"sci-{digests[0][:2]}"
    state = {"release": False, "asset": False}

    def race(request: HttpRequest) -> None:
        if request.method == "POST" and request.url.endswith("/releases") and not state["release"]:
            state["release"] = True
            fake.releases.append(
                {
                    "id": 9,
                    "tag_name": tag,
                    "draft": False,
                    "assets": [],
                    "upload_url": "https://uploads.github.com/repos/lab/research/releases/9/assets",
                }
            )
        if "uploads.github.com" in request.url and not state["asset"]:
            state["asset"] = True
            import hashlib

            fake.assets[1] = data
            fake.releases[0]["assets"].append(
                {
                    "id": 1,
                    "name": f"{digests[0]}.zip",
                    "state": "uploaded",
                    "size": len(data),
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "url": "https://api.github.com/repos/lab/research/releases/assets/1",
                }
            )

    fake.hook = race
    run = publish(cli, folder)
    assert run.code == 0, run.stdout
    assert run.result["results"][0]["bundle"] == "reused" and len(fake.releases) == 1
