"""``catalog.sqlite``: the local search cache keyed by verified publication ID.

File state (device, inode, size, modification and change times) detects
changes; it is not a content proof. Unchanged files reuse their rows without
opening the ZIP. Other commands verify their inputs independently.

Several processes on one machine can share a catalog (WAL mode). WAL does not
work across machines, so keep the state directory on a local disk.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pysci.bundle import read_zip_entry, verify_zip
from pysci.errors import Outcome, SciError
from pysci.manifest import README_PATH
from pysci.source import FileSource

from ..errors import issue_json

SCHEMA_VERSION = "1"
BUSY_TIMEOUT_SECONDS = 30.0
_CORRUPT_CODES = (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
_BUSY_CODES = (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    folder TEXT NOT NULL,
    dev INTEGER NOT NULL,
    ino INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    publication_id TEXT,
    status TEXT NOT NULL,
    issues TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS files_folder ON files(folder);
CREATE INDEX IF NOT EXISTS files_publication ON files(publication_id);
CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    keywords TEXT NOT NULL,
    categories TEXT NOT NULL,
    citations TEXT NOT NULL,
    signer_x TEXT NOT NULL,
    baseline INTEGER NOT NULL,
    search_text TEXT NOT NULL
);
"""

SEARCHED_FIELDS = ("title", "abstract", "keywords", "README.md")


@dataclass(slots=True)
class ScanReport:
    available: list[str] = field(default_factory=list)
    unavailable: list[dict[str, str]] = field(default_factory=list)
    verified: int = 0
    reused: int = 0
    invalid: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class LocalHit:
    publication_id: str
    title: str
    abstract: str
    signer_x: str
    citations: list[Any]
    categories: list[Any]
    keywords: list[str]
    baseline: bool
    paths: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.publication_id,
            "title": self.title,
            "abstract": self.abstract,
            "signer": {"kty": "OKP", "crv": "Ed25519", "x": self.signer_x},
            "categories": self.categories,
            "keywords": self.keywords,
            "citations": self.citations,
            "baseline": self.baseline,
            "paths": self.paths,
        }


class _SchemaMismatch(Exception):
    """The file is a database from another catalog schema."""


def is_corrupt(exc: sqlite3.Error) -> bool:
    """True only for damage to the file; a busy or locked database is not damaged."""
    return (exc.sqlite_errorcode & 0xFF) in _CORRUPT_CODES


