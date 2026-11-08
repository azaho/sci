"""GitHub archive publishing through the REST API (Git data, contents, and releases)."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import urllib.parse
from typing import Any

from pysci.jsonstrict import JsonValue

from ..errors import CliError, ErrorRecord, Exit
from .hosting import (
    ARCHIVE_PATH,
    LIST_PATH,
    ItemReport,
    Prepared,
    RemoteFile,
    check_descriptor,
    confirm_public,
    descriptor_bytes,
    merged_list,
    plan_reader_files,
)
from .http import Deadline, HttpClient, Response

API = "https://api.github.com"
JSON_LIMIT = 32 << 20


def descriptor_for(owner: str, repo: str) -> dict[str, str]:
    return {
        "schema": "sci-archive/0.1",
        "bundle_url": f"https://github.com/{owner}/{repo}/releases/download/sci-{{id}}/{{id}}.zip",
        "entry_url": f"https://raw.githubusercontent.com/{owner}/{repo}/main/sci/publications/"
        "{id2}/{id}/{+path}",
        "list_url": f"https://raw.githubusercontent.com/{owner}/{repo}/main/sci/publications.txt",
    }


class GitHubError(CliError):
    pass


def _api_error(response: Response, what: str) -> CliError:
    message = ""
    try:
        record = json.loads(response.body)
        if isinstance(record, dict) and isinstance(record.get("message"), str):
            message = ": " + record["message"][:300]
    except ValueError:
        pass
    temporary = response.status >= 500 or response.status == 429
    if response.status in (401,):
        exit_code: int = Exit.USAGE
        code = "unauthorized"
    elif response.status == 403 and response.header("x-ratelimit-remaining") == "0":
        exit_code, code, temporary = Exit.INCOMPLETE, "rate_limited", True
    elif response.status == 403:
        exit_code, code = Exit.REFUSED, "forbidden"
    elif temporary:
        exit_code, code = Exit.INCOMPLETE, "temporary_failure"
    else:
        exit_code, code = Exit.INVALID, "github_error"
    return GitHubError(
        code,
        f"GitHub {what} returned HTTP {response.status}{message}",
        exit_code,
        retryable=temporary,
    )


class GitHubArchive:
    def __init__(self, http: HttpClient, owner: str, repo: str, token: str) -> None:
        self.http = http
        self.owner = owner
        self.repo = repo
        self.token = token
        self.descriptor = descriptor_for(owner, repo)
        self.descriptor_url = (
            f"https://raw.githubusercontent.com/{owner}/{repo}/main/{ARCHIVE_PATH}"
        )
        self._trees: dict[str, dict[str, tuple[str, str, int | None]]] = {}
        self._releases: dict[str, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ HTTP

    @property
    def _repo_path(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    def _call(
        self,
        method: str,
        path: str,
        deadline: Deadline,
        *,
        document: object = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        max_bytes: int = JSON_LIMIT,
    ) -> Response:
        url = path if path.startswith("https://") else API + path
        all_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if document is not None:
            body = json.dumps(document).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        all_headers.update(headers or {})
        return self.http.request(
            method,
            url,
            deadline=deadline,
            headers=all_headers,
            body=body,
            auth=f"Bearer {self.token}",
            max_bytes=max_bytes,
            retry=method in ("GET", "HEAD", "PUT", "PATCH", "DELETE"),
        )

    def _json(self, response: Response, what: str, ok: tuple[int, ...] = (200,)) -> Any:
        if response.status not in ok:
            raise _api_error(response, what)
        value: JsonValue = response.json(f"GitHub {what}")
        return value

    # ------------------------------------------------------------------ repository

    def ensure_repository(self, create: bool, deadline: Deadline) -> dict[str, Any]:
        response = self._call("GET", self._repo_path, deadline)
        if response.status == 404:
            if not create:
                raise CliError(
                    "repository_missing",
                    f"GitHub repository {self.owner}/{self.repo} does not exist; pass --create",
                    Exit.USAGE,
                )
            user = self._json(self._call("GET", "/user", deadline), "user lookup")
            login = str(user.get("login", "")) if isinstance(user, dict) else ""
            body = {
                "name": self.repo,
                "private": False,
                "auto_init": True,
                "description": "SCI publication archive",
            }
            path = (
                "/user/repos"
                if login.lower() == self.owner.lower()
                else f"/orgs/{self.owner}/repos"
            )
            repo = self._json(
                self._call("POST", path, deadline, document=body), "repository creation", (201,)
            )
            repo["_created"] = True
        else:
            repo = self._json(response, "repository lookup")
        if not isinstance(repo, dict):
            raise CliError(
                "invalid_response", "GitHub repository response is not an object", Exit.INVALID
            )
        if repo.get("private") is not False or repo.get("visibility", "public") != "public":
            raise CliError(
                "repository_not_public",
                f"GitHub repository {self.owner}/{self.repo} is not public; its visibility "
                "is left unchanged",
                Exit.USAGE,
            )
        return repo

    def _ref(self, branch: str, deadline: Deadline) -> str | None:
        response = self._call("GET", f"{self._repo_path}/git/ref/heads/{branch}", deadline)
        if response.status in (404, 409):
            return None
        record = self._json(response, "branch lookup")
        sha = record.get("object", {}).get("sha") if isinstance(record, dict) else None
        if not isinstance(sha, str):
            raise CliError(
                "invalid_response", "GitHub ref response has no object.sha", Exit.INVALID
            )
        return sha

    def ensure_main(self, repo: dict[str, Any], deadline: Deadline) -> str | None:
        """The ``main`` head commit; creates ``main`` from the default branch if needed.

        Returns None for an empty repository (no commits yet).
        """
        head = self._ref("main", deadline)
        if head is not None:
            return head
        default = repo.get("default_branch")
        if isinstance(default, str) and default != "main":
            base = self._ref(default, deadline)
            if base is not None:
                self._json(
                    self._call(
                        "POST",
                        f"{self._repo_path}/git/refs",
                        deadline,
                        document={"ref": "refs/heads/main", "sha": base},
                    ),
                    "branch creation",
                    (201,),
                )
                return base
        return None

    def initialize(self, deadline: Deadline) -> str:
        """Create the first commit (``sci/archive.json``) in an empty repository."""
        content = base64.b64encode(descriptor_bytes(self.descriptor)).decode("ascii")
        record = self._json(
            self._call(
                "PUT",
                f"{self._repo_path}/contents/{ARCHIVE_PATH}",
                deadline,
                document={
                    "message": "Add SCI archive descriptor",
                    "content": content,
                    "branch": "main",
                },
            ),
            "repository initialization",
            (200, 201),
        )
        sha = record.get("commit", {}).get("sha") if isinstance(record, dict) else None
        if not isinstance(sha, str):
            raise CliError(
                "invalid_response", "GitHub contents response has no commit.sha", Exit.INVALID
            )
        return sha

    # ------------------------------------------------------------------ trees

    def _tree(self, sha: str, deadline: Deadline) -> dict[str, tuple[str, str, int | None]]:
        if sha not in self._trees:
            record = self._json(
                self._call("GET", f"{self._repo_path}/git/trees/{sha}", deadline), "tree lookup"
            )
            entries: dict[str, tuple[str, str, int | None]] = {}
            for entry in record.get("tree", []) if isinstance(record, dict) else []:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    size = entry.get("size")
                    entries[entry["path"]] = (
                        str(entry.get("type")),
                        str(entry.get("sha")),
                        size if isinstance(size, int) else None,
                    )
            self._trees[sha] = entries
        return self._trees[sha]

    def commit_tree(self, commit: str, deadline: Deadline) -> str:
        record = self._json(
            self._call("GET", f"{self._repo_path}/git/commits/{commit}", deadline), "commit lookup"
        )
        sha = record.get("tree", {}).get("sha") if isinstance(record, dict) else None
        if not isinstance(sha, str):
            raise CliError("invalid_response", "GitHub commit has no tree.sha", Exit.INVALID)
        return sha

    def lookup(self, root: str, path: str, deadline: Deadline) -> RemoteFile | None:
        tree = root
        parts = path.split("/")
        for index, name in enumerate(parts):
            entry = self._tree(tree, deadline).get(name)
            if entry is None:
                return None
            kind, sha, size = entry
            if index == len(parts) - 1:
                return RemoteFile(size=size, git_sha1=sha) if kind == "blob" else None
            if kind != "tree":
                return None
            tree = sha
        return None

    def read(self, root: str, path: str, deadline: Deadline) -> bytes | None:
        remote = self.lookup(root, path, deadline)
        if remote is None or remote.git_sha1 is None:
            return None
        record = self._json(
            self._call("GET", f"{self._repo_path}/git/blobs/{remote.git_sha1}", deadline),
            "blob download",
        )
        try:
            return base64.b64decode(str(record["content"]))
        except (KeyError, TypeError, ValueError):
            raise CliError(
                "invalid_response", "GitHub blob response is malformed", Exit.INVALID
            ) from None

    # ------------------------------------------------------------------ releases

    def _all_releases(self, deadline: Deadline) -> dict[str, dict[str, Any]]:
        if self._releases is None:
            releases: dict[str, dict[str, Any]] = {}
            for page in range(1, 51):
                records = self._json(
                    self._call(
                        "GET", f"{self._repo_path}/releases?per_page=100&page={page}", deadline
                    ),
                    "release listing",
                )
                if not isinstance(records, list) or not records:
                    break
                for record in records:
                    if isinstance(record, dict) and isinstance(record.get("tag_name"), str):
                        releases.setdefault(record["tag_name"], record)
                if len(records) < 100:
                    break
            self._releases = releases
        return self._releases

    def upload_release(self, pub: Prepared, deadline: Deadline) -> str:
        """Ensure release ``sci-<id>`` holds ``<id>.zip``; return uploaded/reused.

        The release stays a draft until its asset is complete, then is
        published without being marked latest.
        """
        tag = f"sci-{pub.digest}"
        name = f"{pub.digest}.zip"
        releases = self._all_releases(deadline)
        release = releases.get(tag)
        if release is None:
            release = self._json(
                self._call(
                    "POST",
                    f"{self._repo_path}/releases",
                    deadline,
                    document={
                        "tag_name": tag,
                        "target_commitish": "main",
                        "name": tag,
                        "body": f"SCI publication sha256:{pub.digest}",
                        "draft": True,
                        "make_latest": "false",
                    },
                ),
                "release creation",
                (201,),
            )
            releases[tag] = release
        status = "uploaded"
        asset = next(
            (a for a in release.get("assets", []) if isinstance(a, dict) and a.get("name") == name),
            None,
        )
        if asset is not None and asset.get("state") != "uploaded":
            response = self._call("DELETE", str(asset["url"]), deadline)
            if response.status not in (204, 404):
                raise _api_error(response, "incomplete asset removal")
            asset = None
        if asset is not None:
            if not self._asset_matches(asset, pub, deadline):
                raise CliError(
                    "destination_conflict",
                    f"release {tag} already has a different {name}",
                    Exit.INVALID,
                )
            status = "reused"
        else:
            upload = str(release.get("upload_url", "")).split("{", 1)[0]
            if not upload.startswith("https://"):
                raise CliError("invalid_response", "release has no upload_url", Exit.INVALID)
            uploaded = self._json(
                self._call(
                    "POST",
                    f"{upload}?{urllib.parse.urlencode({'name': name})}",
                    deadline,
                    body=pub.zip_path,
                    headers={"Content-Type": "application/zip"},
                ),
                "asset upload",
                (201,),
            )
            if not isinstance(uploaded, dict) or uploaded.get("size") != pub.zip_size:
                raise CliError(
                    "upload_incomplete",
                    f"{name} upload size mismatch",
                    Exit.INCOMPLETE,
                    retryable=True,
                )
            digest = uploaded.get("digest")
            if isinstance(digest, str) and digest != f"sha256:{pub.zip_sha256}":
                raise CliError(
                    "upload_corrupted",
                    f"{name} digest mismatch after upload",
                    Exit.INCOMPLETE,
                    retryable=True,
                )
        if release.get("draft"):
            published = self._json(
                self._call(
                    "PATCH",
                    f"{self._repo_path}/releases/{release['id']}",
                    deadline,
                    document={"draft": False, "make_latest": "false"},
                ),
                "release publication",
            )
            releases[tag] = published
        return status

    def _asset_matches(self, asset: dict[str, Any], pub: Prepared, deadline: Deadline) -> bool:
        digest = asset.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:"):
            return digest == f"sha256:{pub.zip_sha256}"
        if asset.get("size") != pub.zip_size:
            return False
        response = self._call(
            "GET",
            str(asset["url"]),
            deadline,
            headers={"Accept": "application/octet-stream"},
            max_bytes=pub.zip_size + 1,
        )
        if response.status != 200:
            raise _api_error(response, "asset download")
        return hashlib.sha256(response.body).hexdigest() == pub.zip_sha256

    # ------------------------------------------------------------------ commits

    def commit(self, head: str, files: dict[str, bytes], message: str, deadline: Deadline) -> str:
        """Commit ``files`` on top of ``head`` and fast-forward ``main``.

        Raises:
            _NotFastForward: ``main`` moved; the caller rebuilds and retries.
        """
        root = self.commit_tree(head, deadline)
        entries = []
        for path, data in sorted(files.items()):
            blob = self._json(
                self._call(
                    "POST",
                    f"{self._repo_path}/git/blobs",
                    deadline,
                    document={
                        "content": base64.b64encode(data).decode("ascii"),
                        "encoding": "base64",
                    },
                ),
                "blob creation",
                (201,),
            )
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._json(
            self._call(
                "POST",
                f"{self._repo_path}/git/trees",
                deadline,
                document={"base_tree": root, "tree": entries},
            ),
            "tree creation",
            (201,),
        )
        commit = self._json(
            self._call(
                "POST",
                f"{self._repo_path}/git/commits",
                deadline,
                document={"message": message, "tree": tree["sha"], "parents": [head]},
            ),
            "commit creation",
            (201,),
        )
        response = self._call(
            "PATCH",
            f"{self._repo_path}/git/refs/heads/main",
            deadline,
            document={"sha": commit["sha"], "force": False},
        )
        if response.status in (409, 422):
            raise _NotFastForward()
        self._json(response, "branch update")
        return str(commit["sha"])

    # ------------------------------------------------------------------ workflow

    def publish(
        self, pubs: list[Prepared], *, create: bool, timeout: float
    ) -> tuple[dict[str, Any], list[ItemReport]]:
        deadline = Deadline(timeout)
        repo = self.ensure_repository(create, deadline)
        head = self.ensure_main(repo, deadline)
        if head is None:
            head = self.initialize(deadline)
        root = self.commit_tree(head, deadline)
        write_archive = check_descriptor(self.read(root, ARCHIVE_PATH, deadline), self.descriptor)
        merged_list(self.read(root, LIST_PATH, deadline), [])  # malformed lists stop here
        reports: list[ItemReport] = []
        writes: dict[str, bytes] = {}
        completed: list[str] = []
        for pub in pubs:
            report = ItemReport(pub.item.publication_id)
            reports.append(report)
            item_deadline = Deadline(timeout)
            try:
                remote = {
                    path: found
                    for path in pub.remote_files()
                    if (found := self.lookup(root, path, item_deadline)) is not None
                }
                planned = plan_reader_files(
                    pub,
                    remote,
                    functools.partial(self.read, root, deadline=item_deadline),
                    report,
                )
                if report.status == "failed":
                    continue
                report.bundle = self.upload_release(pub, item_deadline)
            except CliError as exc:
                report.fail(exc.records[0], exc.exit_code)
                continue
            writes.update(planned)
            report.uploaded = sorted(planned)
            completed.append(pub.digest)
            report.status = "pending_commit"
        commit_sha = None
        for _attempt in range(4):
            commit_deadline = Deadline(timeout)
            root = self.commit_tree(head, commit_deadline)
            files = dict(writes)
            if write_archive and self.lookup(root, ARCHIVE_PATH, commit_deadline) is None:
                files[ARCHIVE_PATH] = descriptor_bytes(self.descriptor)
            files[LIST_PATH] = merged_list(self.read(root, LIST_PATH, commit_deadline), completed)
            files = {
                p: d
                for p, d in files.items()
                if (r := self.lookup(root, p, commit_deadline)) is None or r.git_sha1 != _git_sha(d)
            }
            if not files:
                break
            try:
                commit_sha = self.commit(
                    head, files, f"Publish {len(completed)} SCI publication(s)", commit_deadline
                )
                break
            except _NotFastForward:
                self._trees.clear()
                new_head = self._ref("main", commit_deadline)
                if new_head is None:
                    raise CliError(
                        "branch_missing", "main disappeared during publishing", Exit.INCOMPLETE
                    ) from None
                head = new_head
        else:
            raise CliError(
                "concurrent_update",
                "main kept moving during publishing; retry",
                Exit.INCOMPLETE,
                retryable=True,
            )
        for report in reports:
            if report.status == "pending_commit":
                report.status = "hosted"
        hosted = [p for p, r in zip(pubs, reports, strict=True) if r.status == "hosted"]
        checks: list[tuple[str, str, bytes | None]] = [
            ("descriptor", self.descriptor_url, descriptor_bytes(self.descriptor))
        ]
        for pub in hosted:
            entry = (
                self.descriptor["entry_url"]
                .replace("{id2}", pub.digest[:2])
                .replace("{id}", pub.digest)
            )
            checks.append(
                ("manifest", entry.replace("{+path}", "manifest.json"), pub.reader["manifest.json"])
            )
            bundle = self.descriptor["bundle_url"].replace("{id}", pub.digest)
            checks.append(("bundle", bundle, None))
        availability = confirm_public(self.http, checks, Deadline(timeout))
        return (
            {
                "repository": f"https://github.com/{self.owner}/{self.repo}",
                "created": bool(repo.get("_created")),
                "descriptor_url": self.descriptor_url,
                "descriptor": self.descriptor,
                "commit": commit_sha,
                "availability": availability,
            },
            reports,
        )


def _git_sha(data: bytes) -> str:
    from .hosting import git_blob_sha1

    return git_blob_sha1(data)


class _NotFastForward(Exception):
    pass


__all__ = ["ErrorRecord", "GitHubArchive", "descriptor_for"]
