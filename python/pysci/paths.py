"""Bundle file path rules (protocol §3.1)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from .errors import IssueCollector

__all__ = [
    "MAX_PATH_BYTES",
    "check_path",
    "check_path_conflicts",
    "check_paths",
    "is_valid_path",
    "path_sort_key",
]

MAX_PATH_BYTES: Final = 255
_COMPONENT: Final = re.compile(r"[A-Za-z0-9._-]+")
_DEVICE: Final = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", re.IGNORECASE)


def path_sort_key(path: str) -> bytes:
    """Ordering key: ascending lexicographic order of the ASCII path bytes."""
    return path.encode("utf-8")


def _path_problem(path: str) -> tuple[str, str] | None:
    if not path:
        return ("path_empty", "path must not be empty")
    if not path.isascii():
        return ("path_component", "paths use only ASCII letters, digits, '.', '_', and '-'")
    if len(path.encode("ascii")) > MAX_PATH_BYTES:
        return ("path_too_long", f"full path must be at most {MAX_PATH_BYTES} ASCII bytes")
    for component in path.split("/"):
        if not component:
            return (
                "path_empty_component",
                "path components must be nonempty (no leading, trailing, or repeated '/')",
            )
        if not _COMPONENT.fullmatch(component):
            return (
                "path_component",
                "components use only ASCII letters, digits, '.', '_', and '-'",
            )
        if component in (".", "..") or component.endswith("."):
            return ("path_dot_component", "'.', '..', and components ending in '.' are prohibited")
        if _DEVICE.fullmatch(component):
            return ("path_device_name", "Windows device names are prohibited in any component")
    return None


def is_valid_path(path: str) -> bool:
    """True if ``path`` satisfies the per-path rules of §3.1."""
    return _path_problem(path) is None


def check_path(
    path: str, *, collector: IssueCollector, file: str | None = None, field: str | None = None
) -> bool:
    """Check one path; append an issue and return False on failure."""
    problem = _path_problem(path)
    if problem is None:
        return True
    code, requirement = problem
    collector.add(code, requirement, section="3.1", file=file, field=field, observed=path)
    return False


def check_path_conflicts(
    paths: Iterable[str],
    *,
    collector: IssueCollector,
    file: str | None = None,
    field: str | None = None,
) -> None:
    """Case-insensitive file/file, file/directory, and directory-spelling conflicts."""
    files: dict[str, str] = {}
    directories: dict[str, str] = {}
    for path in paths:
        folded = path.lower()
        if folded in files:
            collector.add(
                "path_conflict",
                "paths must differ without regard to ASCII case",
                section="3.1",
                file=file,
                field=field,
                observed=[files[folded], path],
            )
        else:
            files[folded] = path
        components = path.split("/")
        for depth in range(1, len(components)):
            directory = "/".join(components[:depth])
            existing = directories.setdefault(directory.lower(), directory)
            if existing != directory:
                collector.add(
                    "path_directory_case",
                    "every occurrence of a directory must use the same capitalization",
                    section="3.1",
                    file=file,
                    field=field,
                    observed=[existing, directory],
                )
    for folded, directory in directories.items():
        if folded in files:
            collector.add(
                "path_file_directory_conflict",
                "a file path must not also name a directory used by another entry",
                section="3.1",
                file=file,
                field=field,
                observed=[files[folded], directory],
            )


def check_paths(
    paths: Iterable[str],
    *,
    collector: IssueCollector,
    file: str | None = None,
    field_prefix: str | None = None,
) -> None:
    """Per-path rules plus conflict checks for a full set of paths."""
    path_list = list(paths)
    for path in path_list:
        field = (
            None
            if field_prefix is None
            else field_prefix + "/" + path.replace("~", "~0").replace("/", "~1")
        )
        check_path(path, collector=collector, file=file, field=field)
    check_path_conflicts(path_list, collector=collector, file=file, field=field_prefix)
