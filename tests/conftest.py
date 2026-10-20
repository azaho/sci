import json
import os

import psycopg
import pytest
from moto import mock_aws
from PIL import Image
from psycopg.rows import dict_row

from sci.keys import keygen
from sci.publication import sign_publication
from server.database import Database
from server.migrate import migrate
from server.publishing import Publisher
from server.settings import Settings
from server.storage import Storage


@pytest.fixture
def author(tmp_path):
    private = tmp_path / "author.pem"
    keygen(private)
    return private


@pytest.fixture
def make_publication(tmp_path, author):
    counter = 0

    def make(*, attachments=False, metadata=None):
        nonlocal counter
        counter += 1
        folder = tmp_path / f"result-{counter}"
        folder.mkdir()
        value = metadata or {
            "title": "A measured result", "abstract": "A method and its limits.",
            "categories": ["cs.LG", "q-bio.NC"], "keywords": ["control", "learning"],
            "citations": [
                {"target": "doi:10.1000/TEST", "reason": "First source."},
                {"target": "doi:10.1000/%74est", "relation": "reproduces"},
                {"target": "sci:00000000-0000-4000-8000-000000000000"},
            ],
        }
        (folder / "sci.json").write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
        (folder / "README.md").write_bytes(b"# Test\n\nA result.\n\n![not rendered](https://example.org/image.png)\n")
        if attachments:
            (folder / "files" / "code").mkdir(parents=True)
            (folder / "files" / "code" / "test.py").write_text("print('test')\n")
            (folder / "files" / "data" / "deep").mkdir(parents=True)
            (folder / "files" / "data" / "deep" / "numbers.csv").write_text("x,y\n1,2\n")
            Image.new("RGB", (40, 20), "white").save(folder / "headline_figure.png")
        return folder, sign_publication(folder, author)
    return make


@pytest.fixture(scope="session")
def database_url():
    url = os.getenv("SCI_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set SCI_TEST_DATABASE_URL to a disposable PostgreSQL database")
    migrate(url)
    return url


@pytest.fixture
def admin(database_url):
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("""
            TRUNCATE sci_private.citations, sci_private.publications, sci_private.uploads,
              sci_private.identities, sci_private.rate_limits CASCADE
        """)
        conn.execute("UPDATE sci_private.instance_state SET writes_enabled=true")
        yield conn


@pytest.fixture
def settings(database_url):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(database_url)
    params["user"] = "sci_publisher"
    params["password"] = os.getenv("SCI_TEST_PUBLISHER_PASSWORD", "local-publisher-only")
    return Settings(
        database_url=make_conninfo(**params), s3_endpoint="https://s3.amazonaws.com",
        s3_public_endpoint="https://s3.amazonaws.com", s3_access_key="testing",
        s3_secret_key="testing", temporary_bucket="sci-test-temporary", archive_bucket="sci-test-archive",
        instance_url="http://localhost:8080/sci", archive_url="http://localhost:8080/archive/publications",
        region="us-east-1",
    )


@pytest.fixture
def stack(admin, settings):
    from psycopg import sql
    admin.execute(sql.SQL("ALTER ROLE sci_publisher PASSWORD {}").format(
        sql.Literal(os.getenv("SCI_TEST_PUBLISHER_PASSWORD", "local-publisher-only"))
    ))
    with mock_aws():
        storage = Storage(settings)
        storage.client.create_bucket(Bucket=storage.temporary)
        storage.client.create_bucket(Bucket=storage.archive)
        database = Database(settings.database_url)
        publisher = Publisher(database, storage, settings)
        yield publisher, admin
        database.close()


def enable(admin, publication):
    admin.execute("SELECT sci_private.enable_key(%s)", (publication.manifest["public_key"]["x"],))


def upload(publisher, folder, publication):
    _, attempt = publisher.prepare(publication)
    for name in attempt["uploads"]:
        publisher.storage.put(
            publisher.storage.temporary, f"{attempt['upload_id']}/{name}", (folder / name).read_bytes(), name,
        )
    return attempt


def accept(publisher, admin, folder, publication):
    enable(admin, publication)
    attempt = upload(publisher, folder, publication)
    return publisher.complete(publication.uuid, attempt["upload_id"], publication.manifest_sha256)
