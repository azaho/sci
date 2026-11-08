"""Publication metadata ``sci.json`` (protocol §4) and its checks (§6.3)."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Final

from .citations import CITATION_RELATIONS, CitationTarget, parse_citation_target
from .errors import IssueCollector
from .jsonstrict import JsonObject, JsonValue, json_pointer, json_type_name, parse_json_object
from .text import check_text

__all__ = [
    "MAX_CATEGORIES",
    "MAX_CITATIONS",
    "MAX_KEYWORDS",
    "Category",
    "Citation",
    "DisplayItem",
    "Metadata",
    "check_metadata",
    "parse_metadata",
]

MAX_TITLE: Final = 200
MAX_ABSTRACT: Final = 2500
MAX_CATEGORIES: Final = 3
MAX_KEYWORDS: Final = 20
MAX_KEYWORD: Final = 64
MAX_CATEGORY_CODE: Final = 128
MAX_CAPTION: Final = 2000
MAX_CITATIONS: Final = 1000
MAX_REASON: Final = 1000
_SCHEME: Final = re.compile(r"[a-z][a-z0-9.-]{0,63}")
_FIELDS: Final = ("title", "abstract", "categories", "keywords", "display", "citations")
_FILE: Final = "sci.json"


@dataclass(frozen=True, slots=True)
class Category:
    scheme: str
    code: str


@dataclass(frozen=True, slots=True)
class DisplayItem:
    path: str
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class Citation:
    target: CitationTarget
    relations: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Metadata:
    """Validated publication metadata."""

    title: str
    abstract: str
    categories: tuple[Category, ...] = ()
    keywords: tuple[str, ...] = ()
    display: tuple[DisplayItem, ...] = ()
    citations: tuple[Citation, ...] = ()
    raw: JsonObject = field(default_factory=dict, compare=False, repr=False)


def parse_metadata(data: bytes, listed_paths: Collection[str] | None = None) -> Metadata:
    """Decode and check ``sci.json``. Raises :class:`~pysci.errors.Invalid` with all issues.

    ``listed_paths`` are the manifest's ``files`` keys used to check display
    paths (§4.3); pass None to skip that cross-check (e.g. for drafts, where the
    caller supplies the planned file list instead).
    """
    record = parse_json_object(data, file=_FILE)
    issues = IssueCollector()
    metadata = check_metadata(record, listed_paths, issues)
    issues.raise_if_any()
    assert metadata is not None
    return metadata


def check_metadata(
    record: JsonObject, listed_paths: Collection[str] | None, issues: IssueCollector
) -> Metadata | None:
    """Collect every §4 violation in a decoded ``sci.json`` object."""
    start = len(issues)
    _check_keys(record, "", issues)
    extra = sorted(set(record) - set(_FIELDS))
    if extra:
        issues.add(
            "metadata_fields",
            "sci.json permits only title, abstract, categories, keywords, display, and citations",
            section="4",
            file=_FILE,
            observed=extra,
        )
    title = _string(record, "title", "/title", issues, max_len=MAX_TITLE, required=True)
    abstract = _string(
        record, "abstract", "/abstract", issues, max_len=MAX_ABSTRACT, required=True, multiline=True
    )
    categories = _categories(record, issues)
    keywords = _keywords(record, issues)
    display = _display(record, listed_paths, issues)
    citations = _citations(record, issues)
    if len(issues) != start:
        return None
    assert title is not None
    assert abstract is not None
    return Metadata(
        title=title,
        abstract=abstract,
        categories=categories,
        keywords=keywords,
        display=display,
        citations=citations,
        raw=record,
    )


def _check_keys(value: JsonValue, where: str, issues: IssueCollector) -> None:
    """Object keys are decoded strings and follow §4.2 (single-line rules)."""
    stack: list[tuple[JsonValue, str]] = [(value, where)]
    while stack:
        item, pointer = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                child_pointer = pointer + json_pointer(key)
                check_text(key, file=_FILE, field=child_pointer, collector=issues)
                stack.append((child, child_pointer))
        elif isinstance(item, list):
            stack.extend((child, f"{pointer}/{index}") for index, child in enumerate(item))


def _string(
    container: JsonObject,
    name: str,
    where: str,
    issues: IssueCollector,
    *,
    max_len: int,
    required: bool = False,
    multiline: bool = False,
) -> str | None:
    if name not in container:
        if required:
            issues.add(
                "metadata_required", f"{name} is required", section="4", file=_FILE, field=where
            )
        return None
    value = container[name]
    if not isinstance(value, str):
        issues.add(
            "metadata_type",
            f"{name} must be a string",
            section="4",
            file=_FILE,
            field=where,
            observed=json_type_name(value),
        )
        return None
    before = len(issues)
    check_text(
        value,
        multiline=multiline,
        nonblank=True,
        max_code_points=max_len,
        file=_FILE,
        field=where,
        collector=issues,
    )
    return value if len(issues) == before else None


def _array(
    record: JsonObject, name: str, issues: IssueCollector, max_items: int | None
) -> list[JsonValue]:
    value = record.get(name, [])
    if not isinstance(value, list):
        issues.add(
            "metadata_type",
            f"{name} must be an array",
            section="4",
            file=_FILE,
            field="/" + name,
            observed=json_type_name(value),
        )
        return []
    if max_items is not None and len(value) > max_items:
        issues.add(
            "metadata_array_length",
            f"{name} has at most {max_items} items",
            section="4",
            file=_FILE,
            field="/" + name,
            observed=f"{len(value)} items",
        )
    return value


def _object(
    item: JsonValue,
    where: str,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    label: str,
    issues: IssueCollector,
) -> JsonObject | None:
    if not isinstance(item, dict):
        issues.add(
            "metadata_type",
            f"each {label} must be an object",
            section="4",
            file=_FILE,
            field=where,
            observed=json_type_name(item),
        )
        return None
    missing = [name for name in required if name not in item]
    extra = sorted(set(item) - set(required) - set(optional))
    if missing:
        issues.add(
            "metadata_fields",
            f"{label} requires {', '.join(required)}",
            section="4",
            file=_FILE,
            field=where,
            observed=missing,
        )
    if extra:
        allowed = ", ".join(required + optional)
        issues.add(
            "metadata_fields",
            f"{label} permits only {allowed}",
            section="4",
            file=_FILE,
            field=where,
            observed=extra,
        )
    return item


def _categories(record: JsonObject, issues: IssueCollector) -> tuple[Category, ...]:
    result: list[Category] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(_array(record, "categories", issues, MAX_CATEGORIES)):
        where = f"/categories/{index}"
        obj = _object(item, where, ("scheme", "code"), (), "category", issues)
        if obj is None:
            continue
        scheme = obj.get("scheme")
        if "scheme" in obj and not (isinstance(scheme, str) and _SCHEME.fullmatch(scheme)):
            issues.add(
                "category_scheme",
                "scheme must match [a-z][a-z0-9.-]{0,63}",
                section="4.1",
                file=_FILE,
                field=where + "/scheme",
                observed=scheme,
            )
            scheme = None
        code = _string(obj, "code", where + "/code", issues, max_len=MAX_CATEGORY_CODE)
        if isinstance(scheme, str) and code is not None:
            if (scheme, code) in seen:
                issues.add(
                    "category_duplicate",
                    "duplicate (scheme, code) pairs are prohibited",
                    section="4.1",
                    file=_FILE,
                    field=where,
                    observed=[scheme, code],
                )
            seen.add((scheme, code))
            result.append(Category(scheme, code))
    return tuple(result)


def _keywords(record: JsonObject, issues: IssueCollector) -> tuple[str, ...]:
    result: list[str] = []
    for index, item in enumerate(_array(record, "keywords", issues, MAX_KEYWORDS)):
        value = _string({"k": item}, "k", f"/keywords/{index}", issues, max_len=MAX_KEYWORD)
        if value is not None:
            result.append(value)
    return tuple(result)


def _display(
    record: JsonObject, listed_paths: Collection[str] | None, issues: IssueCollector
) -> tuple[DisplayItem, ...]:
    result: list[DisplayItem] = []
    seen: set[str] = set()
    for index, item in enumerate(_array(record, "display", issues, None)):
        where = f"/display/{index}"
        obj = _object(item, where, ("path",), ("caption",), "display item", issues)
        if obj is None:
            continue
        path = obj.get("path")
        ok = True
        if "path" in obj:
            if (
                not isinstance(path, str)
                or not path.startswith("files/")
                or (listed_paths is not None and path not in listed_paths)
            ):
                issues.add(
                    "display_path",
                    "display path must exactly match an entry under files/ in the manifest",
                    section="4.3",
                    file=_FILE,
                    field=where + "/path",
                    observed=path,
                )
                ok = False
            elif path in seen:
                issues.add(
                    "display_duplicate",
                    "display paths must be distinct",
                    section="4.3",
                    file=_FILE,
                    field=where + "/path",
                    observed=path,
                )
                ok = False
        caption = _string(obj, "caption", where + "/caption", issues, max_len=MAX_CAPTION)
        if isinstance(path, str) and ok:
            seen.add(path)
            result.append(DisplayItem(path, caption))
    return tuple(result)


def _citations(record: JsonObject, issues: IssueCollector) -> tuple[Citation, ...]:
    result: list[Citation] = []
    for index, item in enumerate(_array(record, "citations", issues, MAX_CITATIONS)):
        where = f"/citations/{index}"
        obj = _object(item, where, ("target",), ("relations", "reason"), "citation", issues)
        if obj is None:
            continue
        target_value = obj.get("target")
        target: CitationTarget | None = None
        if "target" in obj:
            if not isinstance(target_value, str):
                issues.add(
                    "citation_target",
                    "target must be a string",
                    section="4.4",
                    file=_FILE,
                    field=where + "/target",
                    observed=json_type_name(target_value),
                )
            else:
                check_text(target_value, file=_FILE, field=where + "/target", collector=issues)
                target = parse_citation_target(target_value)
                if target is None:
                    issues.add(
                        "citation_target",
                        "target must be sci:sha256:<hash>, a doi: URI, or an http(s) URI",
                        section="4.4",
                        file=_FILE,
                        field=where + "/target",
                        observed=target_value,
                    )
        relations: tuple[str, ...] = ()
        if "relations" in obj:
            value = obj["relations"]
            if not (isinstance(value, list) and all(isinstance(r, str) for r in value)):
                issues.add(
                    "citation_relations",
                    "relations must be an array of strings",
                    section="4.4",
                    file=_FILE,
                    field=where + "/relations",
                    observed=json_type_name(value),
                )
            else:
                names = [r for r in value if isinstance(r, str)]
                unknown = sorted(set(names) - CITATION_RELATIONS)
                if unknown:
                    issues.add(
                        "citation_relations",
                        "relations must come from uses, reviews, supports, challenges, supersedes",
                        section="4.4",
                        file=_FILE,
                        field=where + "/relations",
                        observed=unknown,
                    )
                if len(set(names)) != len(names):
                    issues.add(
                        "citation_relations",
                        "relations must be distinct",
                        section="4.4",
                        file=_FILE,
                        field=where + "/relations",
                        observed=names,
                    )
                relations = tuple(names)
        reason = _string(
            obj, "reason", where + "/reason", issues, max_len=MAX_REASON, multiline=True
        )
        if target is not None:
            result.append(Citation(target, relations, reason))
    return tuple(result)
