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
    status, rows, _ = get(s, "/rpc/search_publications?q=neur&select=uuid,author_name,accepted_at&order=accepted_at.desc,uuid.desc")
    assert status == 200 and {r["uuid"] for r in rows} == {uids[0], uids[2]} and rows[0]["author_name"] is None
    status, rows, _ = get(s, "/rpc/search_publications?q=zzz&subjects=%7Bstat.ML%7D&select=uuid")
    assert [r["uuid"] for r in rows] == [uids[1]]
    status, rows, _ = get(s, "/rpc/search_publications?q=neur&categories=cs.%7Bq-bio.NC%7D&select=uuid,categories")
    assert status == 200 and [r["uuid"] for r in rows] == [uids[0]]
    # PostgREST only filters and orders an RPC result by selected columns.
    assert get(s, "/rpc/search_publications?q=neur&select=uuid&order=accepted_at.desc")[0] == 400
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


def test_cached_catalog_totals(catalog_stack, tmp_path, key):
    s = catalog_stack
    assert get(s, "/totals")[1] == []  # No misleading zero before the first refresh.
    s.publisher.run_maintenance_once()
    empty = get(s, "/totals")[1][0]
    assert (empty["publications"], empty["citations"]) == (0, 0)

    uids = _publish_set(s, tmp_path, key)
    assert get(s, "/totals")[1] == [empty]  # Reads do not recalculate the totals.
    s.publisher.run_maintenance_once()
    assert get(s, "/totals")[1] == [empty]  # No recount within five minutes.
    s.admin("update sci.catalog_totals set updated_at = now() - interval '5 minutes'")
    s.publisher.run_maintenance_once()
    snapshot = get(s, "/totals")[1][0]
    # Four citation entries, but only three distinct source/normalized-target pairs.
    assert (snapshot["publications"], snapshot["citations"]) == (3, 3)
    assert snapshot["updated_at"] > empty["updated_at"]

    s.admin("update sci.publications set state = 'finalizing' where uuid = %s", (uids[2],))
    assert get(s, "/totals")[1] == [snapshot]
    # Refresh directly so recovery does not re-accept the publication first.
    s.admin("select sci.refresh_citation_counts()")
    totals = get(s, "/totals")[1][0]
    assert (totals["publications"], totals["citations"]) == (2, 1)

    for method in ("POST", "PATCH", "DELETE"):
        status, data, _ = http(method, s.postgrest_url + "/totals", b'{"publications":999}',
                              {**H, "Content-Profile": "catalog_v1", "Content-Type": "application/json"})
        assert status in (401, 403, 405), (method, status, data)
    assert get(s, "/totals")[1] == [totals]


def test_citation_sort_and_cited_by_publishers(catalog_stack, tmp_path, key):
    from sci import keys

    s = catalog_stack
    uids = _publish_set(s, tmp_path, key)
    other = keys.generate_private_key()
    s.enable_key(other)
    f, _ = signed_folder(tmp_path, other, name="o", sci={"title": "Other lab", "abstract": "Builds on both.",
                         "citations": [{"target": f"sci:{uids[0]}"}, {"target": f"sci:{uids[1]}"}, {"target": f"sci:{uids[1]}"}]})
    assert publish(s, f)[0] == 201
    mine, theirs = keys.fingerprint(key.public_key()), keys.fingerprint(other.public_key())

    # Stored counts appear after the maintenance refresh and match citation_counts.
    assert get(s, f"/publications?uuid=eq.{uids[0]}&select=citing_publications")[1] == [{"citing_publications": 0}]
    assert s.publisher.run_maintenance_once()["recounted"] == 2
    assert s.publisher.run_maintenance_once()["recounted"] == 0
    for column in ("citing_publications", "citing_publications_24h", "citing_publications_48h", "citing_publications_7d"):
        order = f"order={column}.desc,accepted_at.desc,uuid.desc"
        status, rows, _ = get(s, f"/publications?select=uuid,{column},accepted_at&{order}&limit=2")
        assert [(r["uuid"], r[column]) for r in rows] == [(uids[0], 2), (uids[1], 1)]
        for r in rows:
            assert get(s, f"/citation_counts?target_key=eq.sci:{r['uuid']}")[1][0]["citing_publications"] == r[column]

        # Keyset pagination in citation order.
        last = rows[-1]
        n, ts = last[column], urllib.parse.quote(last["accepted_at"])
        after = f"accepted_at.lt.{ts},and(accepted_at.eq.{ts},uuid.lt.{last['uuid']})"
        status, page2, _ = get(s, f"/publications?select=uuid,{column}&{order}&limit=10"
                                  f"&or=({column}.lt.{n},and({column}.eq.{n},or({after})))")
        assert status == 200 and len(page2) == 2 and {r[column] for r in page2} == {0}

    # Counting only selected publishers; filters and order still apply.
    rpc = "/rpc/cited_by_publishers?select=uuid,citing_publications&order=citing_publications.desc,uuid.desc&fingerprints="
    assert get(s, rpc + f"{{{theirs}}}")[1] == sorted([{"uuid": uids[0], "citing_publications": 1},
                                                      {"uuid": uids[1], "citing_publications": 1}], key=lambda r: r["uuid"], reverse=True)
    assert get(s, rpc + f"{{{mine}}}")[1] == [{"uuid": uids[0], "citing_publications": 1}]
    assert get(s, rpc + f"{{{mine},{theirs}}}&uuid=eq.{uids[0]}")[1] == [{"uuid": uids[0], "citing_publications": 2}]
    assert get(s, rpc + "{}")[1] == []
    many = ",".join(keys.fingerprint(keys.generate_private_key().public_key()) for _ in range(51))
    assert get(s, rpc + f"{{{many}}}")[0] == 400


