"""Per-invocation context: environment, state directory, configuration, I/O, and HTTP."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from . import __version__
from .clients.http import DEFAULT_TIMEOUT, Deadline, HttpClient, Pacer, Transport, UrllibTransport
from .config import Config, load_config, save_config, state_dir


def run_command(argv: Sequence[str], timeout: float) -> str | None:
    """Run a helper command (e.g. ``gh auth token``); return stripped stdout or None."""
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


@dataclass
class Context:
    env: Mapping[str, str]
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    transport: Transport | None = None
    sleep: Callable[[float], None] = time.sleep
    runner: Callable[[Sequence[str], float], str | None] = run_command
    cwd: Path = field(default_factory=Path.cwd)
    _config: Config | None = None
    _http: HttpClient | None = None

    @property
    def home(self) -> Path:
        return state_dir(self.env)

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config(self.home)
        return self._config

    def save_config(self) -> None:
        save_config(self.home, self.config)

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient(
                self.transport or UrllibTransport(),
                user_agent=f"pysci/{__version__}",
                pacer=Pacer(self.home / "pace", sleep=self.sleep),
                sleep=self.sleep,
            )
        return self._http

    def deadline(self, seconds: float | None) -> Deadline:
        return Deadline(DEFAULT_TIMEOUT if seconds is None else seconds)

    def interactive(self) -> bool:
        try:
            return bool(self.stdin.isatty())
        except (AttributeError, ValueError):
            return False

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        return candidate if candidate.is_absolute() else self.cwd / candidate
