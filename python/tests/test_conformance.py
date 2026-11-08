"""Run the shared conformance corpus through the JSON-lines adapter.

The library covers every kind except ``scenario`` (stateful indexer policy),
which is outside the scope of a protocol library.
"""

from __future__ import annotations

import subprocess
import sys

from conftest import REPO_ROOT

KINDS = (
    "publication",
    "claim",
    "revocation",
    "timestamp",
    "discovery",
    "submission",
    "template",
    "http",
)


def test_adapter_passes_corpus() -> None:
    runner = REPO_ROOT / "protocol" / "test-vectors" / "tools" / "run_adapter.py"
    adapter = REPO_ROOT / "python" / "tests" / "adapter.py"
    command = [sys.executable, str(runner)]
    for kind in KINDS:
        command += ["--kind", kind]
    command += ["--", sys.executable, str(adapter)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    assert completed.returncode == 0, completed.stderr[-4000:]
    assert "Adapter passed 663 cases" in completed.stdout
