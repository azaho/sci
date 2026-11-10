from __future__ import annotations

import io
from pathlib import Path

import pytest
from clitestkit import REPO_ROOT, Cli, cli  # noqa: F401 - cli is a fixture

from pysci._cli import __version__
from pysci._cli.commands import COMMANDS
from pysci._cli.main import main

DESIGN_COMMANDS = {
    "search",
    "read",
    "pull",
    "check",
    "license",
    "sign",
    "verify",
    "publish",
    "submit",
    "keygen",
    "claim",
    "timestamp",
    "revoke",
    "index",
    "folders",
    "spec",
}


def _help(*argv: str) -> str:
    out = io.StringIO()
    code = main([*argv, "--help"], env={}, stdout=out, stderr=io.StringIO())
    assert code == 0
    return out.getvalue()


def test_every_design_command_is_registered() -> None:
    assert {command.name for command in COMMANDS} == DESIGN_COMMANDS


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c.name)
def test_command_help_is_complete(command: object) -> None:
    from pysci._cli.spec import Command

    assert isinstance(command, Command)
    text = _help(command.name)
    for section in ("output:", "files changed:", "network:", "exit codes:", "example"):
        assert section in text, (command.name, section)
    assert "--json" in text
    for spec in command.args:
        if spec.positional:
            assert (spec.kwargs.get("metavar") or spec.flags[0]) in text
        else:
            assert spec.flags[0] in text, (command.name, spec.flags[0])
        if spec.kwargs.get("default") not in (None, False):
            assert "default" in text
    if command.timeout:
        assert "--timeout SECONDS" in text and "default: 120" in text
    for code in command.exit_codes:
        assert f"  {code}  " in text
    assert "  0  requested operation completed" in text
    assert all(example.startswith("sci ") for example in command.examples)
    assert f"sci {command.name}" in text


def test_top_level_help_shows_workflows() -> None:
    text = _help()
    assert "local campaign (offline):" in text
    assert "remote publishing:" in text
    for name in DESIGN_COMMANDS:
        assert name in text
    assert "SCI_HOME" in text


def test_version_and_help_work_offline_without_side_effects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    out = io.StringIO()
    assert main(["--version"], env={"SCI_HOME": str(home)}, stdout=out, stderr=io.StringIO()) == 0
    assert __version__ in out.getvalue()
    _help("search")
    assert not home.exists()


def test_usage_errors_exit_2(cli: Cli) -> None:
    run = cli("sign")
    assert run.code == 2 and "required" in run.stderr
    run = cli("nonsense")
    assert run.code == 2
    run = cli("read", "x", "--list", "--file", "y", "--json")
    assert run.code == 2 and run.json["errors"][0]["code"] == "conflicting_options"
    run = cli("search", "--bogus", "--json")
    assert run.code == 2 and run.json["errors"][0]["code"] == "usage"


def test_package_metadata() -> None:
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "sci-protocol"
    assert project["scripts"]["sci"] == "pysci._cli.main:main"
    assert project["license"] == "Apache-2.0"
    assert project["requires-python"] == ">=3.11"
    assert not any(dep.startswith("pysci") for dep in project["dependencies"])


def test_top_level_help_documents_the_draft_format() -> None:
    import json

    text = _help()
    assert "write a publication" in text and "`sci spec --section 2` and `--section 4`" in text
    for field in ("title", "abstract", "categories", "keywords", "display", "citations"):
        assert f'"{field}"' in text
    for relation in ("uses", "reviews", "supports", "challenges", "supersedes"):
        assert relation in text
    # The example is valid JSON once its placeholder digest is filled in.
    lines = text.split("sci.json example:\n", 1)[1].splitlines()
    end = next(i for i, line in enumerate(lines) if line == "    }")
    example = "\n".join(lines[: end + 1]).replace("<64 hex digits>", "0" * 64)
    assert set(json.loads(example)) == {
        "title",
        "abstract",
        "categories",
        "keywords",
        "display",
        "citations",
    }
