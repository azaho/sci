"""Hugging Face dataset archive publishing through the Hub HTTP API and Git LFS."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import CliError, Exit
from .hosting import (
    ARCHIVE_PATH,
    LIST_PATH,
    ItemReport,
    Prepared,
    RemoteFile,
    check_descriptor,
    confirm_public,
    descriptor_bytes,
    git_blob_sha1,
    merged_list,
    plan_reader_files,
)
from .http import Deadline, HttpClient, Response

HUB = "https://huggingface.co"
JSON_LIMIT = 32 << 20
LFS_TYPE = "application/vnd.git-lfs+json"


def descriptor_for(owner: str, repo: str) -> dict[str, str]:
    base = f"{HUB}/datasets/{owner}/{repo}/resolve/main"
    return {
        "schema": "sci-archive/0.1",
        "bundle_url": f"{base}/bundles/{{id2}}/{{id}}.zip",
        "entry_url": f"{base}/sci/publications/{{id2}}/{{id}}/{{+path}}",
        "list_url": f"{base}/sci/publications.txt",
    }


def bundle_path(digest: str) -> str:
    return f"bundles/{digest[:2]}/{digest}.zip"


@dataclass(frozen=True, slots=True)
class LfsFile:
    path: str
    sha256: str
    size: int
    source: Path | bytes


def _hub_error(response: Response, what: str) -> CliError:
    message = ""
    try:
        record = json.loads(response.body)
        if isinstance(record, dict) and isinstance(record.get("error"), str):
            message = ": " + record["error"][:300]
    except ValueError:
        pass
    temporary = response.status >= 500 or response.status == 429
    if response.status == 401:
        return CliError("unauthorized", f"Hugging Face {what}: HTTP 401{message}", Exit.USAGE)
    if response.status == 403:
        return CliError("forbidden", f"Hugging Face {what}: HTTP 403{message}", Exit.REFUSED)
    return CliError(
        "temporary_failure" if temporary else "huggingface_error",
        f"Hugging Face {what} returned HTTP {response.status}{message}",
        Exit.INCOMPLETE if temporary else Exit.INVALID,
        retryable=temporary,
    )


class _NotFastForward(Exception):
    pass


class HuggingFaceArchive:
    def __init__(self, http: HttpClient, owner: str, repo: str, token: str) -> None:
        self.http = http
        self.owner = owner
        self.repo = repo
        self.repo_id = f"{owner}/{repo}"
        self.token = token
        self.descriptor = descriptor_for(owner, repo)
        self.descriptor_url = f"{HUB}/datasets/{self.repo_id}/resolve/main/{ARCHIVE_PATH}"
        self.api = f"{HUB}/api/datasets/{self.repo_id}"

    def _call(
        self,
        method: str,
        url: str,
        deadline: Deadline,
        *,
        document: object = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        max_bytes: int = JSON_LIMIT,
        auth: bool = True,
    ) -> Response:
        all_headers: dict[str, str] = {"Accept": "application/json"}
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
            auth=f"Bearer {self.token}" if auth else None,
            max_bytes=max_bytes,
            retry=method in ("GET", "PUT"),
        )

    def _json(self, response: Response, what: str, ok: tuple[int, ...] = (200,)) -> Any:
        if response.status not in ok:
            raise _hub_error(response, what)
        return response.json(f"Hugging Face {what}")

    # ------------------------------------------------------------------ repository

    def ensure_repository(self, create: bool, deadline: Deadline) -> dict[str, Any]:
        response = self._call("GET", self.api, deadline)
        created = False
        if response.status in (401, 404):
            if not create:
                raise CliError(
                    "repository_missing",
                    f"Hugging Face dataset {self.repo_id} does not exist (or is not accessible); "
                    "pass --create",
                    Exit.USAGE,
                )
            who = self._json(self._call("GET", f"{HUB}/api/whoami-v2", deadline), "whoami")
            name = who.get("name") if isinstance(who, dict) else None
            body: dict[str, Any] = {"type": "dataset", "name": self.repo, "private": False}
            if not isinstance(name, str) or name.lower() != self.owner.lower():
                body["organization"] = self.owner
            self._json(
                self._call("POST", f"{HUB}/api/repos/create", deadline, document=body),
                "repository creation",
                (200, 201),
            )
            created = True
            response = self._call("GET", self.api, deadline)
        info = self._json(response, "repository lookup")
        if not isinstance(info, dict):
            raise CliError("invalid_response", "repository info is not an object", Exit.INVALID)
        if info.get("private") is not False:
            raise CliError(
                "repository_not_public",
                f"Hugging Face dataset {self.repo_id} is not public; its visibility is left "
                "unchanged",
                Exit.USAGE,
            )
        if info.get("gated") not in (None, False):
            raise CliError(
                "repository_not_public",
                f"Hugging Face dataset {self.repo_id} is gated; readers need open access",
                Exit.USAGE,
            )
        info["_created"] = created
        return info

    def head(self, deadline: Deadline) -> str | None:
        response = self._call("GET", f"{self.api}/revision/main", deadline)
        if response.status == 404:
            return None
        record = self._json(response, "revision lookup")
        sha = record.get("sha") if isinstance(record, dict) else None
        return sha if isinstance(sha, str) else None

    def paths_info(self, paths: list[str], deadline: Deadline) -> dict[str, RemoteFile]:
        found: dict[str, RemoteFile] = {}
        for start in range(0, len(paths), 50):
            chunk = paths[start : start + 50]
            form = urllib.parse.urlencode([*(("paths", p) for p in chunk), ("expand", "true")])
            response = self._call(
                "POST",
                f"{self.api}/paths-info/main",
                deadline,
                body=form.encode("ascii"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status == 404:
                return found
            records = self._json(response, "paths-info")
            for record in records if isinstance(records, list) else []:
                if not isinstance(record, dict) or record.get("type") != "file":
                    continue
                lfs = record.get("lfs")
                sha256 = lfs.get("oid") if isinstance(lfs, dict) else None
                size = record.get("size")
                found[str(record.get("path"))] = RemoteFile(
                    size=size if isinstance(size, int) else None,
                    git_sha1=None if sha256 else str(record.get("oid")),
                    sha256=str(sha256) if sha256 else None,
                )
        return found

    def read(self, path: str, deadline: Deadline) -> bytes | None:
        response = self._call(
            "GET",
            f"{HUB}/datasets/{self.repo_id}/resolve/main/{urllib.parse.quote(path)}",
            deadline,
            max_bytes=64 << 20,
        )
        if response.status == 404:
            return None
        if response.status != 200:
            raise _hub_error(response, f"download of {path}")
        return response.body

    def upload_modes(self, files: dict[str, bytes], deadline: Deadline) -> dict[str, str]:
        if not files:
            return {}
        document = {
            "files": [
                {"path": p, "size": len(d), "sample": base64.b64encode(d[:512]).decode("ascii")}
                for p, d in files.items()
            ]
        }
        record = self._json(
            self._call("POST", f"{self.api}/preupload/main", deadline, document=document),
            "preupload",
        )
        modes: dict[str, str] = {}
        for entry in record.get("files", []) if isinstance(record, dict) else []:
            if isinstance(entry, dict):
                modes[str(entry.get("path"))] = str(entry.get("uploadMode", "regular"))
        return modes

    def lfs_upload(self, item: LfsFile, deadline: Deadline) -> str:
        """Upload one LFS object through the batch API; return uploaded/present."""
        batch = self._json(
            self._call(
                "POST",
                f"{HUB}/datasets/{self.repo_id}.git/info/lfs/objects/batch",
                deadline,
                document={
                    "operation": "upload",
                    "transfers": ["basic"],
                    "objects": [{"oid": item.sha256, "size": item.size}],
                    "hash_algo": "sha256",
                    "ref": {"name": "main"},
                },
                headers={"Accept": LFS_TYPE, "Content-Type": LFS_TYPE},
            ),
            "LFS batch",
        )
        objects = batch.get("objects") if isinstance(batch, dict) else None
        if not isinstance(objects, list) or not objects or not isinstance(objects[0], dict):
            raise CliError("invalid_response", "LFS batch response has no objects", Exit.INVALID)
        obj = objects[0]
        if "error" in obj:
            raise CliError(
                "lfs_error",
                f"LFS refused {item.path}: {obj['error']}",
                Exit.INCOMPLETE,
                retryable=True,
            )
        actions = obj.get("actions") or {}
        upload = actions.get("upload") if isinstance(actions, dict) else None
        if upload is None:
            return "present"
        header = dict(upload.get("header") or {})
        if "chunk_size" in header:
            raise CliError(
                "unsupported_capability",
                "the Hub requested a multipart LFS upload, which this CLI does not support",
                Exit.UNSUPPORTED,
            )
        response = self.http.request(
            "PUT",
            str(upload["href"]),
            deadline=deadline,
            headers={str(k): str(v) for k, v in header.items()},
            body=item.source,
        )
        if response.status not in (200, 201):
            raise _hub_error(response, f"LFS upload of {item.path}")
        verify = actions.get("verify")
        if isinstance(verify, dict):
            response = self.http.request(
                "POST",
                str(verify["href"]),
                deadline=deadline,
                headers={
                    "Accept": LFS_TYPE,
                    "Content-Type": LFS_TYPE,
                    **{str(k): str(v) for k, v in (verify.get("header") or {}).items()},
                },
                body=json.dumps({"oid": item.sha256, "size": item.size}).encode("ascii"),
                auth=f"Bearer {self.token}",
            )
            if response.status != 200:
                raise _hub_error(response, f"LFS verification of {item.path}")
        return "uploaded"

    def commit(
        self,
        parent: str | None,
        regular: dict[str, bytes],
        lfs: list[LfsFile],
        message: str,
        deadline: Deadline,
    ) -> str:
        header: dict[str, Any] = {"summary": message, "description": ""}
        if parent is not None:
            header["parentCommit"] = parent
        lines = [json.dumps({"key": "header", "value": header})]
        for path, data in sorted(regular.items()):
            lines.append(
                json.dumps(
                    {
                        "key": "file",
                        "value": {
                            "path": path,
                            "content": base64.b64encode(data).decode("ascii"),
                            "encoding": "base64",
                        },
                    }
                )
            )
        for item in lfs:
            lines.append(
                json.dumps(
                    {
                        "key": "lfsFile",
                        "value": {
                            "path": item.path,
                            "algo": "sha256",
                            "oid": item.sha256,
                            "size": item.size,
                        },
                    }
                )
            )
        response = self._call(
            "POST",
            f"{self.api}/commit/main",
            deadline,
            body=("\n".join(lines) + "\n").encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
        )
        if response.status in (409, 412):
            raise _NotFastForward()
        record = self._json(response, "commit", (200, 201))
        oid = record.get("commitOid") if isinstance(record, dict) else None
        return str(oid) if oid else ""

    # ------------------------------------------------------------------ workflow

    def publish(
        self, pubs: list[Prepared], *, create: bool, timeout: float
    ) -> tuple[dict[str, Any], list[ItemReport]]:
        deadline = Deadline(timeout)
        repo = self.ensure_repository(create, deadline)
        head = self.head(deadline)
        managed = self.paths_info([ARCHIVE_PATH, LIST_PATH], deadline)
        archive_existing = self.read(ARCHIVE_PATH, deadline) if ARCHIVE_PATH in managed else None
        write_archive = check_descriptor(archive_existing, self.descriptor)
        list_existing = self.read(LIST_PATH, deadline) if LIST_PATH in managed else None
        merged_list(list_existing, [])
        reports: list[ItemReport] = []
        regular: dict[str, bytes] = {}
        lfs: list[LfsFile] = []
        completed: list[str] = []
        for pub in pubs:
            report = ItemReport(pub.item.publication_id)
            reports.append(report)
            item_deadline = Deadline(timeout)
            try:
                paths = [*pub.remote_files(), bundle_path(pub.digest)]
                remote = self.paths_info(paths, item_deadline)
                planned = plan_reader_files(
                    pub, remote, functools.partial(self.read, deadline=item_deadline), report
                )
                if report.status == "failed":
                    continue
                bundle_remote = remote.get(bundle_path(pub.digest))
                item_lfs: list[LfsFile] = []
                if bundle_remote is not None:
                    if bundle_remote.sha256 != pub.zip_sha256:
                        raise CliError(
                            "destination_conflict",
                            f"{bundle_path(pub.digest)} exists with different bytes",
                            Exit.INVALID,
                        )
                    report.bundle = "reused"
                else:
                    item_lfs.append(
                        LfsFile(bundle_path(pub.digest), pub.zip_sha256, pub.zip_size, pub.zip_path)
                    )
                modes = self.upload_modes(planned, item_deadline)
                item_regular: dict[str, bytes] = {}
                for path, data in planned.items():
                    if modes.get(path) == "lfs":
                        item_lfs.append(
                            LfsFile(path, hashlib.sha256(data).hexdigest(), len(data), data)
                        )
                    else:
                        item_regular[path] = data
                for item in item_lfs:
                    state = self.lfs_upload(item, item_deadline)
                    if item.path == bundle_path(pub.digest):
                        report.bundle = "uploaded" if state == "uploaded" else "reused"
            except CliError as exc:
                report.fail(exc.records[0], exc.exit_code)
                continue
            regular.update(item_regular)
            lfs.extend(item_lfs)
            report.uploaded = sorted(planned)
            completed.append(pub.digest)
            report.status = "pending_commit"
        commit_oid = None
        for _attempt in range(4):
            commit_deadline = Deadline(timeout)
            files = dict(regular)
            current = self.paths_info([ARCHIVE_PATH, LIST_PATH], commit_deadline)
            if write_archive and ARCHIVE_PATH not in current:
                files[ARCHIVE_PATH] = descriptor_bytes(self.descriptor)
            existing_list = self.read(LIST_PATH, commit_deadline) if LIST_PATH in current else None
            new_list = merged_list(existing_list, completed)
            if new_list != existing_list:
                files[LIST_PATH] = new_list
            if not files and not lfs:
                break
            try:
                commit_oid = self.commit(
                    head,
                    files,
                    lfs,
                    f"Publish {len(completed)} SCI publication(s)",
                    commit_deadline,
                )
                break
            except _NotFastForward:
                head = self.head(commit_deadline)
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
        checks: list[tuple[str, str, bytes | None]] = [
            ("descriptor", self.descriptor_url, descriptor_bytes(self.descriptor))
        ]
        for pub, report in zip(pubs, reports, strict=True):
            if report.status != "hosted":
                continue
            entry = (
                self.descriptor["entry_url"]
                .replace("{id2}", pub.digest[:2])
                .replace("{id}", pub.digest)
            )
            checks.append(
                ("manifest", entry.replace("{+path}", "manifest.json"), pub.reader["manifest.json"])
            )
            checks.append(
                (
                    "bundle",
                    self.descriptor["bundle_url"]
                    .replace("{id2}", pub.digest[:2])
                    .replace("{id}", pub.digest),
                    None,
                )
            )
        availability = confirm_public(self.http, checks, Deadline(timeout))
        return (
            {
                "repository": f"{HUB}/datasets/{self.repo_id}",
                "created": bool(repo.get("_created")),
                "descriptor_url": self.descriptor_url,
                "descriptor": self.descriptor,
                "commit": commit_oid,
                "availability": availability,
            },
            reports,
        )


__all__ = ["HuggingFaceArchive", "descriptor_for", "git_blob_sha1"]
