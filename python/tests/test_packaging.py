from __future__ import annotations

from conftest import PYTHON_ROOT, REPO_ROOT

import pysci


def test_license_copy_matches_repository_license() -> None:
    # python/LICENSE is a copy so that sdists and wheels carry the license text.
    assert (PYTHON_ROOT / "LICENSE").read_bytes() == (REPO_ROOT / "LICENSE").read_bytes()


def test_package_data_and_typing_marker() -> None:
    package = PYTHON_ROOT / "pysci"
    assert (package / "py.typed").exists()
    assert (package / "data" / "sci-baseline-LICENSE.txt").read_bytes() == (
        REPO_ROOT / "protocol" / "sci-baseline-LICENSE.txt"
    ).read_bytes()
    assert pysci.__version__ == "0.1.0"
