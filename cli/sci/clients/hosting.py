"""Provider adapters preserve remote files and never force-push."""

from __future__ import annotations
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from pysci import Bundle, Invalid, Incomplete
from pysci.bundle import canonical_json
from sci.storage.files import atomic


def destination(value: str) -> tuple[str, str]:
    if value.startswith("git@"):
        host, path = value[4:].split(":", 1)
        value = "ssh://git@" + host + "/" + path
    p = urlsplit(value)
    if p.scheme not in ("https", "ssh") or p.query or p.fragment or p.password or p.port:
        raise Invalid("unsupported repository address")
    if p.username not in (None, "git"):
        raise Invalid("repository username")
    path = p.path.strip("/").removesuffix(".git")
    if p.hostname == "github.com":
        provider = "github"
    elif p.hostname in ("huggingface.co", "hf.co") and path.startswith("datasets/"):
        provider = "huggingface"
        path = path.removeprefix("datasets/")
    else:
        raise Invalid("unsupported hosting provider")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise Invalid("repository OWNER/REPO required")
    return provider, path


def run(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise Incomplete("Provider command failed: " + result.stderr[:500])
    return result.stdout


def host(b: Bundle, address: str, create: bool, dry_run: bool) -> str:
    provider, repo = destination(address)
    if provider == "huggingface":
        return host_huggingface(b, repo, create, dry_run)
    descriptor = f"https://raw.githubusercontent.com/{repo}/main/sci/archive.json"
    if dry_run:
        return descriptor
    try:
        metadata = json.loads(run(["gh", "api", f"repos/{repo}"]))
    except Incomplete:
        if not create:
            raise
        run(["gh", "repo", "create", repo, "--public", "--add-readme"])
        metadata = {"private": False}
    if metadata.get("private") is not False:
        raise Invalid("archive repository must already be public; visibility is never changed")
    d = {
        "schema": "sci-archive/0.1",
        "bundle_url": f"https://github.com/{repo}/releases/download/sci-{{id}}/{{id}}.zip",
        "entry_url": f"https://raw.githubusercontent.com/{repo}/main/sci/publications/{{id2}}/{{id}}/{{+path}}",
        "list_url": f"https://raw.githubusercontent.com/{repo}/main/sci/publications.txt",
    }
    with tempfile.TemporaryDirectory(prefix="sci-publish-") as temp:
        root = Path(temp)
        checkout = root / "repo"
        run(["git", "clone", "--single-branch", f"https://github.com/{repo}.git", str(checkout)])
        branch = run(["git", "symbolic-ref", "--short", "HEAD"], checkout).strip()
        if branch != "main":
            raise Invalid("archive default branch must be main")
        include = (
            {"manifest.json", "manifest.json.sig", "sci.json", "README.md"}
            | ({"LICENSE"} if "LICENSE" in b.entries else set())
            | {i["path"] for i in b.metadata.get("display", [])}
        )
        for path in sorted(include):
            atomic(checkout / "sci/publications" / b.id[:2] / b.id / path, b.entries[path])
        atomic(checkout / "sci/archive.json", canonical_json(d))
        packed = root / (b.id + ".zip")
        packed.write_bytes(b.zip())
        tag = "sci-" + b.id
        try:
            release = json.loads(run(["gh", "api", f"repos/{repo}/releases/tags/{tag}"]))
        except Incomplete:
            run(
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    "--repo",
                    repo,
                    "--draft",
                    "--title",
                    tag,
                    "--notes",
                    "SCI publication " + b.id,
                    "--target",
                    "main",
                ]
            )
            release = {"draft": True, "assets": []}
        assets = [a for a in release.get("assets", []) if a["name"] == packed.name]
        if assets:
            downloaded = root / "existing"
            downloaded.mkdir()
            run(
                [
                    "gh",
                    "release",
                    "download",
                    tag,
                    "--repo",
                    repo,
                    "--pattern",
                    packed.name,
                    "--dir",
                    str(downloaded),
                ]
            )
            if (downloaded / packed.name).read_bytes() != packed.read_bytes():
                raise Invalid("conflicting release asset")
        else:
            run(["gh", "release", "upload", tag, str(packed), "--repo", repo])
        listing = checkout / "sci/publications.txt"
        ids = set(listing.read_text().splitlines()) if listing.exists() else set()
        if any(not re.fullmatch("[0-9a-f]{64}", x) for x in ids):
            raise Invalid("malformed remote publication list")
        ids.add(b.id)
        atomic(listing, ("\n".join(sorted(ids)) + "\n").encode(), replace=True)
        run(["git", "add", "sci"], checkout)
        if run(["git", "diff", "--cached", "--name-only"], checkout).strip():
            run(["git", "commit", "-m", "Publish SCI " + b.id], checkout)
            # A concurrent push fails safely; retry reloads remote state and merges the list.
            run(["git", "push", "origin", "HEAD:main"], checkout)
        if release.get("draft"):
            run(["gh", "release", "edit", tag, "--repo", repo, "--draft=false", "--latest=false"])
    return descriptor


