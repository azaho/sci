"""CLI errors, exit codes, and conversion of protocol issues into error records."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import IntEnum
from typing import Any

from pysci.errors import Issue, Outcome, SciError


class Exit(IntEnum):
    """Process exit codes (cli/DESIGN.md, Results)."""

    OK = 0
    USAGE = 2
    """Invalid command or missing configuration."""
    INVALID = 3
    """Failed protocol, signature, or content check."""
    UNSUPPORTED = 4
    """Unsupported schema or service capability."""
    INCOMPLETE = 5
    """Incomplete operation, temporary failure, or partial result."""
    REFUSED = 6
    """Baseline refusal before signing or service policy refusal."""


EXIT_MEANINGS: dict[int, str] = {
    Exit.OK: "requested operation completed",
    Exit.USAGE: "invalid command or missing configuration",
    Exit.INVALID: "failed protocol, signature, or content check",
    Exit.UNSUPPORTED: "unsupported schema or service capability",
    Exit.INCOMPLETE: "incomplete operation, temporary failure, or partial result",
    Exit.REFUSED: "baseline refusal before signing or service policy refusal",
}

_OUTCOME_EXIT: dict[Outcome, Exit] = {
    Outcome.VALID: Exit.OK,
    Outcome.INVALID: Exit.INVALID,
    Outcome.UNSUPPORTED: Exit.UNSUPPORTED,
    Outcome.INCOMPLETE: Exit.INCOMPLETE,
}

# Severity order used to combine several exit codes into one.
_SEVERITY = [Exit.OK, Exit.INCOMPLETE, Exit.REFUSED, Exit.UNSUPPORTED, Exit.INVALID, Exit.USAGE]


def exit_for_outcome(outcome: Outcome) -> Exit:
    return _OUTCOME_EXIT[outcome]


def combine_exits(codes: Iterable[int]) -> Exit:
    """The most severe exit code; a mix of successes and failures is partial (5)."""
    values = [Exit(code) for code in codes]
    if not values:
        return Exit.OK
    failures = [code for code in values if code is not Exit.OK]
    if not failures:
        return Exit.OK
    if len(failures) < len(values):
        return Exit.INCOMPLETE
    return max(failures, key=_SEVERITY.index)


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    """One reported error: code, explanation, retryability, and validation location."""

    code: str
    message: str
    retryable: bool = False
    file: str | None = None
    field: str | None = None
    requirement: str | None = None
    observed: str | None = None
    section: str | None = None
    extra: dict[str, Any] = dc_field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        for name in ("file", "field", "requirement", "observed", "section"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        out.update(self.extra)
        return out

    def __str__(self) -> str:
        where = self.file or ""
        if self.field:
            where += ("#" if where else "") + self.field
        text = f"[{self.code}] "
        if where:
            text += where + ": "
        text += self.message
        if self.section:
            text += f" (§{self.section})"
        if self.observed is not None:
            text += f"; observed {self.observed}"
        if self.retryable:
            text += " (retrying may help)"
        return text


def issue_record(issue: Issue) -> ErrorRecord:
    return ErrorRecord(
        code=issue.code,
        message=issue.requirement,
        retryable=issue.outcome is Outcome.INCOMPLETE,
        file=issue.file,
        field=issue.field,
        requirement=issue.requirement,
        observed=issue.observed,
        section=issue.section,
    )


def issue_json(issue: Issue) -> dict[str, Any]:
    return issue_record(issue).to_json()


class CliError(Exception):
    """A failure reported to the user with an exit code and error records."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = Exit.USAGE,
        *,
        retryable: bool = False,
        records: Sequence[ErrorRecord] = (),
        result: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.exit_code = Exit(exit_code)
        head = ErrorRecord(code, message, retryable, extra=dict(extra.items()))
        self.records: list[ErrorRecord] = [head, *records]
        self.result = result or {}

    @property
    def code(self) -> str:
        return self.records[0].code

    @classmethod
    def from_sci(cls, exc: SciError, message: str | None = None) -> CliError:
        records = [issue_record(issue) for issue in exc.issues]
        exit_code = exit_for_outcome(exc.outcome)
        first = records[0]
        error = cls(
            first.code,
            message or first.message,
            exit_code,
            retryable=exc.outcome is Outcome.INCOMPLETE,
        )
        error.records = records if message is None else [error.records[0], *records]
        return error


def usage(code: str, message: str, **extra: Any) -> CliError:
    return CliError(code, message, Exit.USAGE, **extra)
