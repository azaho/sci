import json
from pathlib import Path

import psycopg
import pytest

from sci.errors import SCIError
from sci.validation import normalize_citation_target

FIXTURES = json.loads((Path(__file__).parent / "fixtures/citation_targets.json").read_text())


@pytest.mark.parametrize("target,key", FIXTURES["valid"])
def test_python_valid(target, key):
    assert normalize_citation_target(target) == key


@pytest.mark.parametrize("target", FIXTURES["invalid"])
def test_python_invalid(target):
    with pytest.raises(SCIError):
        normalize_citation_target(target)


def test_target_length_boundary():
    target = "doi:10.1000/" + "a" * (512 - len("doi:10.1000/"))
    assert normalize_citation_target(target) == target
    with pytest.raises(SCIError):
        normalize_citation_target(target + "a")


@pytest.mark.integration
def test_sql_same_fixtures(admin):
    for target, key in FIXTURES["valid"]:
        assert admin.execute("SELECT catalog_v1.normalize_citation_target(%s) AS key", (target,)).fetchone()["key"] == key
    for target in FIXTURES["invalid"]:
        with pytest.raises(psycopg.Error):
            admin.execute("SELECT catalog_v1.normalize_citation_target(%s)", (target,))
    for length in [512, 513]:
        target = "doi:10.1000/" + "a" * (length - len("doi:10.1000/"))
        if length == 512:
            assert admin.execute("SELECT catalog_v1.normalize_citation_target(%s) AS key", (target,)).fetchone()["key"] == target
        else:
            with pytest.raises(psycopg.Error):
                admin.execute("SELECT catalog_v1.normalize_citation_target(%s)", (target,))
