import copy
import hashlib
import io
import shutil
import subprocess
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image

from sci.errors import SCIError
from sci.keys import (
    decode_public,
    encode_public,
    fingerprint,
    keygen,
    load_private,
    load_public,
    raw_public,
)
from sci.preview import preview_paths
from sci.publication import sign_publication, verify_documents, verify_publication
from sci.validation import (
    JSON_LIMIT,
    LICENSE,
    compact,
    strict_json,
    validate_metadata,
    validate_png,
    validate_timestamp,
    validate_uuid,
)


def resign(publication, author, mutate, metadata=None):
    manifest = copy.deepcopy(publication.manifest)
    sci = publication.sci_bytes if metadata is None else metadata
    manifest["files"]["sci.json"] = {"sha256": hashlib.sha256(sci).hexdigest(), "bytes": len(sci)}
    mutate(manifest)
    data = compact(manifest)
    return data, sci, load_private(author).sign(data)


def test_roundtrip_exact_bytes_and_trusted_signer(make_publication, author, tmp_path):
    folder, pub = make_publication(attachments=True)
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    verified = verify_publication(folder, author.with_suffix(".pub.pem"))
    assert pub == verified
    assert before == {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    assert (folder / "LICENSE").read_bytes() == LICENSE
    assert len(pub.signature) == 64
    keygen(tmp_path / "other.pem")
    with pytest.raises(SCIError, match="trusted"):
        verify_publication(folder, tmp_path / "other.pub.pem")
    assert pub.manifest["files_tree"]["paths"] == ["code/", "data/", "data/deep/", "code/test.py"]
    with zipfile.ZipFile(folder / "files.zip") as archive:
        assert "data/deep/numbers.csv" in archive.namelist()
        assert "LICENSE" not in archive.namelist()


@pytest.mark.parametrize("name", ["manifest.json", "sci.json", "README.md", "LICENSE", "files.zip", "headline_figure.png"])
def test_changed_and_missing_files_fail(make_publication, name):
    folder, _ = make_publication(attachments=True)
    path = folder / name
    original = path.read_bytes()
    path.write_bytes(original + b"x")
    with pytest.raises(SCIError):
        verify_publication(folder)
    path.unlink()
    with pytest.raises(SCIError):
        verify_publication(folder)


@pytest.mark.parametrize("signature", [b"", b"x"*63, b"x"*65, b"x"*88, b"x"*64])
def test_bad_signatures(make_publication, signature):
    _, pub = make_publication()
    with pytest.raises(SCIError):
        verify_documents(pub.manifest_bytes, pub.sci_bytes, signature)


def test_key_identity_and_no_overwrite(author, tmp_path):
    key = load_private(author)
    public = load_public(author.with_suffix(".pub.pem"))
    raw = raw_public(public)
    assert decode_public(encode_public(raw)) == raw_public(key.public_key())
    assert fingerprint(raw) == "ed25519:" + hashlib.sha256(raw).hexdigest()
    assert author.stat().st_mode & 0o777 == 0o600
    original = author.read_bytes()
    with pytest.raises(SCIError):
        keygen(author)
    assert author.read_bytes() == original
    target = tmp_path / "existing.pub.pem"
    target.touch()
    with pytest.raises(SCIError):
        keygen(tmp_path / "existing.pem")
    assert not (tmp_path / "existing.pem").exists()
    for bad in ["a", encode_public(raw)+"=", " "+encode_public(raw), encode_public(raw)[:-1] + "B"]:
        with pytest.raises(SCIError):
            decode_public(bad)
    key_path = tmp_path / "rsa.pem"
    key_path.write_bytes(rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(SCIError):
        load_private(key_path)


def test_openssl_interoperability(make_publication, author):
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL executable is optional")
    folder, pub = make_publication()
    subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey",
                    str(author.with_suffix(".pub.pem")), "-in", str(folder/"manifest.json"),
                    "-sigfile", str(folder/"manifest.json.sig")], check=True, capture_output=True)
    result = subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(author),
                             "-in", str(folder/"manifest.json")], check=True, capture_output=True)
    assert verify_documents(pub.manifest_bytes, pub.sci_bytes, result.stdout).uuid == pub.uuid


@pytest.mark.parametrize("bad", [
    b'{"title":"A","title":"B","abstract":"x"}', b'{"title":"A","\\u0074itle":"B","abstract":"x"}',
    b'\xef\xbb\xbf{}', b'{} trailing', b'[]', b'{"title":null}', b'{"x":NaN}',
    b'{"x":Infinity}', b'{"x":1.0}', b'{"x":1e0}', b'{"title":"\\ud800"}',
    b'{"title":"\\u0000"}', b'{"title":"\\u007f"}', b'{"title":"\\u0085"}',
    b'{"title":"\\ufdd0"}', b'{"title":"\\uffff"}', b'{"x":"\xff"}',
])
def test_strict_json_rejects(bad):
    with pytest.raises(SCIError):
        strict_json(bad, "sci.json")


def test_unicode_preservation_and_whitespace():
    obj = strict_json(b'{"title":"\\ud83d\\ude00 e\\u0301","abstract":"\\u03b1\\t\\n\\r"}', "sci.json")
    validate_metadata(obj)
    assert obj["title"] == "😀 e\u0301"
    assert "é" not in obj["title"]
    validate_metadata({"title": "😀"*200, "abstract": "好"*2500})
    for field, value in [("title", "x\n"), ("title", "x\u2028"), ("title", "\u3000"),
                         ("abstract", "\t\r\n "), ("title", "😀"*201), ("abstract", "好"*2501)]:
        with pytest.raises(SCIError):
            validate_metadata({"title": "T", "abstract": "A", field: value})


