import json
import os
import subprocess
import sys
import pytest


def cli(home, *args, expected=0):
    r = subprocess.run(
        [sys.executable, "-m", "sci.main", *map(str, args)],
        env={**os.environ, "SCI_HOME": str(home)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == expected, (r.stdout, r.stderr)
    return json.loads(r.stdout)


def test_complete_offline_workflow(tmp_path):
    home = tmp_path / "state"
    draft = tmp_path / "draft"
    draft.mkdir()
    campaign = tmp_path / "campaign"
    (draft / "sci.json").write_text(
        json.dumps({"title": "An example", "abstract": "A reproducible local publication."})
    )
    (draft / "README.md").write_text("# Experiment\n\nThe measured result was 42.\n")
    assert cli(home, "check", draft)["baseline"] is False
    assert not home.exists()
    cli(home, "keygen")
    assert (home / "key.pem").stat().st_mode & 0o777 == 0o600
    cli(home, "license", draft)
    first = cli(home, "sign", draft, "--no-timestamp", "--output", campaign)
    assert first["baseline"] is True
    original = (draft / "manifest.json").read_bytes()
    second = cli(home, "sign", draft, "--no-timestamp")
    assert first["id"] == second["id"] and original == (draft / "manifest.json").read_bytes()
    packed = next(campaign.glob("*.zip"))
    assert cli(home, "verify", packed)["id"] == first["id"]
    assert (
        cli(home, "search", "measured", "--in", campaign)["groups"][0]["results"][0]["id"]
        == first["id"][7:]
    )
    assert "42" in cli(home, "read", packed)["text"]
    (draft / "README.md").write_text("changed")
    cli(home, "verify", draft, expected=3)


def test_symlink_and_private_key_refusal(tmp_path):
    from pysci import prepare, Invalid
    from pysci.bundle import read_directory
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    draft = tmp_path / "draft"
    draft.mkdir()
    (draft / "README.md").symlink_to("/etc/hosts")
    with pytest.raises(OSError):
        read_directory(draft)
    with pytest.raises(Invalid):
        prepare({"README.md": b"-----BEGIN PRIVATE KEY-----"}, Ed25519PrivateKey.generate())


def test_help_does_not_touch_home(tmp_path):
    from sci.main import parser

    for command in parser()._subparsers._group_actions[0].choices:
        r = subprocess.run(
            [sys.executable, "-m", "sci.main", command, "--help"],
            env={**os.environ, "SCI_HOME": str(tmp_path / "absent")},
            capture_output=True,
        )
        assert r.returncode == 0
    assert not (tmp_path / "absent").exists()


def test_timestamp_failure_preserves_signed_files(tmp_path, monkeypatch):
    from sci.main import parser
    from sci.commands.local import sign
    from sci.commands import identity
    from pysci import load_bundle

    monkeypatch.setenv("SCI_HOME", str(tmp_path / "home"))
    cli(tmp_path / "home", "keygen")
    draft = tmp_path / "draft"
    draft.mkdir()
    (draft / "sci.json").write_text('{"title":"T","abstract":"A"}')
    (draft / "README.md").write_text("Body")
    cli(tmp_path / "home", "license", draft)

    def unavailable(args):
        raise OSError("TSA unavailable")

    monkeypatch.setattr(identity, "timestamp", unavailable)
    result = sign(parser().parse_args(["sign", str(draft)]))
    assert result["status"] == "partial" and result["exit_code"] == 5
    old = load_bundle(draft)
    (draft / "README.md").write_text("Updated body")
    result = sign(parser().parse_args(["sign", str(draft), "--replace", "--no-timestamp"]))
    assert result["id"] != "sha256:" + old.id
