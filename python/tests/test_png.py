from __future__ import annotations

import json
import zipfile

import pytest
from conftest import REPO_ROOT

from pysci.png import PngStructure, display_problem, png_decode_problem

CORPUS = REPO_ROOT / "protocol" / "test-vectors" / "0.1"
# Cases that meet the baseline structure checks but are not decodable PNGs.
UNDECODABLE = {
    "bad-crc",
    "missing-idat",
    "unknown-critical",
    "bad-zlib",
    "short-pixels",
    "extra-pixels",
    "bad-filter",
    "extra-zlib",
    "unchecked-header-fields",
}


def display_cases() -> list[tuple[str, bytes, bool]]:
    catalog = json.loads((CORPUS / "catalog.json").read_bytes())
    found = []
    for case in catalog["cases"]:
        name = case["id"].removeprefix("display-png-")
        bundle = case["artifacts"].get("bundle.zip")
        if name == case["id"] or bundle is None or name in ("empty-array", "supporting-files"):
            continue
        with zipfile.ZipFile(CORPUS / bundle) as archive:
            metadata = json.loads(archive.read("sci.json"))
            if len(metadata["display"]) != 1 or not metadata["display"][0]["path"].endswith(
                "figure.png"
            ):
                continue
            data = archive.read(metadata["display"][0]["path"])
        found.append((name, data, case["expected"]["baseline"]))
    return found


CASES = display_cases()


@pytest.mark.parametrize(("name", "data", "baseline"), CASES, ids=[c[0] for c in CASES])
def test_structure_matches_corpus_at_any_chunk_size(name: str, data: bytes, baseline: bool) -> None:
    for size in (1, 7, max(len(data), 1)):
        structure = PngStructure()
        for start in range(0, len(data), size):
            structure.feed(data[start : start + size])
        assert (display_problem("files/figure.png", len(data), structure) is None) is baseline


BASELINE_CASES = [c[:2] for c in CASES if c[2]]


@pytest.mark.parametrize(("name", "data"), BASELINE_CASES, ids=[c[0] for c in BASELINE_CASES])
def test_decode_check_flags_only_undecodable_files(name: str, data: bytes) -> None:
    assert (png_decode_problem(data) is not None) is (name in UNDECODABLE)


def test_display_problem_names_extension_and_size() -> None:
    assert display_problem("files/a.jpg", 10, None) == "the extension is not .png"
    problem = display_problem("files/a.png", 2097153, None)
    assert problem is not None and "2,097,153" in problem
