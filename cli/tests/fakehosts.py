"""In-memory fakes of the GitHub REST API and the Hugging Face Hub API (no real uploads)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from sci.clients.http import HttpRequest, RawResponse


def _reply(
    status: int,
    value: object = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> RawResponse:
    body = raw if raw is not None else (b"" if value is None else json.dumps(value).encode())
    all_headers = {"content-type": "application/json", **(headers or {})}
    return RawResponse(status, all_headers, io.BytesIO(body))


def _sha(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


@dataclass
class FakeGitHub:
    owner: str = "lab"
    repo: str = "research"
    exists: bool = False
    private: bool = False
    blobs: dict[str, bytes] = field(default_factory=dict)
    trees: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    commits: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    main: str | None = None
    releases: list[dict[str, Any]] = field(default_factory=list)
    assets: dict[int, bytes] = field(default_factory=dict)
    log: list[tuple[str, str]] = field(default_factory=list)
    race_once: bool = False
    tokens: list[str | None] = field(default_factory=list)

    # ---------------------------------------------------------------- git model

    def _tree_sha(self, entries: dict[str, tuple[str, str]]) -> str:
        sha = _sha(json.dumps(sorted(entries.items())).encode())
        self.trees[sha] = dict(entries)
        return sha

    def _apply(self, base: str | None, files: dict[str, str]) -> str:
        entries = dict(self.trees[base]) if base else {}
        nested: dict[str, dict[str, str]] = {}
        for path, sha in files.items():
            head, _, rest = path.partition("/")
            if rest:
                nested.setdefault(head, {})[rest] = sha
            else:
                entries[head] = ("blob", sha)
        for name, sub in nested.items():
            current = entries.get(name)
            entries[name] = ("tree", self._apply(current[1] if current else None, sub))
        return self._tree_sha(entries)

    def commit_files(self, files: dict[str, bytes], parent: str | None) -> str:
        shas = {}
        for path, data in files.items():
            sha = _blob_sha(data)
            self.blobs[sha] = data
            shas[path] = sha
        base = self.commits[parent][0] if parent else None
        tree = self._apply(base, shas)
        sha = _sha(f"{tree}{parent}{len(self.commits)}".encode())
        self.commits[sha] = (tree, [parent] if parent else [])
        return sha

    def read(self, path: str) -> bytes | None:
        if self.main is None:
            return None
        tree = self.commits[self.main][0]
        parts = path.split("/")
        for index, name in enumerate(parts):
            entry = self.trees[tree].get(name)
            if entry is None:
                return None
            if index == len(parts) - 1:
                return self.blobs.get(entry[1]) if entry[0] == "blob" else None
            tree = entry[1]
        return None

    def create(self) -> None:
        self.exists = True
        self.main = self.commit_files({"README.md": b"# research\n"}, None)

    # ---------------------------------------------------------------- transport

    def send(self, request: HttpRequest, timeout: float) -> RawResponse:
        url = urllib.parse.urlsplit(request.url)
        self.log.append((request.method, request.url))
        body = request.body_bytes()
        if url.netloc == "raw.githubusercontent.com":
            path = url.path.removeprefix(f"/{self.owner}/{self.repo}/main/")
            data = self.read(path)
            if data is None:
                return _reply(404, raw=b"404")
            return _reply(200, raw=data, headers={"access-control-allow-origin": "*"})
        if url.netloc == "github.com":
            prefix = f"/{self.owner}/{self.repo}/releases/download/"
            tag, name = url.path.removeprefix(prefix).split("/")
            for release in self.releases:
                if release["tag_name"] == tag and not release["draft"]:
                    for asset in release["assets"]:
                        if asset["name"] == name:
                            return _reply(206, raw=self.assets[asset["id"]][:1])
            return _reply(404, raw=b"")
        self.tokens.append(request.unredirected.get("Authorization"))
        repo_path = f"/repos/{self.owner}/{self.repo}"
        path = url.path
        if url.netloc == "uploads.github.com":
            release_id = int(path.split("/")[-2])
            name = urllib.parse.parse_qs(url.query)["name"][0]
            release = next(r for r in self.releases if r["id"] == release_id)
            asset_id = 1000 + len(self.assets)
            self.assets[asset_id] = body
            asset = {
                "id": asset_id,
                "name": name,
                "state": "uploaded",
                "size": len(body),
                "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                "url": f"https://api.github.com{repo_path}/releases/assets/{asset_id}",
            }
            release["assets"].append(asset)
            return _reply(201, asset)
        if path == "/user":
            return _reply(200, {"login": self.owner})
        if path == "/user/repos" and request.method == "POST":
            self.create()
            return _reply(201, self._repo())
        if not path.startswith(repo_path):
            return _reply(404, {"message": "Not Found"})
        if not self.exists:
            return _reply(404, {"message": "Not Found"})
        rest = path.removeprefix(repo_path)
        if rest == "":
            return _reply(200, self._repo())
        if rest == "/git/ref/heads/main":
            if self.main is None:
                return _reply(409, {"message": "Git Repository is empty."})
            return _reply(200, {"object": {"sha": self.main}})
        if rest.startswith("/git/commits/") and request.method == "GET":
            tree, _ = self.commits[rest.rsplit("/", 1)[1]]
            return _reply(200, {"tree": {"sha": tree}})
        if rest.startswith("/git/trees/") and request.method == "GET":
            entries = self.trees[rest.rsplit("/", 1)[1]]
            return _reply(
                200,
                {
                    "tree": [
                        {
                            "path": name,
                            "type": kind,
                            "sha": sha,
                            **({"size": len(self.blobs[sha])} if kind == "blob" else {}),
                        }
                        for name, (kind, sha) in entries.items()
                    ],
                    "truncated": False,
                },
            )
        if rest.startswith("/git/blobs/") and request.method == "GET":
            data = self.blobs[rest.rsplit("/", 1)[1]]
            return _reply(200, {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        document = json.loads(body) if body and request.method != "GET" else {}
        if rest == "/git/blobs":
            data = base64.b64decode(document["content"])
            self.blobs[_blob_sha(data)] = data
            return _reply(201, {"sha": _blob_sha(data)})
        if rest == "/git/trees":
            files = {e["path"]: e["sha"] for e in document["tree"]}
            return _reply(201, {"sha": self._apply(document["base_tree"], files)})
        if rest == "/git/commits":
            sha = _sha(f"{document['tree']}{document['parents']}{len(self.commits)}".encode())
            self.commits[sha] = (document["tree"], document["parents"])
            return _reply(201, {"sha": sha})
        if rest == "/git/refs/heads/main" and request.method == "PATCH":
            if self.race_once:
                self.race_once = False
                other = "f" * 64
                current = self.read("sci/publications.txt") or b""
                self.main = self.commit_files(
                    {"sci/publications.txt": current + f"{other}\n".encode()}, self.main
                )
            if self.commits[document["sha"]][1] != [self.main]:
                return _reply(422, {"message": "Update is not a fast forward"})
            self.main = document["sha"]
            return _reply(200, {"object": {"sha": self.main}})
        if rest.startswith("/contents/") and request.method == "PUT":
            path_name = rest.removeprefix("/contents/")
            self.main = self.commit_files(
                {path_name: base64.b64decode(document["content"])}, self.main
            )
            return _reply(201, {"commit": {"sha": self.main}})
        if rest.startswith("/releases") and request.method == "GET" and "assets" not in rest:
            page = int(urllib.parse.parse_qs(url.query).get("page", ["1"])[0])
            return _reply(200, self.releases if page == 1 else [])
        if rest == "/releases" and request.method == "POST":
            release_id = len(self.releases) + 1
            release = {
                "id": release_id,
                "tag_name": document["tag_name"],
                "draft": document["draft"],
                "make_latest": document.get("make_latest"),
                "assets": [],
                "upload_url": f"https://uploads.github.com{repo_path}/releases/{release_id}/assets"
                "{?name,label}",
            }
            self.releases.append(release)
            return _reply(201, release)
        if rest.startswith("/releases/") and request.method == "PATCH":
            release = next(r for r in self.releases if r["id"] == int(rest.rsplit("/", 1)[1]))
            release.update(document)
            return _reply(200, release)
        return _reply(500, {"message": f"unhandled {request.method} {rest}"})

    def _repo(self) -> dict[str, Any]:
        return {
            "name": self.repo,
            "private": self.private,
            "visibility": "private" if self.private else "public",
            "default_branch": "main",
        }


@dataclass
class FakeHub:
    owner: str = "lab"
    repo: str = "data"
    exists: bool = False
    files: dict[str, bytes] = field(default_factory=dict)
    lfs_paths: set[str] = field(default_factory=set)
    objects: dict[str, bytes] = field(default_factory=dict)
    head: int = 0
    log: list[tuple[str, str]] = field(default_factory=list)

    def send(self, request: HttpRequest, timeout: float) -> RawResponse:
        url = urllib.parse.urlsplit(request.url)
        self.log.append((request.method, request.url))
        body = request.body_bytes()
        repo_id = f"{self.owner}/{self.repo}"
        api = f"/api/datasets/{repo_id}"
        path = url.path
        if url.netloc == "lfs.example":
            oid = path.rsplit("/", 1)[1]
            if request.method == "PUT":
                assert hashlib.sha256(body).hexdigest() == oid
                self.objects[oid] = body
            return _reply(200, {})
        if path == "/api/whoami-v2":
            return _reply(200, {"name": self.owner})
        if path == "/api/repos/create":
            document = json.loads(body)
            assert document["type"] == "dataset" and document["private"] is False
            self.exists = True
            self.files[".gitattributes"] = b"*.zip filter=lfs\n"
            return _reply(200, {"url": f"https://huggingface.co/datasets/{repo_id}"})
        if not self.exists:
            return _reply(404, {"error": "Repository not found"})
        if path == api:
            return _reply(200, {"id": repo_id, "private": False, "gated": False})
        if path == f"{api}/revision/main":
            return _reply(200, {"sha": f"{self.head:040x}"})
        if path == f"{api}/paths-info/main":
            wanted = urllib.parse.parse_qs(body.decode())["paths"]
            out = []
            for name in wanted:
                if name not in self.files:
                    continue
                data = self.files[name]
                entry: dict[str, Any] = {
                    "type": "file",
                    "path": name,
                    "size": len(data),
                    "oid": _blob_sha(data),
                }
                if name in self.lfs_paths:
                    entry["lfs"] = {"oid": hashlib.sha256(data).hexdigest(), "size": len(data)}
                out.append(entry)
            return _reply(200, out)
        if path == f"{api}/preupload/main":
            document = json.loads(body)
            return _reply(
                200,
                {
                    "files": [
                        {
                            "path": f["path"],
                            "uploadMode": "lfs"
                            if f["path"].endswith((".zip", ".png"))
                            else "regular",
                        }
                        for f in document["files"]
                    ]
                },
            )
        if path == f"/datasets/{repo_id}.git/info/lfs/objects/batch":
            document = json.loads(body)
            obj = document["objects"][0]
            actions = (
                {}
                if obj["oid"] in self.objects
                else {
                    "upload": {"href": f"https://lfs.example/upload/{obj['oid']}", "header": {}},
                    "verify": {"href": f"https://lfs.example/verify/{obj['oid']}"},
                }
            )
            return _reply(
                200,
                {"objects": [{**obj, "actions": actions}] if actions else [obj]},
                headers={"content-type": "application/vnd.git-lfs+json"},
            )
        if path == f"{api}/commit/main":
            lines = [json.loads(line) for line in body.decode().splitlines() if line]
            header = lines[0]["value"]
            if header.get("parentCommit") not in (None, f"{self.head:040x}"):
                return _reply(412, {"error": "parent mismatch"})
            for line in lines[1:]:
                value = line["value"]
                if line["key"] == "file":
                    self.files[value["path"]] = base64.b64decode(value["content"])
                    self.lfs_paths.discard(value["path"])
                elif line["key"] == "lfsFile":
                    self.files[value["path"]] = self.objects[value["oid"]]
                    self.lfs_paths.add(value["path"])
            self.head += 1
            return _reply(200, {"commitOid": f"{self.head:040x}"})
        prefix = f"/datasets/{repo_id}/resolve/main/"
        if path.startswith(prefix):
            name = urllib.parse.unquote(path.removeprefix(prefix))
            found = self.files.get(name)
            if found is None:
                return _reply(404, {"error": "Entry not found"})
            data = found
            if "range" in {k.lower() for k in request.headers}:
                return _reply(206, raw=data[:1], headers={"access-control-allow-origin": "*"})
            return _reply(200, raw=data, headers={"access-control-allow-origin": "*"})
        return _reply(500, {"error": f"unhandled {request.method} {path}"})