def test_citation_windows_expire_without_new_publications(database, tmp_path, key):
    from datetime import timedelta, timezone

    from psycopg.rows import dict_row
    from server.database import insert_publication
    from sci import keys

    with psycopg.connect(database["admin"], row_factory=dict_row) as c:
        # A single transaction fixes now() for exact boundary checks.
        now = c.execute("select now() as t").fetchone()["t"].astimezone(timezone.utc)
        c.execute("select * from sci.enable_key(%s)", (keys.b64url_encode(keys.raw_public_key(key.public_key())),))

        def add(name, age, citations=(), state="accepted"):
            folder, _ = signed_folder(tmp_path, key, name=name,
                                      sci={"title": name, "abstract": "A", "citations": list(citations)})
            docs = pub.verify_folder(folder)
            return insert_publication(c, docs=docs, upload_id=None, state=state,
                                      accepted_at=now - age, object_prefix=f"publications/{docs.manifest.uuid}/")

        target = add("Old target", timedelta(days=30))["uuid"]
        citations = [{"target": f"sci:{target}"}, {"target": f"sci:{target}", "relation": "uses"},
                     {"target": "doi:10.1000/example"}]
        epsilon = timedelta(microseconds=1)
        ages = [timedelta(0), timedelta(hours=24) - epsilon, timedelta(hours=24),
                timedelta(hours=48) - epsilon, timedelta(hours=48), timedelta(hours=168) - epsilon,
                timedelta(hours=168), timedelta(hours=-1)]
        for i, age in enumerate(ages):
            add(f"Source {i}", age, citations)
        add("Not accepted", timedelta(hours=1), citations, state="finalizing")

        columns = "citing_publications, citing_publications_24h, citing_publications_48h, citing_publications_7d"
        query = f"select {columns} from catalog_v1.publications where uuid = %s"
        assert c.execute("select sci.refresh_citation_counts() as n").fetchone()["n"] == 1
        assert list(c.execute(query, (target,)).fetchone().values()) == [8, 2, 4, 6]
        assert c.execute("select sci.refresh_citation_counts() as n").fetchone()["n"] == 0
        fingerprint = keys.fingerprint(key.public_key())
        assert list(c.execute(f"select {columns} from catalog_v1.cited_by_publishers(%s)",
                              ([fingerprint],)).fetchone().values()) == [8, 2, 4, 6]
        c.execute("set local role anon")
        assert list(c.execute(query, (target,)).fetchone().values()) == [8, 2, 4, 6]
        c.execute("reset role")

        # No new citation: passage of time alone must drop the window counts.
        c.execute("update sci.publications set accepted_at = accepted_at - interval '8 days'")
        assert c.execute("select sci.refresh_citation_counts() as n").fetchone()["n"] == 1
        assert list(c.execute(query, (target,)).fetchone().values()) == [8, 0, 0, 0]
        c.execute("update sci.publications set state = 'finalizing' where uuid <> %s", (target,))
        c.execute("select sci.refresh_citation_counts()")
        assert list(c.execute(query, (target,)).fetchone().values()) == [0, 0, 0, 0]


