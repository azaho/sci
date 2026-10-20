"""Local-only ZIP packaging. The publishing service never imports this module."""

import bisect
import os
import stat
import tempfile
import zipfile
from pathlib import Path

from sci.errors import SCIError
from sci.validation import (
    PAYLOAD_LIMIT,
    PREVIEW_COUNT,
    PREVIEW_LIMIT,
    compact,
    path_order,
    safe_path,
)


def preview_paths(entries):
    """Keep a sorted prefix with O(200) candidate memory, regardless of entry count."""
    candidates = []
    omitted = False
    for name in entries:
        parts = safe_path(name)
        eligible = []
        for depth in range(1, min(2, len(parts)) + 1):
            is_dir = depth < len(parts) or name.endswith("/")
            eligible.append("/".join(parts[:depth]) + ("/" if is_dir else ""))
        for path in eligible:
            key = path_order(path)
            i = bisect.bisect_left(candidates, key)
            if i < len(candidates) and candidates[i] == key:
                continue
            candidates.insert(i, key)
            if len(candidates) > PREVIEW_COUNT:
                candidates.pop()
                omitted = True
    preview = {"paths": [p for _, p in candidates], "truncated": omitted}
    while len(compact(preview)) > PREVIEW_LIMIT:
        preview["paths"].pop()
        preview["truncated"] = True
    return preview


def _entries(root):
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise SCIError(f"Supporting files must be regular files/directories, not links: {path}")
            relative = path.relative_to(root).as_posix() + ("/" if stat.S_ISDIR(mode) else "")
            safe_path(relative)
            yield path, relative


def package(root: Path, destination: Path):
    if root.is_symlink() or not root.is_dir():
        raise SCIError("files/ must be a directory, not a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".sci-zip-", dir=destination.parent)
    os.close(fd)
    try:
        def names():
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                for path, name in _entries(root):
                    if path.is_dir():
                        archive.writestr(name, b"")
                    else:
                        # O_NOFOLLOW prevents switching a checked input to a symlink.
                        source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                        with os.fdopen(source_fd, "rb") as source, archive.open(name, "w") as target:
                            while block := source.read(1024 * 1024):
                                target.write(block)
                                if archive.fp.tell() > PAYLOAD_LIMIT:
                                    raise SCIError("files.zip exceeds 50,000,000 bytes")
                    yield name
        preview = preview_paths(names())
        if os.path.getsize(temporary) > PAYLOAD_LIMIT:
            raise SCIError("files.zip exceeds 50,000,000 bytes")
        os.replace(temporary, destination)
        return preview
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepared_preview(path: Path):
    try:
        with zipfile.ZipFile(path) as archive:
            def names():
                for info in archive.infolist():
                    if stat.S_ISLNK(info.external_attr >> 16):
                        raise SCIError(f"ZIP contains a symlink: {info.filename}")
                    yield info.filename
            return preview_paths(names())
    except (zipfile.BadZipFile, UnicodeError) as exc:
        raise SCIError(f"files.zip is not a readable ZIP: {exc}") from exc
