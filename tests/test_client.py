import json
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from sci.cli import main
from sci.client import (
    append_url,
    discover,
    load_config,
    pull,
    push,
    resolve_server,
    set_server,
    validate_url,
)
from sci.errors import SCIError
from sci.publication import verify_publication
from server.app import create_app
from tests.conftest import enable


def test_offline_config_and_help(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sci.client.config_path", lambda: tmp_path / "config.json")
    assert main(["server", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"server": None}
    assert not (tmp_path / "config.json").exists()
    with pytest.raises(SCIError, match="sci server"):
        resolve_server()
    (tmp_path / "config.json").write_text('{"key":"/example/key.pem","other":42}')
    set_server("https://example.org/sci")
    assert load_config()["key"] == "/example/key.pem" and load_config()["other"] == 42
    assert resolve_server("https://other.example/") == "https://other.example/"
    assert load_config()["server"] == "https://example.org/sci"
    before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(SCIError):
        set_server("not a URL")
    assert (tmp_path / "config.json").read_bytes() == before
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    guide = capsys.readouterr().out
    for phrase in ["citations", "doi:<DOI>", "Accept-Profile", "CC BY 4.0", "50,000,000", "supersedes"]:
        assert phrase in guide


@pytest.mark.parametrize("url", [
    "http://example.org", "https://user:secret@example.org", "https://example.org/?q=1",
    "https://example.org/#fragment", "file:///tmp/x", "//example.org", "https://example.org/ x",
])
def test_bad_urls(url):
    with pytest.raises(SCIError):
        validate_url(url)


def test_discovery_subpath_and_no_redirected_key():
    visited = []
    descriptor = {
        "schema_version": 1, "publishing_api_url": "https://publish.example/v1",
        "catalog_url": "https://catalog.example/rest/v1", "catalog_schema": "catalog_v1",
        "archive_url": "https://archive.example/sub/publications",
        "supabase_publishable_key": "sb_publishable_test",
    }
    def handler(request):
        visited.append(str(request.url))
        assert "apikey" not in request.headers
        return httpx.Response(200, json=descriptor)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert discover(client, "https://example.org/sci") == descriptor
        assert discover(client, "https://example.org/sci/") == descriptor
    assert visited == ["https://example.org/sci/sci-server.json"] * 2
    assert append_url(descriptor["archive_url"], "uuid/manifest.json") == "https://archive.example/sub/publications/uuid/manifest.json"


@pytest.mark.integration
def test_push_pull_without_keys_catalog_or_publisher(stack, make_publication, author, tmp_path):
    publisher, admin = stack
    folder, pub = make_publication(attachments=True)
    enable(admin, pub)
    author.unlink()
    descriptor = {
        "schema_version": 1, "publishing_api_url": "http://localhost:8000",
        "catalog_url": "http://localhost:8080/catalog", "catalog_schema": "catalog_v1",
        "archive_url": publisher.settings.archive_url, "supabase_publishable_key": "sb_publishable_test",
    }
    requests = []
    with TestClient(create_app(publisher.settings, publisher.database, publisher.storage, background=False)) as api:
        def handler(request):
            requests.append((request.method, str(request.url)))
            assert "apikey" not in request.headers
            path = request.url.path
            if path.endswith("/sci-server.json"):
                return httpx.Response(200, json=descriptor)
            if request.url.port == 8000:
                response = api.request(request.method, path, content=request.read(), headers=request.headers)
                return httpx.Response(response.status_code, content=response.content, headers=response.headers)
            if request.method == "PUT":
                _, bucket, key = unquote(path).split("/", 2)
                publisher.storage.client.put_object(Bucket=bucket, Key=key, Body=request.read())
                return httpx.Response(200)
            if path.startswith("/archive/publications/"):
                key = path.removeprefix("/archive/")
                data, _ = publisher.storage.read(publisher.storage.archive, key, 50_000_000)
                return httpx.Response(200, content=data)
            pytest.fail("Unexpected request: " + str(request.url))
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            outcome = push(folder, publisher.settings.instance_url, client)
            assert outcome["status"] == "accepted"
            requests.clear()
            result = pull(pub.uuid, publisher.settings.instance_url, tmp_path / "copy", client)
            assert result["uuid"] == pub.uuid
            assert not any(":8000" in url or "/catalog" in url for _, url in requests)
            assert verify_publication(tmp_path / "copy") == pub
            assert not (tmp_path / "copy" / "acceptance.json").exists()
            for name in pub.descriptors:
                assert (folder / name).read_bytes() == (tmp_path / "copy" / name).read_bytes()
            with pytest.raises(SCIError, match="nonempty"):
                pull(pub.uuid, publisher.settings.instance_url, tmp_path / "copy", client)
            publisher.storage.client.delete_object(
                Bucket=publisher.storage.archive, Key=f"publications/{pub.uuid}/files.zip")
            with pytest.raises(Exception):
                pull(pub.uuid, publisher.settings.instance_url, tmp_path / "partial", client)
            assert not (tmp_path / "partial").exists()
            assert not list(tmp_path.glob(".sci-pull-*"))