def test_catalog_hides_unaccepted_and_private_data(catalog_stack, tmp_path, key):
    s = catalog_stack
    uids = _publish_set(s, tmp_path, key)
    assert s.admin("select sci.refresh_citation_counts() as n") == [{"n": 1}]
    s.admin("update sci.publications set state = 'finalizing' where uuid = %s", (uids[2],))
    status, rows, _ = get(s, "/publications?select=uuid")
    assert {r["uuid"] for r in rows} == {uids[0], uids[1]}
    status, rows, _ = get(s, f"/citations?source_uuid=eq.{uids[2]}")
    assert rows == []
    status, rows, _ = get(s, f"/citation_counts?target_key=eq.sci:{uids[0]}")
    assert rows == []
    s.admin("select sci.refresh_citation_counts()")
    assert s.admin("select citing_publications from sci.publications where uuid = %s", (uids[0],)) == [{"citing_publications": 0}]
    assert get(s, f"/rpc/cited_by_publishers?fingerprints={{{s.admin('select author_fingerprint from sci.publications limit 1')[0]['author_fingerprint']}}}")[1] == []
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
                  "select sci.enable_key('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')",
                  "select sci.refresh_citation_counts()", "update sci.catalog_totals set publications = 999"):
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


def test_search_publications_and_author_names(database, tmp_path, key):
    from datetime import datetime, timezone

    from psycopg.rows import dict_row
    from server.database import insert_publication
    from sci import keys

    other = keys.generate_private_key()
    unpublished = keys.generate_private_key()
    b64 = lambda k: keys.b64url_encode(keys.raw_public_key(k.public_key()))  # noqa: E731
    with psycopg.connect(database["admin"], row_factory=dict_row, autocommit=True) as c:
        for k in (key, other, unpublished):
            c.execute("select * from sci.enable_key(%s)", (b64(k),))
        assert c.execute("select label from sci.set_key_label(%s, %s)", (b64(key), "oya")).fetchone() == {"label": "oya"}
        c.execute("select * from sci.set_key_label(%s, %s)", (b64(other), "lxc-01"))
        c.execute("select * from sci.set_key_label(%s, %s)", (b64(unpublished), "hidden"))
        assert [r["label"] for r in c.execute("select label from sci.list_keys()")] .count("oya") == 1
        for bad in ("", " oya", "oya ", "x" * 41, "a\nb", "<b>"):
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("select * from sci.set_key_label(%s, %s)", (b64(key), bad))
        with pytest.raises(psycopg.errors.NoDataFound):
            c.execute("select * from sci.set_key_label(%s, 'x')", (b64(keys.generate_private_key()),))

        def add(name, signer, sci):
            folder, _ = signed_folder(tmp_path, signer, name=name, sci={"title": name, **sci})
            docs = pub.verify_folder(folder)
            return insert_publication(c, docs=docs, upload_id=None, state="accepted",
                                      accepted_at=datetime.now(timezone.utc),
                                      object_prefix=f"publications/{docs.manifest.uuid}/")["uuid"]

        neural = add("Neural coding of choice", key, {"abstract": "Spikes predict choice.", "keywords": ["spike timing"]})
        cortex = add("Cortex maps", other, {"abstract": "Neuroscience of vision.", "categories": ["q-bio.NC"]})
        tables = add("Transformers for tables", other, {"abstract": "Attention on tabular data.", "categories": ["cs.LG"]})

        c.execute("set role anon")

        def search(q, subjects=(), authors=()):
            rows = c.execute("select uuid, author_name from catalog_v1.search_publications(%s, %s, %s)",
                             (q, list(subjects), list(authors))).fetchall()
            return {str(r["uuid"]) for r in rows}

        assert search("neuro") == {str(neural), str(cortex)}              # prefix, stemmed
        assert search("NEURO cod") == {str(neural)}                       # every word must match
        assert search("transformer table") == {str(tables)}
        assert search("oya") == {str(neural)}                             # author display name
        assert search("lxc") == {str(cortex), str(tables)}
        assert search("hidden") == set()
        assert search("zzz", subjects=["cs.LG"]) == {str(tables)}         # label-matched subjects
        assert search("zzz", authors=[keys.fingerprint(key.public_key())]) == {str(neural)}
        assert search("") == search("the") == search("'&|!:*()") == set()
        assert c.execute("select count(*) as n from catalog_v1.search_publications('neuro') where categories && '{q-bio.NC}'").fetchone()["n"] == 1
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            search("x" * 301)

        names = {str(r["uuid"]): r["author_name"] for r in c.execute("select uuid, author_name from catalog_v1.publications")}
        assert names == {str(neural): "oya", str(cortex): "lxc-01", str(tables): "lxc-01"}
        # Only labels of keys with accepted publications are public, never key material.
        assert {r["name"] for r in c.execute("select name from catalog_v1.authors")} == {"oya", "lxc-01"}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select public_key from sci.identities")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select * from sci.set_key_label(%s, 'x')", (b64(key),))
        c.execute("reset role")

        c.execute("select * from sci.set_key_label(%s, null)", (b64(key),))
        assert c.execute("select author_name from catalog_v1.publications where uuid = %s", (neural,)).fetchone() == {"author_name": None}