def host_huggingface(b: Bundle, repo: str, create: bool, dry_run: bool) -> str:
    from huggingface_hub import HfApi, CommitOperationAdd, hf_hub_download
    from huggingface_hub.errors import RepositoryNotFoundError, EntryNotFoundError, HfHubHTTPError

    descriptor = f"https://huggingface.co/datasets/{repo}/resolve/main/sci/archive.json"
    if dry_run:
        return descriptor
    api = HfApi()
    try:
        info = api.repo_info(repo, repo_type="dataset")
    except RepositoryNotFoundError:
        if not create:
            raise
        api.create_repo(repo, repo_type="dataset", private=False, exist_ok=False)
        info = api.repo_info(repo, repo_type="dataset")
    if info.private:
        raise Invalid("archive repository must already be public")
    d = {
        "schema": "sci-archive/0.1",
        "bundle_url": f"https://huggingface.co/datasets/{repo}/resolve/main/bundles/{{id2}}/{{id}}.zip",
        "entry_url": f"https://huggingface.co/datasets/{repo}/resolve/main/sci/publications/{{id2}}/{{id}}/{{+path}}",
        "list_url": f"https://huggingface.co/datasets/{repo}/resolve/main/sci/publications.txt",
    }
    include = (
        {"manifest.json", "manifest.json.sig", "sci.json", "README.md"}
        | ({"LICENSE"} if "LICENSE" in b.entries else set())
        | {item["path"] for item in b.metadata.get("display", [])}
    )
    desired = {f"sci/publications/{b.id[:2]}/{b.id}/{p}": b.entries[p] for p in include}
    desired[f"bundles/{b.id[:2]}/{b.id}.zip"] = b.zip()
    desired["sci/archive.json"] = canonical_json(d)
    for attempt in range(3):
        info = api.repo_info(repo, repo_type="dataset")

        def existing(path: str) -> bytes | None:
            try:
                return Path(
                    hf_hub_download(repo, path, repo_type="dataset", revision=info.sha)
                ).read_bytes()
            except EntryNotFoundError:
                return None

        old_list = existing("sci/publications.txt")
        ids = set(old_list.decode().splitlines()) if old_list else set()
        if any(not re.fullmatch("[0-9a-f]{64}", pid) for pid in ids):
            raise Invalid("malformed remote publication list")
        operations = []
        for path, raw in desired.items():
            old = existing(path)
            if old is not None and old != raw:
                raise Invalid("conflicting remote file: " + path)
            if old is None:
                operations.append(CommitOperationAdd(path_in_repo=path, path_or_fileobj=raw))
        ids.add(b.id)
        listing = ("\n".join(sorted(ids)) + "\n").encode()
        if listing != old_list:
            operations.append(
                CommitOperationAdd(path_in_repo="sci/publications.txt", path_or_fileobj=listing)
            )
        if not operations:
            return descriptor
        try:
            api.create_commit(
                repo_id=repo,
                repo_type="dataset",
                operations=operations,
                commit_message="Publish SCI " + b.id,
                parent_commit=info.sha,
            )
            return descriptor
        except HfHubHTTPError as exc:
            if exc.response.status_code not in (409, 412) or attempt == 2:
                raise
    raise Incomplete("concurrent archive updates; retry publication")
