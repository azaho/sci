"""Public catalog contract through anonymous PostgREST HTTP, and SQL grants."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import psycopg
import pytest

from sci import publication as pub

from .conftest import FIXTURES, write_folder
from .test_server import http, publish, signed_folder

H = {"Accept-Profile": "catalog_v1"}


def get(stack, path, headers=None):
    status, data, hdrs = http("GET", stack.postgrest_url + path, None, {**H, **(headers or {})})
    return status, json.loads(data) if data else None, hdrs


def _publish_set(stack, tmp_path, key):
    stack.enable_key(key)
    uids = []
    specs = [
        {"title": "Neural coding of choice", "abstract": "Spikes predict choice.", "categories": ["q-bio.NC"], "keywords": ["spike timing"]},
        {"title": "Transformers for tables", "abstract": "Attention on tabular data.", "categories": ["cs.LG", "stat.ML"],
         "citations": [{"target": "doi:10.48550/arXiv.1706.03762"}, {"target": "doi:10.48550/ARXIV.1706.03762", "relation": "uses"}]},
    ]
    for i, sci in enumerate(specs):
        f, _ = signed_folder(tmp_path, key, name=f"s{i}", sci=sci)
        assert publish(stack, f)[0] == 201
        uids.append(json.loads((f / "manifest.json").read_bytes())["uuid"])
    third = {"title": "Reproduction of neural coding", "abstract": "We reproduce it.",
             "citations": [{"target": f"sci:{uids[0]}", "relation": "reproduces", "reason": "Same data."},
                           {"target": "doi:10.48550/arxiv.1706.03762"}]}
    f, _ = signed_folder(tmp_path, key, name="s2", sci=third)
    assert publish(stack, f)[0] == 201
    uids.append(json.loads((f / "manifest.json").read_bytes())["uuid"])
    return uids


def test_anonymous_catalog_queries(catalog_stack, tmp_path, key):
    s = catalog_stack
    uids = _publish_set(s, tmp_path, key)

    status, rows, hdrs = get(s, f"/publications?uuid=eq.{uids[0]}&select=uuid,title,objects,files_tree,files,categories")
    assert status == 200 and len(rows) == 1
    assert hdrs["Content-Type"].startswith("application/json")
    row = rows[0]
    assert row["objects"]["README.md"] == pub.object_url(s.settings.archive_url, uids[0], "README.md")
    assert set(row["objects"]) == {"sci.json", "manifest.json", "manifest.json.sig", "LICENSE", "README.md", "files.zip", "headline_figure.png"}
    assert row["files_tree"]["truncated"] is False

    # Full-text, category (any position), publisher filters.
    q = urllib.parse.quote("neural")
    status, rows, _ = get(s, f"/publications?search_document=wfts(english).{q}&select=uuid&order=accepted_at.desc,uuid.desc")
    assert {r["uuid"] for r in rows} == {uids[0], uids[2]}
    status, rows, _ = get(s, "/publications?categories=cs.%7Bstat.ML%7D&select=uuid")
    assert [r["uuid"] for r in rows] == [uids[1]]
    fp = s.admin("select author_fingerprint from sci.publications limit 1")[0]["author_fingerprint"]
    status, rows, _ = get(s, f"/publications?author_fingerprint=eq.{fp}&select=uuid")
    assert len(rows) == 3

    # Keyset pagination.
    status, page1, _ = get(s, "/publications?select=uuid,accepted_at&order=accepted_at.desc,uuid.desc&limit=2")
    last = page1[-1]
    ts = urllib.parse.quote(last["accepted_at"])
    status, page2, _ = get(s, f"/publications?select=uuid&order=accepted_at.desc,uuid.desc&limit=2&or=(accepted_at.lt.{ts},and(accepted_at.eq.{ts},uuid.lt.{last['uuid']}))")
    assert len(page1) == 2 and len(page2) == 1 and page2[0]["uuid"] not in {r["uuid"] for r in page1}

    # Incoming / outgoing citations and embedding.
    status, rows, _ = get(s, f"/citations?target_key=eq.sci:{uids[0]}&select=source_uuid,relation,reason,publications(uuid,title)")
    assert rows == [{"source_uuid": uids[2], "relation": "reproduces", "reason": "Same data.",
                     "publications": {"uuid": uids[2], "title": "Reproduction of neural coding"}}]
    status, rows, _ = get(s, f"/citations?source_uuid=eq.{uids[1]}&order=position.asc")
    assert [r["target"] for r in rows] == ["doi:10.48550/arXiv.1706.03762", "doi:10.48550/ARXIV.1706.03762"]
    assert rows[0]["relation"] is None

    # Counts: repeated/equivalent targets count once per source.
    status, rows, _ = get(s, "/citation_counts?target_key=eq.doi:10.48550/arxiv.1706.03762")
    assert rows == [{"target_key": "doi:10.48550/arxiv.1706.03762", "citing_publications": 2}]
    status, rows, _ = get(s, "/citation_counts?order=citing_publications.desc,target_key.asc&limit=1")
    assert rows[0]["citing_publications"] == 2
    status, rows, _ = get(s, f"/citation_counts?target_key=eq.sci:{pub.new_uuid4()}")
    assert rows == []

    # Normalization RPC with original spelling (percent signs encoded in the query).
    t = urllib.parse.quote("doi:10.48550/arXiv.1706.03762", safe="")
    status, key, _ = get(s, f"/rpc/normalize_citation_target?target={t}")
    assert status == 200 and key == "doi:10.48550/arxiv.1706.03762"
    t = urllib.parse.quote("doi:10.1000/a%3cb", safe="")
    assert get(s, f"/rpc/normalize_citation_target?target={t}")[1] == "doi:10.1000/a%3Cb"
    status, err, _ = get(s, "/rpc/normalize_citation_target?target=" + urllib.parse.quote("https://doi.org/10.1000/1", safe=""))
    assert status == 400

    # Page cap.
    status, rows, _ = get(s, "/citations?limit=1000")
    assert status == 200 and len(rows) <= 100


def test_catalog_hides_unaccepted_and_private_data(catalog_stack, tmp_path, key):
    s = catalog_stack
    uids = _publish_set(s, tmp_path, key)
    s.admin("update sci.publications set state = 'finalizing' where uuid = %s", (uids[2],))
    status, rows, _ = get(s, "/publications?select=uuid")
    assert {r["uuid"] for r in rows} == {uids[0], uids[1]}
    status, rows, _ = get(s, f"/citations?source_uuid=eq.{uids[2]}")
    assert rows == []
    status, rows, _ = get(s, f"/citation_counts?target_key=eq.sci:{uids[0]}")
    assert rows == []
    status, rows, _ = get(s, "/citations?select=*,publications(uuid)&limit=100")
    assert all(r["publications"]["uuid"] != uids[2] for r in rows)

    # No write access, no private tables, no other profiles, no aggregates.
    for method in ("POST", "PATCH", "DELETE"):
        status, data, _ = http(method, s.postgrest_url + "/publications?uuid=eq." + uids[0], b'{"title":"x"}',
                               {**H, "Content-Profile": "catalog_v1", "Content-Type": "application/json"})
        assert status in (401, 403, 405), (method, status, data)
    assert get(s, "/identities")[0] in (404, 400)
    assert get(s, "/uploads")[0] in (404, 400)
    assert get(s, "/publications", {"Accept-Profile": "sci"})[0] in (406, 400)
    assert get(s, "/publications?select=title.count()")[0] in (400,)
    assert get(s, "/rpc/enable_key?public_key=x")[0] in (404, 400)

    # Response contains no internal columns.
    status, rows, _ = get(s, "/publications?limit=1")
    assert not {"state", "failure_reason", "upload_id", "object_prefix"} & set(rows[0])


def test_sql_grants(database):
    """Direct SQL checks for the public, publisher, and administrator roles."""
    with psycopg.connect(database["admin"], autocommit=True) as c:
        c.execute("set role anon")
        for q in ("select * from sci.identities", "select * from sci.uploads", "select failure_reason from sci.publications",
                  "insert into sci.identities values ('x','y',true)", "select * from sci.list_keys()",
                  "select sci.enable_key('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with c.transaction():
                    c.execute(q)
        c.execute("reset role")
    with psycopg.connect(database["publisher"], autocommit=True) as c:
        c.execute("select * from sci.identities")
        for q in ("update sci.identities set enabled = true", "insert into sci.identities (fingerprint, public_key) values ('a','b')",
                  "select sci.enable_key('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')", "delete from sci.publications"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with c.transaction():
                    c.execute(q)


def test_admin_helpers(database):
    from sci import keys

    k = keys.generate_private_key().public_key()
    x = keys.b64url_encode(keys.raw_public_key(k))
    with psycopg.connect(database["admin"], autocommit=True) as c:
        row = c.execute("select * from sci.enable_key(%s)", (x,)).fetchone()
        assert row == (keys.fingerprint(k), x, True)
        assert c.execute("select * from sci.enable_key(%s)", (x,)).fetchone()[2] is True
        assert c.execute("select * from sci.disable_key(%s)", (x,)).fetchone()[2] is False
        assert c.execute("select * from sci.disable_key(%s)", (x,)).fetchone()[2] is False
        assert c.execute("select count(*) from sci.list_keys()").fetchone()[0] == 1
        other = keys.b64url_encode(keys.raw_public_key(keys.generate_private_key().public_key()))
        with pytest.raises(psycopg.errors.NoDataFound):
            c.execute("select * from sci.disable_key(%s)", (other,))
        for bad in (x + "=", x[:-1], " " + x, x.replace(x[-1], "B") if x[-1] not in "B" else x.replace(x[-1], "C"), "-----BEGIN PRIVATE KEY-----"):
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                c.execute("select * from sci.enable_key(%s)", (bad,))


def test_sql_and_python_normalizers_agree(database):
    fx = json.loads((FIXTURES / "citation_targets.json").read_text())
    with psycopg.connect(database["admin"], autocommit=True) as c:
        for case in fx["valid"]:
            got = c.execute("select catalog_v1.normalize_citation_target(%s)", (case["target"],)).fetchone()[0]
            assert got == case["key"] == pub.normalize_citation_target(case["target"]), case
        for case in fx["invalid"]:
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                c.execute("select catalog_v1.normalize_citation_target(%s)", (case["target"],))
            with pytest.raises(pub.PublicationError):
                pub.normalize_citation_target(case["target"])


def test_migrations_are_idempotent_and_preserve_data(database, key):
    from server.database import migrate

    from .conftest import PUBLISHER_PASSWORD

    from sci import keys

    x = keys.b64url_encode(keys.raw_public_key(key.public_key()))
    with psycopg.connect(database["admin"], autocommit=True) as c:
        c.execute("select * from sci.enable_key(%s)", (x,))
    assert migrate(database["admin"], publisher_password=PUBLISHER_PASSWORD) == []
    with psycopg.connect(database["admin"], autocommit=True) as c:
        assert c.execute("select count(*) from sci.identities where enabled").fetchone()[0] == 1
