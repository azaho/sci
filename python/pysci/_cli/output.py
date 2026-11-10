"""Result objects and their rendering as human text or ``--json`` machine output."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TextIO

from .errors import EXIT_MEANINGS, ErrorRecord, Exit


@dataclass(slots=True)
class Result:
    """The outcome of one command.

    ``data`` is the machine-readable result object; ``lines`` is the human
    text. Errors and warnings are rendered for both audiences.
    """

    command: str
    data: dict[str, Any] = field(default_factory=dict)
    exit_code: int = Exit.OK
    errors: list[ErrorRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    partial: bool = False

    def say(self, line: str = "") -> None:
        self.lines.append(line)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, record: ErrorRecord) -> None:
        self.errors.append(record)

    @property
    def status(self) -> str:
        if self.exit_code == Exit.OK:
            return "ok"
        if self.partial:
            return "partial"
        return "failed"

    def to_json(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "status": self.status,
            "exit_code": int(self.exit_code),
            "result": self.data,
            "errors": [record.to_json() for record in self.errors],
            "warnings": list(self.warnings),
        }


def render(result: Result, *, as_json: bool, stdout: TextIO, stderr: TextIO) -> None:
    if as_json:
        stdout.write(json.dumps(result.to_json(), indent=2, ensure_ascii=False, sort_keys=False))
        stdout.write("\n")
        return
    for line in result.lines:
        stdout.write(line + "\n")
    for warning in result.warnings:
        stderr.write(f"warning: {warning}\n")
    # Commands such as `check` list their issues in the report itself; do not
    # print the same problem a second time on stderr.
    shown = {line.strip() for line in result.lines}
    for record in result.errors:
        text = str(record)
        if text in shown or text.removesuffix(" (retrying may help)") in shown:
            continue
        stderr.write(f"error: {text}\n")
    if result.exit_code != Exit.OK:
        meaning = EXIT_MEANINGS.get(int(result.exit_code), "")
        label = "partial result" if result.partial else "failed"
        stderr.write(f"sci {result.command}: {label} (exit {int(result.exit_code)}: {meaning})\n")


def indent(text: str, prefix: str = "  ") -> list[str]:
    return [prefix + line if line else line for line in text.splitlines()]
