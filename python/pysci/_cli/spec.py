"""Command definitions from which the argument parser and the help text are generated."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import EXIT_MEANINGS

if TYPE_CHECKING:
    from .context import Context
    from .output import Result


@dataclass(frozen=True)
class Arg:
    flags: tuple[str, ...]
    help: str
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def positional(self) -> bool:
        return not self.flags[0].startswith("-")


def arg(*flags: str, help: str, **kwargs: Any) -> Arg:
    return Arg(tuple(flags), help, kwargs)


TIMEOUT_ARG = arg(
    "--timeout",
    type=float,
    metavar="SECONDS",
    default=120.0,
    help="operation deadline including retries; for batches, per publication and destination",
)


@dataclass(frozen=True)
class Command:
    """One subcommand: arguments, documentation sections, and its handler."""

    name: str
    usage_hint: str
    summary: str
    description: str
    args: tuple[Arg, ...]
    outputs: str
    files: str
    network: str
    exit_codes: dict[int, str]
    examples: tuple[str, ...]
    run: Callable[[argparse.Namespace, Context], Result]
    timeout: bool = False

    def epilog(self) -> str:
        lines = ["output:"]
        lines += [f"  {line}" for line in self.outputs.strip().splitlines()]
        lines += ["  --json prints one result object (command, status, exit_code, result,"]
        lines += ["  errors with code/message/retryable, warnings) instead of text."]
        lines += ["", "files changed:"]
        lines += [f"  {line}" for line in self.files.strip().splitlines()]
        lines += ["", "network:"]
        lines += [f"  {line}" for line in self.network.strip().splitlines()]
        lines += ["", "exit codes:"]
        codes = {0: EXIT_MEANINGS[0], 2: EXIT_MEANINGS[2], **self.exit_codes}
        for code in sorted(codes):
            lines.append(f"  {code}  {codes[code]}")
        lines += ["", "example:" if len(self.examples) == 1 else "examples:"]
        lines += [f"  {example}" for example in self.examples]
        return "\n".join(lines)


def _help_text(spec: Arg) -> str:
    text = spec.help
    default = spec.kwargs.get("default")
    if (
        default not in (None, False, argparse.SUPPRESS, [])
        and "default" not in text
        and spec.kwargs.get("action") not in ("store_true", "store_false")
    ):
        text += " (default: %(default)s)"
    return text.replace("%", "%%").replace("%%(default)s", "%(default)s")


class _Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=30, width=100)


def add_command(subparsers: Any, command: Command) -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = subparsers.add_parser(
        command.name,
        help=command.summary,
        description=f"{command.summary}\n\n{command.description.strip()}",
        epilog=command.epilog(),
        formatter_class=_Formatter,
        usage=f"sci {command.usage_hint}" if command.usage_hint else None,
    )
    positional = [a for a in command.args if a.positional]
    optional = [a for a in command.args if not a.positional]
    for spec in positional:
        parser.add_argument(*spec.flags, help=_help_text(spec), **spec.kwargs)
    for spec in optional:
        parser.add_argument(*spec.flags, help=_help_text(spec), **spec.kwargs)
    common = parser.add_argument_group("common options")
    common.add_argument("--json", action="store_true", help="print a machine-readable JSON result")
    if command.timeout:
        common.add_argument(*TIMEOUT_ARG.flags, help=_help_text(TIMEOUT_ARG), **TIMEOUT_ARG.kwargs)
    parser.set_defaults(handler=command.run, command_name=command.name)
    return parser
