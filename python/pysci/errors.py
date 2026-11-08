"""Outcome vocabulary, structured issues, and the exception hierarchy.

Every check in the library reports failures as :class:`Issue` records so that
callers (such as the CLI) can show the file or field, the violated
requirement, and the observed value. Fail-fast parsers raise a
:class:`SciError` subclass carrying the issues; collecting validators return
lists of issues.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Incomplete",
    "Invalid",
    "Issue",
    "IssueCollector",
    "Outcome",
    "SciError",
    "Unsupported",
    "observed_repr",
    "raise_for",
]


class Outcome(StrEnum):
    """Verification outcome (protocol §6)."""

    VALID = "valid"
    """The requested checks succeeded."""
    INVALID = "invalid"
    """A required format, signature, binding, or association check failed."""
    UNSUPPORTED = "unsupported"
    """The ``schema`` string identifies no supported schema."""
    INCOMPLETE = "incomplete"
    """Unavailable evidence or a local resource limit prevented the checks."""


def observed_repr(value: object, limit: int = 120) -> str:
    """Return a short, printable representation of an observed value."""
    text = repr(value)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


@dataclass(frozen=True, slots=True)
class Issue:
    """One failed requirement.

    Attributes:
        code: Stable machine-readable identifier, e.g. ``"text_not_assignable"``.
        requirement: Human-readable statement of the violated requirement.
        section: Protocol section, e.g. ``"4.2"``.
        file: Bundle path or artifact the issue concerns, e.g. ``"sci.json"``.
        field: JSON pointer (RFC 6901) of the offending value inside ``file``.
        observed: Short representation of the observed value, when useful.
        outcome: The outcome this issue implies (invalid, unsupported, incomplete).
    """

    code: str
    requirement: str
    section: str | None = None
    file: str | None = None
    field: str | None = None
    observed: str | None = None
    outcome: Outcome = Outcome.INVALID

    def located(self, *, file: str | None = None, prefix: str | None = None) -> Issue:
        """Return a copy attributed to ``file`` and/or with a JSON-pointer prefix."""
        new_field = self.field
        if prefix is not None:
            new_field = prefix + (self.field or "")
        return Issue(
            code=self.code,
            requirement=self.requirement,
            section=self.section,
            file=file if file is not None else self.file,
            field=new_field,
            observed=self.observed,
            outcome=self.outcome,
        )

    def __str__(self) -> str:
        where = ""
        if self.file:
            where = self.file
        if self.field:
            where += ("#" if where else "") + self.field
        parts = [f"[{self.code}]"]
        if where:
            parts.append(where + ":")
        parts.append(self.requirement)
        if self.section:
            parts.append(f"(§{self.section})")
        if self.observed is not None:
            parts.append(f"observed {self.observed}")
        return " ".join(parts)


def _worst(issues: Iterable[Issue]) -> Outcome:
    outcomes = {issue.outcome for issue in issues}
    for candidate in (Outcome.UNSUPPORTED, Outcome.INVALID, Outcome.INCOMPLETE):
        if candidate in outcomes:
            return candidate
    return Outcome.VALID


class SciError(Exception):
    """Base class for protocol check failures. Carries one or more issues."""

    default_outcome = Outcome.INVALID

    def __init__(self, issues: Issue | Sequence[Issue]) -> None:
        self.issues: tuple[Issue, ...] = (issues,) if isinstance(issues, Issue) else tuple(issues)
        if not self.issues:
            raise ValueError("SciError requires at least one issue")
        super().__init__("; ".join(str(issue) for issue in self.issues))

    @property
    def outcome(self) -> Outcome:
        return _worst(self.issues)

    @property
    def issue(self) -> Issue:
        """The first issue."""
        return self.issues[0]


class Invalid(SciError):
    """A required check failed."""


class Unsupported(SciError):
    """A schema string identifies no supported schema version."""

    default_outcome = Outcome.UNSUPPORTED


class Incomplete(SciError):
    """Evidence was unavailable or a local limit prevented the check."""

    default_outcome = Outcome.INCOMPLETE


def raise_for(issues: Sequence[Issue]) -> None:
    """Raise the exception class matching the worst issue, if any."""
    if not issues:
        return
    worst = _worst(issues)
    if worst is Outcome.UNSUPPORTED:
        raise Unsupported(issues)
    if worst is Outcome.INCOMPLETE:
        raise Incomplete(issues)
    raise Invalid(issues)


@dataclass(slots=True)
class IssueCollector:
    """Accumulates issues from independent checks."""

    issues: list[Issue] = field(default_factory=list)

    def add(
        self,
        code: str,
        requirement: str,
        *,
        section: str | None = None,
        file: str | None = None,
        field: str | None = None,
        observed: object = None,
        outcome: Outcome = Outcome.INVALID,
    ) -> None:
        self.issues.append(
            Issue(
                code=code,
                requirement=requirement,
                section=section,
                file=file,
                field=field,
                observed=None if observed is None else observed_repr(observed),
                outcome=outcome,
            )
        )

    def extend(self, issues: Iterable[Issue]) -> None:
        self.issues.extend(issues)

    def __bool__(self) -> bool:
        return bool(self.issues)

    def __len__(self) -> int:
        return len(self.issues)

    @property
    def outcome(self) -> Outcome:
        return _worst(self.issues)

    def raise_if_any(self) -> None:
        raise_for(self.issues)
