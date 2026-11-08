"""Publishing to GitHub and Hugging Face through injected fake transports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from clitestkit import Cli, cli, write_draft  # noqa: F401 - cli is a fixture
from fakehosts import FakeGitHub, FakeHub

from sci.destinations import parse_remote, resolve_destinations
from sci.errors import CliError


def _campaign(cli: Cli, count: int = 2) -> tuple[Path, list[str]]:
    cli("keygen")
    campaign = cli.cwd / "campaign"
    digests = []
    for i in range(count):
        draft = write_draft(cli.cwd / f"d{i}", title=f"Paper {i}")
        run = cli("sign", str(draft), "--no-timestamp", "--output", str(campaign), "--json")
        digests.append(run.result["publication_id"].removeprefix("sha256:"))
    digests.sort()
    (campaign / f"{digests[0]}.tsr").write_bytes(b"")
    return campaign, digests


def test_destination_parsing() -> None:
    https = parse_remote("https://github.com/Lab/Research.git/")
    ssh = parse_remote("git@github.com:lab/research.git")
    url = parse_remote("ssh://git@github.com/lab/research")
    assert https and ssh and url and https.key == ssh.key == url.key
    hf = parse_remote("git@hf.co:datasets/lab/data")
    assert (
        hf and hf.kind == "huggingface" and hf.display == "https://huggingface.co/datasets/lab/data"
    )
    assert parse_remote("./folder") is None
    for bad in (
        "https://github.com/lab/research/tree/main",
        "https://github.com/lab/research?x=1",
        "https://github.com/lab/research#readme",
        "https://huggingface.co/lab/model",
        "file:///tmp/x",
        "http://github.com/lab/research",
    ):
        with pytest.raises(CliError):
            parse_remote(bad)
    destinations = resolve_destinations(
        ["https://github.com/lab/research", "git@github.com:lab/research.git"], [], Path("/")
    )
    assert len(destinations) == 1


def test_github_publish_create_reuse_and_conflict(cli: Cli) -> None:
    campaign, digests = _campaign(cli)
    fake = FakeGitHub()
    cli.transport = fake
    cli.env["GH_TOKEN"] = "gh-test-token"
    run = cli("publish", str(campaign), "--to", "https://github.com/lab/research", "--json")
    assert run.code == 2 and run.json["errors"][0]["code"] == "repository_missing"
    run = cli(
        "publish", str(campaign), "--to", "https://github.com/lab/research", "--create", "--json"
    )
    assert run.code == 0, run.stdout
    archive = run.result["archives"][0]
    assert archive["created"] is True
    assert archive["descriptor_url"] == (
        "https://raw.githubusercontent.com/lab/research/main/sci/archive.json"
    )
    assert archive["availability"]["confirmed"] and archive["availability"]["reader_cors"]
    descriptor = json.loads(fake.read("sci/archive.json") or b"{}")
    assert descriptor["bundle_url"].endswith("/releases/download/sci-{id2}/{id}.zip")
    assert fake.read("sci/publications.txt") == "".join(f"{d}\n" for d in digests).encode()
    for digest in digests:
        prefix = f"sci/publications/{digest[:2]}/{digest}"
        for name in (
            "manifest.json",
            "manifest.json.sig",
            "sci.json",
            "README.md",
            "LICENSE",
            "files/fig.png",
        ):
            assert fake.read(f"{prefix}/{name}") is not None, name
        assert fake.read(f"{prefix}/files/data.csv") is None  # not a reader file
    assert fake.read(f"sci/publications/{digests[0][:2]}/{digests[0]}/manifest.json.sig.tsr") == b""
    releases = {r["tag_name"]: r for r in fake.releases}
    for digest in digests:
        release = releases[f"sci-{digest[:2]}"]
        assert release["draft"] is False and release["make_latest"] == "false"
        assert f"{digest}.zip" in [a["name"] for a in release["assets"]]
    assert set(fake.tokens) == {"Bearer gh-test-token"}
    # retry: everything is reused, no new commits or uploads
    commits = len(fake.commits)
    run = cli("publish", str(campaign), "--to", "git@github.com:lab/research.git", "--json")
    assert run.code == 0
    assert all(
        r["bundle"] == "reused" and not r["reader_files_uploaded"] for r in run.result["results"]
    )
    assert len(fake.commits) == commits
    # conflicting remote bytes for one publication: rejected, other preserved
    prefix = f"sci/publications/{digests[0][:2]}/{digests[0]}"
    fake.main = fake.commit_files({f"{prefix}/README.md": b"other"}, fake.main)
    run = cli("publish", str(campaign), "--to", "https://github.com/lab/research", "--json")
    assert run.code == 5 and run.json["status"] == "partial"
    statuses = {r["publication_id"]: r["status"] for r in run.result["results"]}
    assert statuses[f"sha256:{digests[0]}"] == "failed"
    assert statuses[f"sha256:{digests[1]}"] == "hosted"


def test_github_concurrent_update_is_merged_without_force(cli: Cli) -> None:
    campaign, digests = _campaign(cli, count=1)
    fake = FakeGitHub()
    fake.create()
    fake.race_once = True
    cli.transport = fake
    cli.env["GITHUB_TOKEN"] = "t"
    run = cli("publish", str(campaign), "--to", "https://github.com/lab/research", "--json")
    assert run.code == 0, run.stdout
    listed = (fake.read("sci/publications.txt") or b"").decode().split()
    assert set(listed) == {digests[0], "f" * 64}
    assert listed == sorted(listed)


def test_github_private_repo_and_missing_credentials(cli: Cli) -> None:
    campaign, _ = _campaign(cli, count=1)
    fake = FakeGitHub(exists=True, private=True)
    fake.create()
    fake.private = True
    cli.transport = fake
    run = cli("publish", str(campaign), "--to", "https://github.com/lab/research", "--json")
    assert run.code == 2 and run.json["errors"][0]["code"] == "missing_credentials"
    cli.env["GH_TOKEN"] = "t"
    run = cli("publish", str(campaign), "--to", "https://github.com/lab/research", "--json")
    assert run.code == 2 and run.json["errors"][0]["code"] == "repository_not_public"
    # dry run never contacts the provider
    fake.log.clear()
    run = cli("publish", str(campaign), "--to", "https://github.com/lab/research", "--dry-run")
    assert run.code == 0 and fake.log == []


def test_huggingface_publish_and_reuse(cli: Cli) -> None:
    campaign, digests = _campaign(cli)
    hub = FakeHub()
    cli.transport = hub
    cli.env["HF_TOKEN"] = "hf-test"
    run = cli(
        "publish",
        str(campaign),
        "--to",
        "https://huggingface.co/datasets/lab/data",
        "--create",
        "--json",
    )
    assert run.code == 0, run.stdout
    archive = run.result["archives"][0]
    assert archive["descriptor"]["bundle_url"] == (
        "https://huggingface.co/datasets/lab/data/resolve/main/bundles/{id2}/{id}.zip"
    )
    assert archive["availability"]["confirmed"]
    for digest in digests:
        bundle = hub.files[f"bundles/{digest[:2]}/{digest}.zip"]
        assert bundle == (campaign / f"{digest}.zip").read_bytes()
        assert f"bundles/{digest[:2]}/{digest}.zip" in hub.lfs_paths
        assert f"sci/publications/{digest[:2]}/{digest}/files/fig.png" in hub.lfs_paths
    assert hub.files["sci/publications.txt"] == "".join(f"{d}\n" for d in digests).encode()
    head = hub.head
    run = cli("publish", str(campaign), "--to", "git@hf.co:datasets/lab/data", "--json")
    assert run.code == 0
    assert hub.head == head
    assert all(r["bundle"] == "reused" for r in run.result["results"])


def test_huggingface_token_file(cli: Cli, tmp_path: Path) -> None:
    from sci.clients.credentials import huggingface_token

    token = tmp_path / "hf" / "token"
    token.parent.mkdir()
    token.write_text("from-file\n")
    assert huggingface_token({"HF_HOME": str(token.parent)}) == "from-file"
    with pytest.raises(CliError):
        huggingface_token({"HOME": str(tmp_path / "none")})
