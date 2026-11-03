"""Signed profile actions: format, server acceptance, and the CLI."""

from __future__ import annotations

import json

import pytest

from sci import actions, keys
from sci import cli
from sci import publication as pub

from .conftest import run_cli

AUDIENCE = "https://sci.example.org"


def signed(key, *, revision=0, name="Ada", bio=None, audience=AUDIENCE, **changes):
    payload = actions.profile_payload(
        signer=keys.fingerprint(key.public_key()), audience=audience, expected_revision=revision, name=name, bio=bio
    )
    payload.update(changes)
    return json.dumps(actions.sign(key, payload)).encode()


# ---------------------------------------------------------------------------
# format
# ---------------------------------------------------------------------------


def test_verify_round_trip(key):
    action = actions.verify(json.loads(signed(key, bio="Line one.\nLine two.")))
    assert action.type == "profile"
    assert action.signer == keys.fingerprint(key.public_key())
    assert action.payload["bio"] == "Line one.\nLine two."


def test_signature_covers_the_action_prefix(key):
    body = json.loads(signed(key))
    data = keys.b64url_decode(body["payload"])
    body["signatures"][0]["signature"] = keys.b64url_encode(keys.sign(key, data))
    with pytest.raises(pub.PublicationError, match="does not verify"):
        actions.verify(body)


def test_signer_must_match_public_key(key):
    other = keys.generate_private_key()
    body = json.loads(signed(key))
    body["signatures"][0]["public_key"] = keys.b64url_encode(keys.raw_public_key(other.public_key()))
    with pytest.raises(pub.PublicationError, match="does not match the signer"):
        actions.verify(body)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"name": "x" * (actions.MAX_NAME + 1)}, "maximum is 64"),
        ({"name": "Ada\nLovelace"}, "control character"),
        ({"bio": " "}, "non-whitespace"),
        ({"expected_revision": -1}, "non-negative"),
        ({"type": "link"}, "unknown action type"),
        ({"avatar": "x"}, "unknown field"),
    ],
)
def test_invalid_payloads(key, changes, message):
    with pytest.raises(pub.PublicationError, match=message):
        actions.verify(json.loads(signed(key, **changes)))


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


@pytest.fixture
def publisher(database):
    from server.app import Publisher, Settings
    from server.database import Database

    settings = Settings(
        instance_url=AUDIENCE + "/", archive_url="http://archive.invalid", database_url=database["publisher"],
        s3_endpoint="http://s3.invalid", s3_access_key_id="x", s3_secret_access_key="x", temp_bucket="t", public_bucket="p",
    )
    db = Database(database["publisher"])
    try:
        yield Publisher(settings, db, storage=None)
    finally:
        db.close()


def enable(database, key):
    import psycopg

    x = keys.b64url_encode(keys.raw_public_key(key.public_key()))
    with psycopg.connect(database["admin"], autocommit=True) as c:
        c.execute("select * from sci.enable_key(%s)", (x,))


def profile_row(database, key):
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database["admin"], autocommit=True, row_factory=dict_row) as c:
        return c.execute(
            "select name, bio, revision, (select count(*) from sci.actions) as actions from sci.profiles where fingerprint = %s",
            (keys.fingerprint(key.public_key()),),
        ).fetchone()


def test_profile_updates_follow_revisions(publisher, database, key):
    from server.app import ApiError

    enable(database, key)
    first = signed(key, name="Ada")
    assert publisher.apply_action(first) == (201, {"id": json.loads(keys.b64url_decode(json.loads(first)["payload"]))["id"],
                                                   "type": "profile", "signer": keys.fingerprint(key.public_key()), "revision": 1})
    assert publisher.apply_action(first)[0] == 200  # same ID and bytes: the existing result
    assert publisher.apply_action(signed(key, revision=1, name="Ada L.", bio="Analyst."))[0] == 201
    assert profile_row(database, key) == {"name": "Ada L.", "bio": "Analyst.", "revision": 2, "actions": 2}

    with pytest.raises(ApiError) as exc:
        publisher.apply_action(signed(key, revision=1, name="Stale"))
    assert exc.value.code == "revision_conflict"
    assert profile_row(database, key)["name"] == "Ada L."


def test_rejected_actions(publisher, database, key):
    from server.app import ApiError

    with pytest.raises(ApiError) as exc:
        publisher.apply_action(signed(key))
    assert exc.value.code == "key_not_enabled"

    enable(database, key)
    with pytest.raises(ApiError) as exc:
        publisher.apply_action(signed(key, audience="https://other.example"))
    assert exc.value.code == "wrong_audience"

    body = json.loads(signed(key))
    publisher.apply_action(json.dumps(body).encode())
    payload = json.loads(keys.b64url_decode(body["payload"]))
    with pytest.raises(ApiError) as exc:
        publisher.apply_action(signed(key, revision=1, id=payload["id"]))
    assert exc.value.code == "action_conflict"


def test_public_roles_read_profiles_only(publisher, database, key):
    import psycopg

    enable(database, key)
    publisher.apply_action(signed(key, name="Ada"))
    with psycopg.connect(database["admin"], autocommit=True) as c:
        c.execute("set role anon")
        assert c.execute("select name from catalog_v1.profiles").fetchall() == [("Ada",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select payload from sci.actions")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_sets_one_field_and_keeps_the_other(publisher, database, sci_home, tmp_path, monkeypatch):
    key_path = tmp_path / "key.pem"
    _, _, key = keys.write_new_key_pair(key_path)
    enable(database, key)
    fp = keys.fingerprint(key.public_key())

    def current(d, fingerprint):
        row = profile_row(database, key)
        return row and {"name": row["name"], "bio": row["bio"], "revision": row["revision"]}

    def post(method, url, *, data, **kw):
        status, result = publisher.apply_action(data)
        return cli.HttpResult(status, json.dumps(result).encode(), {})

    monkeypatch.setattr(cli, "fetch_descriptor", lambda server: {"publishing_api_url": "http://api.invalid", "catalog_url": "x"})
    monkeypatch.setattr(cli, "fetch_profile", current)
    monkeypatch.setattr(cli, "http_retrying", post)
    common = ("--key", str(key_path), "--server", AUDIENCE)

    code, _, err = run_cli("profile", "set", "bio", "Analyst.", *common)
    assert code == 1 and "set a name first" in err
    assert run_cli("profile", "set", "name", "Ada", *common)[0] == 0
    assert run_cli("profile", "set", "bio", "Analyst.", *common)[0] == 0
    assert run_cli("profile", "set", "name", "Ada Lovelace", *common)[0] == 0
    assert profile_row(database, key) == {"name": "Ada Lovelace", "bio": "Analyst.", "revision": 3, "actions": 3}

    code, out, _ = run_cli("profile", "--json", *common)
    assert code == 0 and json.loads(out)["fingerprint"] == fp and json.loads(out)["bio"] == "Analyst."


def test_http_route(publisher, database, key):
    from starlette.testclient import TestClient

    from server.app import create_app

    enable(database, key)
    with TestClient(create_app(publisher.settings, start_worker=False)) as client:
        r = client.post("/actions", content=signed(key), headers={"Content-Type": "application/json"})
        assert r.status_code == 201 and r.json()["revision"] == 1
        r = client.post("/actions", content=b"{}", headers={"Content-Type": "application/json"})
        assert r.status_code == 400
