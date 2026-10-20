"""CLI behavior that needs no server."""

from __future__ import annotations

import json

from sci import keys

from .conftest import run_cli, write_folder


def test_main_help_is_self_contained():
    code, out, err = run_cli("--help")
    assert code == 0
    for needle in ("sci server", "sci keygen", "sci sign", "sci verify", "sci push", "sci pull", "Accept-Profile: catalog_v1",
                   "apikey", "sci:<UUID>", "doi:<DOI>", "supersedes", "reproduces", "challenges", "headline_figure",
                   "CC BY 4.0", "MIT", "50 MB", "64 KiB", "sci-server.json", "categories.json", "target_key",
                   "rpc/normalize_citation_target", "no `sci search`"):
        assert needle in out, needle
    for cmd in ("server", "keygen", "sign", "verify", "push", "pull"):
        assert run_cli(cmd, "--help")[0] == 0
    assert "Mirroring" in run_cli("push", "--help")[1] and "mirroring" in run_cli("pull", "--help")[1]


def test_server_config(sci_home):
    code, out, _ = run_cli("server")
    assert code == 0 and "No default server" in out and not (sci_home / "config.json").exists()
    assert run_cli("server", "https://sci.lab.example")[1].strip() == "https://sci.lab.example"
    cfg = json.loads((sci_home / "config.json").read_text())
    cfg["key"] = "/custom/key.pem"
    (sci_home / "config.json").write_text(json.dumps(cfg))
    for bad in ("ftp://x", "not a url", "https://user:pw@x", "https://x/?q=1", "https://x/#f", "/relative"):
        code, _, err = run_cli("server", bad)
        assert code == 1 and "error" in err
    cfg = json.loads((sci_home / "config.json").read_text())
    assert cfg == {"server": "https://sci.lab.example", "key": "/custom/key.pem"}
    assert json.loads(run_cli("server", "--json")[1]) == {"server": "https://sci.lab.example"}


def test_keygen_never_overwrites(sci_home):
    code, out, _ = run_cli("keygen", "--json")
    info = json.loads(out)
    assert info["fingerprint"].startswith("ed25519:") and len(info["public_key_base64url"]) == 43
    before = (sci_home / "key.pem").read_bytes()
    code, _, err = run_cli("keygen")
    assert code == 1 and "never overwrites" in err
    assert (sci_home / "key.pem").read_bytes() == before
    code, out, _ = run_cli("keygen", "--key", str(sci_home / "lab"), "--json")
    assert json.loads(out)["public_key"].endswith("lab.pub.pem")


def test_sign_verify_cli(sci_home, tmp_path):
    run_cli("keygen")
    folder = write_folder(tmp_path / "r")
    code, out, err = run_cli("sign", str(folder), "--json")
    assert code == 0, err
    res = json.loads(out)
    code, out, _ = run_cli("verify", str(folder), "--json")
    assert json.loads(out)["uuid"] == res["uuid"]
    pem = tmp_path / "other.pub.pem"
    pem.write_bytes(keys.public_key_pem(keys.generate_private_key().public_key()))
    code, _, err = run_cli("verify", str(folder), "--public-key", str(pem))
    assert code == 1 and "trusted key" in err
    code, _, err = run_cli("verify", str(folder), "--public-key", str(sci_home / "key.pub.pem"))
    assert code == 0


def test_sign_refuses_key_inside_folder(sci_home, tmp_path):
    folder = write_folder(tmp_path / "r")
    run_cli("keygen", "--key", str(folder / "files" / "key.pem"))
    code, _, err = run_cli("sign", str(folder), "--key", str(folder / "files" / "key.pem"))
    assert code == 1 and "inside the publication folder" in err


def test_pull_rejects_bad_uuid(sci_home):
    code, _, err = run_cli("pull", "sci:b2836a2b-6f5d-4c3f-9e7e-92b19656b627", "--server", "https://x.example")
    assert code == 1 and "UUIDv4" in err
    code, _, err = run_cli("pull", "b2836a2b-6f5d-4c3f-9e7e-92b19656b627")
    assert code == 1 and "sci server URL" in err
