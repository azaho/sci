from __future__ import annotations

from clitestkit import REPO_ROOT, Cli, cli  # noqa: F401 - cli is a fixture


def test_spec_prints_the_protocol_byte_for_byte(cli: Cli) -> None:
    run = cli("spec")
    assert run.code == 0
    assert run.stdout.encode("utf-8") == (REPO_ROOT / "protocol" / "README.md").read_bytes()


def test_spec_section_and_list(cli: Cli) -> None:
    run = cli("spec", "--section", "4.4")
    assert run.code == 0 and run.stdout.startswith("### 4.4 Citations\n")
    assert "### 5" not in run.stdout
    listing = cli("spec", "--list", "--json")
    assert listing.code == 0
    sections = listing.json["result"]["sections"]
    assert {"number": "8.1.4", "title": "Publication submission"} in sections
    data = cli("spec", "--section", "2.1", "--json").json["result"]
    assert data["section"] == "2.1" and data["text"].startswith("### 2.1 Baseline publications")
    assert len(data["sha256"]) == 64


def test_spec_rejects_unknown_sections_and_conflicts(cli: Cli) -> None:
    run = cli("spec", "--section", "9", "--json")
    assert run.code == 2 and run.json["errors"][0]["code"] == "unknown_section"
    run = cli("spec", "--section", "4", "--list", "--json")
    assert run.code == 2 and run.json["errors"][0]["code"] == "conflicting_options"


def test_check_reports_each_problem_once(cli: Cli, tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    draft = tmp_path / "draft"
    draft.mkdir()
    (draft / "sci.json").write_text('{"title": "x", "abstract": "y", "extra": 1}')
    (draft / "README.md").write_text("# x\n")
    run = cli("check", str(draft))
    assert run.code == 3
    combined = run.stdout + run.stderr
    assert combined.count("[metadata_fields]") == 1
    assert "sci check: failed" in run.stderr
