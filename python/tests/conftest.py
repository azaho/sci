"""Shared fixtures: corpus location, deterministic keys, and small draft bundles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pysci.baseline import baseline_license_bytes

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b55"
    "0000000a4944415478da6360000000020001e527defc0000000049454e44ae426082"
)

PYTHON_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PYTHON_ROOT.parent
CORPUS = REPO_ROOT / "protocol" / "test-vectors" / "0.1"
DATA = Path(__file__).resolve().parent / "data"


def corpus_case(case_id: str) -> dict[str, object]:
    catalog = json.loads((CORPUS / "catalog.json").read_text())
    for case in catalog["cases"]:
        if case["id"] == case_id:
            return dict(case)
    raise KeyError(case_id)


def corpus_bytes(relative: str) -> bytes:
    return (CORPUS / relative).read_bytes()


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def make_draft(**overrides: bytes) -> dict[str, bytes]:
    """A small protocol-valid baseline draft; keyword names use '__' for '/'."""
    draft: dict[str, bytes] = {
        "sci.json": json.dumps(
            {
                "title": "A test publication",
                "abstract": "Synthetic abstract.",
                "display": [{"path": "files/fig.png", "caption": "Figure"}],
            }
        ).encode(),
        "README.md": b"# Hello\n\nBody text.\n",
        "LICENSE": baseline_license_bytes(),
        "files/fig.png": TINY_PNG,
        "files/data/table.csv": b"a,b\n1,2\n",
    }
    for key, value in overrides.items():
        draft[key.replace("__", "/")] = value
    return draft


@pytest.fixture
def draft() -> dict[str, bytes]:
    return make_draft()