@pytest.mark.parametrize("field,value", [
    ("keywords", [""]), ("keywords", ["x"*65]), ("keywords", ["x"]*21),
    ("keywords", [1]), ("categories", ["cs.LG","cs.LG"]), ("categories", ["unknown"]),
    ("categories", ["cs.LG","q-bio.NC","cs.AI","cs.CV"]),
    ("headline_figure_caption", "x"*401), ("headline_figure_caption", " "),
    ("citations", [{"target":"doi:10.1/a","relation":"other"}]),
    ("citations", [{"target":"doi:10.1/a","reason":"x"*1001}]),
    ("citations", [{"target":"doi:10.1/a","reason":"\u3000"}]),
    ("citations", [{"target":"doi:10.1/a","extra":"x"}]),
    ("unknown", "x"),
])
def test_metadata_rules(field, value):
    with pytest.raises(SCIError):
        validate_metadata({"title": "T", "abstract": "A", field: value})


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(schema_version=True), lambda m: m.update(schema_version=2),
    lambda m: m.update(uuid="00000-00001"), lambda m: m.update(extra=1),
    lambda m: m["public_key"].update(d="private"), lambda m: m["public_key"].update(crv="X25519"),
    lambda m: m["files"]["README.md"].update(bytes=True),
    lambda m: m["files"]["README.md"].update(bytes=-1),
    lambda m: m["files"]["README.md"].update(bytes=1_000_001),
    lambda m: m["files"]["README.md"].update(sha256="A"*64),
    lambda m: m["files"]["LICENSE"].update(bytes=0),
    lambda m: m["files"].pop("LICENSE"),
    lambda m: m["files"].update({"../README.md": {"sha256":"a"*64,"bytes":1}}),
    lambda m: m.update(files_tree={"paths":[], "truncated":False}),
])
def test_correctly_signed_invalid_manifest(make_publication, author, mutate):
    _, pub = make_publication()
    with pytest.raises(SCIError):
        verify_documents(*resign(pub, author, mutate))


@pytest.mark.parametrize("value", [
    "2024-02-30T00:00:00Z", "0000-01-01T00:00:00Z", "2024-01-01",
    "2024-01-01T00:00:00", "2024-01-01T00:00:60Z", "2024-01-01T24:00:00Z",
    "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00.1234567Z", " 2024-01-01T00:00:00Z",
])
def test_invalid_timestamp(value):
    with pytest.raises(SCIError):
        validate_timestamp(value)


def test_valid_old_future_timestamp_uuid():
    for value in ["0001-01-01T00:00:00Z", "2024-02-29T23:59:59.1Z", "9999-12-31T23:59:59.123456Z"]:
        validate_timestamp(value)
    validate_uuid("00000000-0000-4000-8000-000000000000")
    for value in ["00000000-0000-1000-8000-000000000000", "ABC00000-0000-4000-8000-000000000000",
                  "00000000-0000-4000-7000-000000000000"]:
        with pytest.raises(SCIError):
            validate_uuid(value)


def test_license_not_overwritten(make_publication, author):
    folder, _ = make_publication()
    (folder / "LICENSE").write_bytes(b"custom terms")
    with pytest.raises(SCIError):
        sign_publication(folder, author)
    assert (folder / "LICENSE").read_bytes() == b"custom terms"


def test_preview_bounded_million_entries():
    preview = preview_paths(f"d/{i:08d}.txt" for i in range(1_000_000, 0, -1))
    assert preview["truncated"]
    assert len(preview["paths"]) == 200
    assert preview["paths"][1] == "d/00000001.txt"
    assert len(compact(preview)) <= 8192
    assert preview_paths([]) == {"paths": [], "truncated": False}


@pytest.mark.parametrize("name", ["/abs", "../x", "a//b", "a/./b", "a\\b", "C:/file", "a//", "x\n"])
def test_unsafe_paths(name):
    with pytest.raises(SCIError):
        preview_paths([name])


def test_png_limits_animation_and_corruption():
    for dimensions in [(1601,1), (1,1601)]:
        out = io.BytesIO()
        Image.new("RGB", dimensions).save(out, format="PNG")
        with pytest.raises(SCIError):
            validate_png(out.getvalue())
    out = io.BytesIO()
    Image.new("RGB", (2,2)).save(out, format="PNG", save_all=True,
                               append_images=[Image.new("RGB", (2,2), "white")], duration=100, loop=0)
    with pytest.raises(SCIError):
        validate_png(out.getvalue())
    with pytest.raises(SCIError):
        validate_png(b"not PNG")


def test_static_png_text_metadata_and_frozen_license():
    from PIL.PngImagePlugin import PngInfo
    metadata = PngInfo()
    metadata.add_text("loop", "an ordinary text value")
    stream = io.BytesIO()
    Image.new("RGB", (2,2)).save(stream, format="PNG", pnginfo=metadata)
    validate_png(stream.getvalue())
    assert len(LICENSE) == 20921
    assert hashlib.sha256(LICENSE).hexdigest() == "221819c0f43f1eb3f86eb5c454092b4ff76856cc93485941cd2ddce2015f2c03"


def test_prepared_empty_zip_and_symlink(make_publication, author):
    folder, _ = make_publication()
    with zipfile.ZipFile(folder / "files.zip", "w"):
        pass
    pub = sign_publication(folder, author)
    assert pub.manifest["files_tree"] == {"paths": [], "truncated": False}
    (folder / "files").mkdir()
    (folder / "files" / "link").symlink_to(author)
    with pytest.raises(SCIError, match="links"):
        sign_publication(folder, author)


def test_json_combined_limit(make_publication, author):
    _, pub = make_publication()
    metadata = b" " * JSON_LIMIT + pub.sci_bytes
    with pytest.raises(SCIError, match="65,536"):
        verify_documents(*resign(pub, author, lambda m: None, metadata))
