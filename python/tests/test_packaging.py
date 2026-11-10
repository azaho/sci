from __future__ import annotations

from importlib.metadata import version

from conftest import PYTHON_ROOT, REPO_ROOT

import pysci


def test_package_data_and_typing_marker() -> None:
    package = PYTHON_ROOT / "pysci"
    assert (package / "py.typed").exists()
    assert (package / "data" / "sci-baseline-LICENSE.txt").read_bytes() == (
        REPO_ROOT / "protocol" / "sci-baseline-LICENSE.txt"
    ).read_bytes()
    assert pysci.__version__ == version("pysci")


def test_bundled_specification_matches_protocol_readme_and_corpus_pin() -> None:
    import json

    from pysci.specification import PROTOCOL_SPEC_SHA256, protocol_specification

    readme = (REPO_ROOT / "protocol" / "README.md").read_bytes()
    assert (PYTHON_ROOT / "pysci" / "data" / "sci-protocol.md").read_bytes() == readme
    assert protocol_specification().encode("utf-8") == readme
    catalog = json.loads(
        (REPO_ROOT / "protocol" / "test-vectors" / "0.1" / "catalog.json").read_bytes()
    )
    assert catalog["spec_sha256"] == PROTOCOL_SPEC_SHA256


def test_specification_sections() -> None:
    from pysci.specification import specification_section, specification_sections

    numbers = [number for number, _ in specification_sections()]
    assert numbers[:3] == ["1", "2", "2.1"] and "8.2.1" in numbers
    citations = specification_section("4.4")
    assert citations is not None and citations.startswith("### 4.4 Citations\n")
    assert "## 5." not in citations
    # A section ends at the next heading of the same or higher level, numbered or not.
    last = specification_section("8.2.1")
    assert last is not None and "Acknowledgements" not in last
    section_four = specification_section("4")
    assert section_four is not None and "### 4.4 Citations" in section_four
    assert specification_section("9") is None
