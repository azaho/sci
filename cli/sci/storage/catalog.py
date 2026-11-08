"""Verified metadata keyed by publication ID, with independent file memberships."""

from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any
from pysci import Invalid, Incomplete, load_bundle


class Catalog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db = sqlite3.connect(path)
            if self.db.execute("pragma quick_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("cache integrity")
            self._schema()
        except sqlite3.DatabaseError:
            self.db.close()
            if path.exists():
                path.replace(path.with_suffix(".corrupt"))
            self.db = sqlite3.connect(path)
            self._schema()

    def _schema(self) -> None:
        self.db.executescript("""
        create table if not exists publications(id text primary key, data text not null);
        create table if not exists memberships(path text primary key, identity text not null, id text not null);
        """)

    @staticmethod
    def identity(path: Path) -> str:
        st = path.lstat()
        return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"

    def scan(self, folder: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        if not folder.is_dir():
            raise Invalid("folder unavailable")
        # An unsuccessful directory listing must not delete old memberships.
        paths = sorted(p for p in folder.iterdir() if p.name.endswith(".zip"))
        current = {str(p) for p in paths}
        results, errors = [], []
        for path in paths:
            try:
                identity = self.identity(path)
                row = self.db.execute(
                    "select p.data from memberships m join publications p using(id) where m.path=? and m.identity=?",
                    (str(path), identity),
                ).fetchone()
                if row:
                    obj = json.loads(row[0])
                else:
                    bundle = load_bundle(path)
                    if self.identity(path) != identity:
                        raise Incomplete("input changed during verification")
                    obj = {
                        "id": bundle.id,
                        "metadata": bundle.metadata,
                        "signer": bundle.signer,
                        "readme": bundle.entries["README.md"].decode(),
                    }
                    with self.db:
                        self.db.execute(
                            "insert or replace into publications values(?,?)",
                            (bundle.id, json.dumps(obj)),
                        )
                        self.db.execute(
                            "insert or replace into memberships values(?,?,?)",
                            (str(path), identity, bundle.id),
                        )
                results.append({**obj, "path": str(path)})
            except Exception as exc:
                errors.append({"path": str(path), "error": str(exc)})
                with self.db:
                    self.db.execute("delete from memberships where path=?", (str(path),))
        with self.db:
            for (path,) in self.db.execute("select path from memberships").fetchall():
                if Path(path).parent == folder and path not in current:
                    self.db.execute("delete from memberships where path=?", (path,))
            self.db.execute("delete from publications where id not in (select id from memberships)")
        return results, errors

    def close(self) -> None:
        self.db.close()