def _stat_key(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class Catalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rebuilt = False
        self.db = self._open()

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            return self._connect()
        except _SchemaMismatch:
            pass
        except sqlite3.DatabaseError as exc:
            if not is_corrupt(exc):
                raise
        self._discard()
        return self._connect()

    def _discard(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(str(self.path) + suffix)
        self.rebuilt = True

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            self._enable_wal(db)
            # An existing catalog is opened without taking the write lock.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() is None:
                db.executescript(
                    f"BEGIN IMMEDIATE;{_SCHEMA}INSERT OR IGNORE INTO meta(key, value) "
                    f"VALUES ('schema', '{SCHEMA_VERSION}');COMMIT;"
                )
            row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                raise _SchemaMismatch
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _enable_wal(db: sqlite3.Connection) -> None:
        try:
            db.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError as exc:
            # Another process holds the lock; a later open switches the mode.
            if (exc.sqlite_errorcode & 0xFF) not in _BUSY_CODES:
                raise

    def reset(self) -> None:
        """Replace a catalog found to be corrupt after opening."""
        self.db.close()
        self._discard()
        self.db = self._connect()

    def close(self) -> None:
        self.db.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # ------------------------------------------------------------------ scanning

    def scan(self, folders: Sequence[Path]) -> ScanReport:
        report = ScanReport()
        changed = False
        for folder in folders:
            key = str(folder)
            try:
                entries = list(os.scandir(folder))
            except OSError as exc:
                report.unavailable.append({"folder": key, "error": str(exc)})
                continue
            current: dict[str, os.stat_result] = {}
            for entry in entries:
                if not entry.name.endswith(".zip"):
                    continue
                try:
                    info = os.stat(entry.path)
                except OSError:
                    continue
                if not os.path.isfile(entry.path):
                    continue
                current[entry.path] = info
            known = {
                row[0]: tuple(row[1:6])
                for row in self.db.execute(
                    "SELECT path, dev, ino, size, mtime_ns, ctime_ns FROM files WHERE folder=?",
                    (key,),
                )
            }
            # Random order spreads processes that start together over different files.
            for path, info in random.sample(sorted(current.items()), len(current)):
                state = _stat_key(info)
                if known.get(path) == state or self._indexed(path, state):
                    report.reused += 1
                    continue
                self._index_file(Path(path), key, info, report)
            # A path missing from this listing may have been added since it was taken.
            absent = [p for p in known if p not in current and not os.path.exists(p)]
            if absent:
                changed = True
                with self._write() as db:
                    db.executemany("DELETE FROM files WHERE path=?", [(p,) for p in absent])
            report.available.append(key)
            for row in self.db.execute(
                "SELECT path, status, publication_id, issues FROM files "
                "WHERE folder=? AND status != 'valid' ORDER BY path",
                (key,),
            ):
                report.invalid.append(
                    {
                        "path": row[0],
                        "status": row[1],
                        "publication_id": row[2],
                        "issues": json.loads(row[3]),
                    }
                )
        if changed or report.verified:
            with self._write() as db:
                db.execute(
                    "DELETE FROM publications WHERE id NOT IN "
                    "(SELECT publication_id FROM files WHERE publication_id IS NOT NULL)"
                )
        return report

    def _indexed(self, path: str, state: tuple[int, ...]) -> bool:
        """True if another process has indexed this file state since the scan began."""
        row = self.db.execute(
            "SELECT dev, ino, size, mtime_ns, ctime_ns FROM files WHERE path=?", (path,)
        ).fetchone()
        return row is not None and tuple(row) == state

    def _index_file(
        self, path: Path, folder: str, info: os.stat_result, report: ScanReport
    ) -> None:
        result = verify_zip(path)
        report.verified += 1
        if result.outcome is Outcome.INCOMPLETE:
            # Not cached: an unavailable file or local limit is retried next time.
            report.invalid.append(
                {
                    "path": str(path),
                    "status": str(result.outcome),
                    "publication_id": result.publication_id,
                    "issues": [issue_json(i) for i in result.issues],
                }
            )
            return
        publication: tuple[Any, ...] | None = None
        status = str(result.outcome)
        issues = [issue_json(i) for i in result.issues]
        if result.ok and result.metadata is not None and result.manifest is not None:
            try:
                with FileSource(path) as source:
                    _, readme = read_zip_entry(
                        source, README_PATH, expected_id=result.publication_id
                    )
            except (SciError, OSError) as exc:
                status, issues = "incomplete", [{"code": "read_failed", "message": str(exc)}]
                report.invalid.append({"path": str(path), "status": status, "issues": issues})
                return
            meta = result.metadata
            raw = meta.raw
            search_text = "\n".join(
                [meta.title, meta.abstract, " ".join(meta.keywords), readme.decode("utf-8")]
            ).casefold()
            publication = (
                result.manifest.publication_id,
                meta.title,
                meta.abstract,
                json.dumps(list(meta.keywords)),
                json.dumps(raw.get("categories", [])),
                json.dumps(raw.get("citations", [])),
                result.manifest.public_key.x,
                1 if result.baseline else 0,
                search_text,
            )
        with self._write() as db:
            if publication is not None:
                db.execute(
                    "INSERT OR REPLACE INTO publications VALUES (?,?,?,?,?,?,?,?,?)", publication
                )
            db.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    str(path),
                    folder,
                    *_stat_key(info),
                    result.publication_id if publication is not None else None,
                    status,
                    json.dumps(issues),
                ),
            )

    # ------------------------------------------------------------------ queries

    def search(
        self,
        folders: Sequence[str],
        terms: Sequence[str],
        *,
        category: tuple[str, str] | None,
        key_x: str | None,
        after: str | None,
        limit: int,
    ) -> tuple[list[LocalHit], bool]:
        """Matches in ID order after ``after``; returns (hits, more available)."""
        if not folders:
            return [], False
        marks = ",".join("?" for _ in folders)
        sql = (
            "SELECT p.id, p.title, p.abstract, p.signer_x, p.citations, p.categories, "
            "p.keywords, p.baseline, group_concat(f.path, char(10)) "
            "FROM publications p JOIN files f ON f.publication_id = p.id "
            f"WHERE f.folder IN ({marks}) AND p.id > ?"
        )
        params: list[Any] = [*folders, after or ""]
        for term in terms:
            sql += " AND instr(p.search_text, ?) > 0"
            params.append(term)
        if key_x is not None:
            sql += " AND p.signer_x = ?"
            params.append(key_x)
        sql += " GROUP BY p.id ORDER BY p.id"
        hits: list[LocalHit] = []
        for row in self.db.execute(sql, params):
            categories = json.loads(row[5])
            if category is not None and not any(
                c.get("scheme") == category[0] and c.get("code") == category[1] for c in categories
            ):
                continue
            hits.append(
                LocalHit(
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    json.loads(row[4]),
                    categories,
                    json.loads(row[6]),
                    bool(row[7]),
                    sorted(row[8].split("\n")),
                )
            )
            if len(hits) > limit:
                break
        return hits[:limit], len(hits) > limit
