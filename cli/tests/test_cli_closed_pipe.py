"""A reader that closes the pipe early (`sci ... | head`) is not an error."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from clitestkit import Cli, cli  # noqa: F401 - cli is a fixture


def run_with_closed_stdout(home: Path, *args: str) -> tuple[int, str]:
    reader, writer = os.pipe()
    os.close(reader)  # every write to stdout now fails with EPIPE
    try:
        done = subprocess.run(
            [sys.executable, "-m", "pysci._cli", *args],
            env={**os.environ, "SCI_HOME": str(home)},
            stdout=writer,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    finally:
        os.close(writer)
    return done.returncode, done.stderr.decode()


def test_closed_pipe_exits_quietly(tmp_path: Path) -> None:
    for args in (("spec",), ("--help",), ("spec", "--list")):
        assert run_with_closed_stdout(tmp_path / "state", *args) == (0, "")


def test_closed_pipe_keeps_the_exit_code_of_a_failed_command(cli: Cli, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    expected = cli("check", str(empty)).code
    assert expected != 0
    code, stderr = run_with_closed_stdout(cli.home, "check", str(empty))
    assert code == expected and "Traceback" not in stderr
