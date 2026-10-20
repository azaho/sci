"""End-to-end: sign -> verify -> push -> pull -> mirror, against real services."""

from __future__ import annotations

import json
import urllib.request

import pytest

from sci import keys
from sci import publication as pub

from .conftest import run_cli, start_stack, write_folder


def _setup_cli(sci_home, stack, key=None):
    sci_home.mkdir(parents=True, exist_ok=True)
    code, out, err = run_cli("keygen")
    assert code == 0, err
    priv = keys.load_private_key(sci_home / "key.pem")
    stack.enable_key(priv)
    assert run_cli("server", stack.instance_url)[0] == 0
    return priv


def _read_tree(folder):
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()}


def test_sign_push_pull_preserves_bytes(stack, sci_home, tmp_path):
    _setup_cli(sci_home, stack)
    folder = write_folder(tmp_path / "result")
    code, uid, err = run_cli("sign", str(folder))
    assert code == 0, err
    uid = uid.strip()
    assert run_cli("verify", str(folder))[0] == 0

    code, out, err = run_cli("push", str(folder), "--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["uuid"] == uid and result["status"] == "accepted"
    assert result["url"] == f"{stack.instance_url}publication/?id={uid}"
    assert "Server: " + stack.instance_url in err

    # SQL holds extracted fields only; R2 holds exact originals.
    row = stack.admin("select state, title, categories, files_tree from sci.publications where uuid = %s", (uid,))[0]
    assert row["state"] == "accepted" and row["categories"] == ["q-bio.NC", "cs.LG"]
    cites = stack.admin("select position, target, target_key, relation, reason from sci.citations where source_uuid = %s order by position", (uid,))
    assert [c["target_key"] for c in cites] == ["doi:10.48550/arxiv.1706.03762", "sci:b2836a2b-6f5d-4c3f-9e7e-92b19656b627"]
    assert cites[1]["relation"] is None and cites[1]["reason"] is None

    for name in ("sci.json", "manifest.json", "manifest.json.sig", "LICENSE", "README.md", "files.zip", "headline_figure.png"):
        url = pub.object_url(stack.settings.archive_url, uid, name)
        with urllib.request.urlopen(url) as r:
            assert r.read() == (folder / name).read_bytes()
            assert r.headers["Content-Type"].startswith(pub.CONTENT_TYPES[name].split(";")[0])
    with urllib.request.urlopen(pub.object_url(stack.settings.archive_url, uid, "acceptance.json")) as r:
        rec = pub.parse_acceptance(r.read())
    assert rec.uuid == uid and rec.instance_url == stack.instance_url

    # Pull on "another machine" (no keys) and compare exact bytes.
    dest = tmp_path / "pulled"
    code, out, err = run_cli("pull", uid, "--output", str(dest))
    assert code == 0, err
    pulled = _read_tree(dest)
    assert "acceptance.json" not in pulled
    original = {k: v for k, v in _read_tree(folder).items()}
    assert pulled == original
    assert len(pulled["manifest.json.sig"]) == 64

    # Default destination ./UUID and refusal to overwrite a nonempty folder.
    code, _, err = run_cli("pull", uid, "--output", str(dest))
    assert code == 1 and "not an empty folder" in err

    # Retrying push returns the existing result without another copy.
    code, out, err = run_cli("push", str(folder), "--json")
    assert code == 0 and "Already accepted" in err
    assert stack.admin("select count(*) as n from sci.uploads where publication_uuid = %s", (uid,))[0]["n"] == 1


def test_readme_only_publication(stack, sci_home, tmp_path):
    _setup_cli(sci_home, stack)
    folder = tmp_path / "r"
    write_folder(folder, sci={"title": "Only text", "abstract": "A."}, figure=False, files={})
    code, uid, err = run_cli("sign", str(folder))
    assert code == 0, err
    code, out, err = run_cli("push", str(folder))
    assert code == 0, err
    m = json.loads((folder / "manifest.json").read_bytes())
    assert set(m["files"]) == {"sci.json", "README.md", "LICENSE"} and "files_tree" not in m
    dest = tmp_path / "p"
    assert run_cli("pull", uid.strip(), "--output", str(dest))[0] == 0
    assert _read_tree(dest) == _read_tree(folder)


def test_push_requires_server_and_whitelist(stack, sci_home, tmp_path):
    sci_home.mkdir(parents=True)
    run_cli("keygen")
    folder = write_folder(tmp_path / "r")
    run_cli("sign", str(folder))
    code, _, err = run_cli("push", str(folder))
    assert code == 1 and "sci server URL" in err
    code, _, err = run_cli("push", str(folder), "--server", stack.instance_url, "--json")
    assert code == 1
    assert json.loads(err.strip().splitlines()[-1])["error"]["code"] == "key_not_enabled"


def test_push_rejects_changed_files_without_resigning(stack, sci_home, tmp_path):
    _setup_cli(sci_home, stack)
    folder = write_folder(tmp_path / "r")
    run_cli("sign", str(folder))
    manifest = (folder / "manifest.json").read_bytes()
    (folder / "README.md").write_bytes(b"# changed\n")
    code, _, err = run_cli("push", str(folder))
    assert code == 1 and "README.md" in err
    assert (folder / "manifest.json").read_bytes() == manifest


def test_mirror_between_instances_without_private_key(database, rustfs, tmp_path, sci_home):
    """Alice signs; Bob (no key) mirrors from a lab instance to a public one."""
    import psycopg

    from .conftest import PUBLISHER_PASSWORD, admin_dsn, publisher_dsn
    from server.database import migrate

    lab, stop_lab = start_stack(database, rustfs, tmp_path / "lab")
    cluster = database["cluster"]
    with psycopg.connect(admin_dsn(cluster), autocommit=True) as c:
        c.execute('create database "mirror_target"') if not c.execute("select 1 from pg_database where datname='mirror_target'").fetchone() else None
    migrate(admin_dsn(cluster, "mirror_target"), publisher_password=PUBLISHER_PASSWORD)
    db2 = {"name": "mirror_target", "admin": admin_dsn(cluster, "mirror_target"), "publisher": publisher_dsn(cluster, "mirror_target"), "cluster": cluster}
    public, stop_public = start_stack(db2, rustfs, tmp_path / "public", subpath="")
    try:
        alice = _setup_cli(sci_home, lab)
        folders = []
        for i in range(2):
            f = write_folder(tmp_path / f"res{i}", sci={"title": f"Result {i}", "abstract": "A."})
            uid = run_cli("sign", str(f))[1].strip()
            assert run_cli("push", str(f))[0] == 0
            folders.append((f, uid))
        # Bob has no key at all.
        bob_home = tmp_path / "bob"
        import os

        os.environ["SCI_HOME"] = str(bob_home)
        sel = tmp_path / "selected"
        assert run_cli("pull", folders[0][1], "--server", lab.instance_url, "--output", str(sel))[0] == 0
        code, _, err = run_cli("push", str(sel), "--server", public.instance_url, "--json")
        assert code == 1 and "key_not_enabled" in err  # approval on lab does not grant public
        public.enable_key(alice)
        code, out, err = run_cli("push", str(sel), "--server", public.instance_url, "--json")
        assert code == 0, err
        assert json.loads(out)["uuid"] == folders[0][1]
        # Bob has no saved server: the override did not change config.
        assert run_cli("server")[1].startswith("No default server")
        # Identical signed bytes on both archives; unselected result not transferred.
        for name in ("manifest.json", "manifest.json.sig", "sci.json"):
            a = urllib.request.urlopen(pub.object_url(lab.settings.archive_url, folders[0][1], name)).read()
            b = urllib.request.urlopen(pub.object_url(public.settings.archive_url, folders[0][1], name)).read()
            assert a == b
        assert public.admin("select count(*) as n from sci.publications")[0]["n"] == 1
        # Each instance writes its own acceptance record.
        ra = pub.parse_acceptance(urllib.request.urlopen(pub.object_url(lab.settings.archive_url, folders[0][1], "acceptance.json")).read())
        rb = pub.parse_acceptance(urllib.request.urlopen(pub.object_url(public.settings.archive_url, folders[0][1], "acceptance.json")).read())
        assert ra.instance_url == lab.instance_url and rb.instance_url == public.instance_url
    finally:
        stop_lab()
        stop_public()
