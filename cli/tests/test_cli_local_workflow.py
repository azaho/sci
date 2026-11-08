from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from clitestkit import Cli, cli, write_draft  # noqa: F401 - cli is a fixture

from pysci.baseline import baseline_license_bytes


def _sign(cli: Cli, draft: Path, campaign: Path, *extra: str) -> str:
    run = cli("sign", str(draft), "--no-timestamp", "--output", str(campaign), "--json", *extra)
    assert run.code == 0, run.stdout + run.stderr
    publication_id = run.result["publication_id"]
    assert isinstance(publication_id, str)
    return publication_id


def test_end_to_end_local_workflow(cli: Cli) -> None:
    work = cli.cwd
    # keygen
    run = cli("keygen", "--json")
    assert run.code == 0
    key = cli.home / "key.pem"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert (cli.home / "key.pub.pem").exists()
    x = run.result["x"]
    assert run.result["public_key"] == {"kty": "OKP", "crv": "Ed25519", "x": x}
    # license + check
    draft = write_draft(work / "draft", license=False)
    run = cli("check", str(draft), "--json")
    assert run.code == 6
    assert run.result["baseline"]["status"] == "not_eligible"
    assert run.result["valid"] is True
    assert cli("license", str(draft)).code == 0
    assert (draft / "LICENSE").read_bytes() == baseline_license_bytes()
    run = cli("check", str(draft), "--json")
    assert run.code == 0
    assert run.result["baseline"]["status"] == "eligible"
    assert run.result["planned_zip_bytes"] > 0
    # sign --no-timestamp --output campaign
    campaign = work / "campaign"
    publication_id = _sign(cli, draft, campaign)
    digest = publication_id.removeprefix("sha256:")
    bundle = campaign / f"{digest}.zip"
    assert bundle.exists()
    # verify
    run = cli("verify", str(bundle), "--public-key", str(cli.home / "key.pub.pem"), "--json")
    assert run.code == 0
    checks = run.result["checks"]
    assert checks["bundle"]["status"] == "valid"
    assert checks["baseline"]["status"] == "eligible"
    assert checks["timestamps"]["status"] == "absent"
    assert set(checks["not_performed"]) == {"associations", "revocation"}
    assert run.result["public_key_check"]["status"] == "match"
    # search local
    run = cli("search", "neural SPIKES", "--in", str(campaign), "--json")
    assert run.code == 0
    group = run.result["groups"][0]
    assert group["kind"] == "local"
    assert [hit["id"] for hit in group["results"]] == [publication_id]
    assert group["results"][0]["paths"] == [str(bundle.resolve())]
    assert group["results"][0]["signer"]["x"] == x
    # read
    run = cli("read", str(bundle), "--json")
    assert run.code == 0
    assert run.result["metadata"]["title"] == "Neural decoding of spikes"
    assert run.result["text"]["lines"][0] == "# Result"
    assert set(run.result["verified_files"]) == {"sci.json", "README.md", "LICENSE"}
    assert run.result["checks"]["bundle"]["status"] == "not_performed"
    # publish to folder
    mirror = work / "mirror"
    run = cli("publish", str(campaign), "--to", str(mirror), "--json")
    assert run.code == 0
    assert (mirror / f"{digest}.zip").read_bytes() == bundle.read_bytes()
    run = cli("publish", str(campaign), "--to", str(mirror), "--json")
    assert run.result["results"][0]["bundle"] == "unchanged"
    # folders remembered
    run = cli("folders", "--json")
    paths = {entry["path"] for entry in run.result["folders"]}
    assert {str(campaign.resolve()), str(mirror.resolve())} <= paths


def test_sign_preserves_unchanged_and_requires_replace(cli: Cli) -> None:
    cli("keygen")
    draft = write_draft(cli.cwd / "draft")
    first = _sign(cli, draft, cli.cwd / "c")
    manifest = (draft / "manifest.json").read_bytes()
    (draft / "manifest.json.sig.tsr").write_bytes(b"")  # pretend proofs
    assert _sign(cli, draft, cli.cwd / "c") == first
    assert (draft / "manifest.json").read_bytes() == manifest
    (draft / "README.md").write_text("# Changed\n")
    run = cli("sign", str(draft), "--no-timestamp", "--json")
    assert run.code == 2 and run.json["errors"][0]["code"] == "signature_exists"
    second = _sign(cli, draft, cli.cwd / "c", "--replace")
    assert second != first
    assert not (draft / "manifest.json.sig.tsr").exists()
    assert not [p for p in draft.iterdir() if p.name.startswith(".sci-tmp-")]


def test_sign_refusals_and_draft_errors(cli: Cli) -> None:
    cli("keygen")
    draft = write_draft(cli.cwd / "draft", license=False)
    run = cli("sign", str(draft), "--no-timestamp", "--json")
    assert run.code == 6
    assert run.json["errors"][0]["code"] == "baseline_refused"
    run = cli("sign", str(draft), "--no-timestamp", "--allow-nonbaseline", "--json")
    assert run.code == 0 and run.result["baseline"] is False
    run = cli("verify", str(draft), "--json")
    assert run.code == 0 and run.result["checks"]["baseline"]["status"] == "not_eligible"

    bad = write_draft(cli.cwd / "bad")
    (bad / "sci.json").write_text(json.dumps({"title": "", "abstract": "x", "extra": 1}))
    (bad / "notes.txt").write_text("stray")
    run = cli("check", str(bad), "--json")
    assert run.code == 3
    codes = {issue["code"] for issue in run.result["issues"]}
    assert "manifest_path_location" in codes
    fields = {(i.get("file"), i.get("field")) for i in run.result["issues"]}
    assert ("sci.json", "/title") in fields
    for issue in run.json["errors"]:
        assert {"code", "message", "retryable"} <= set(issue)
    # private key material is refused
    leak = write_draft(
        cli.cwd / "leak", extra={"files/key.pem": (cli.home / "key.pem").read_bytes()}
    )
    run = cli("sign", str(leak), "--no-timestamp", "--json")
    assert run.code == 3
    assert "private_key_material" in {e["code"] for e in run.json["errors"]}
    # key inside the draft is refused
    inside = write_draft(cli.cwd / "inside")
    assert cli("keygen", "--key", str(inside.parent / "k.pem")).code == 0
    assert cli("keygen", "--key", str(inside.parent / "k.pem")).code == 2
    run = cli("sign", str(inside), "--no-timestamp", "--key", str(inside / "k.pem"), "--json")
    assert run.code == 2


def test_verify_detects_tampering_and_key_mismatch(cli: Cli) -> None:
    cli("keygen")
    draft = write_draft(cli.cwd / "draft")
    _sign(cli, draft, cli.cwd / "c")
    cli("keygen", "--key", str(cli.cwd / "other.pem"))
    run = cli("verify", str(draft), "--public-key", str(cli.cwd / "other.pub.pem"), "--json")
    assert run.code == 3 and run.json["errors"][0]["code"] == "public_key_mismatch"
    (draft / "files" / "data.csv").write_text("tampered")
    run = cli("verify", str(draft), "--json")
    assert run.code == 3
    assert run.result["checks"]["bundle"]["status"] == "invalid"
    assert run.result["checks"]["baseline"]["status"] == "not_performed"


def test_license_conflicts_and_signed_dirs(cli: Cli) -> None:
    draft = write_draft(cli.cwd / "d", license=False)
    (draft / "LICENSE").write_text("MIT\n")
    run = cli("license", str(draft), "--json")
    assert run.code == 3 and run.json["errors"][0]["code"] == "license_conflict"
    assert (draft / "LICENSE").read_text() == "MIT\n"
    (draft / "manifest.json").write_text("{}")
    assert cli("license", str(draft)).code == 2


def test_local_search_cache_and_candidates(cli: Cli) -> None:
    cli("keygen")
    campaign = cli.cwd / "campaign"
    ids = [
        _sign(cli, write_draft(cli.cwd / f"d{i}", title=f"Study {i} of decoding"), campaign)
        for i in range(3)
    ]
    (campaign / "broken.zip").write_bytes(b"not a zip")
    run = cli("search", "decoding", "--in", str(campaign), "--limit", "2", "--json")
    assert run.code == 0
    group = run.result["groups"][0]
    assert [hit["id"] for hit in group["results"]] == sorted(ids)[:2]
    assert group["invalid_candidates"][0]["path"].endswith("broken.zip")
    assert group["cache"]["verified"] == 4
    cursor = run.result["next_cursor"]
    assert cursor
    run = cli("search", "--cursor", cursor, "--json")
    assert [hit["id"] for hit in run.result["groups"][0]["results"]] == sorted(ids)[2:]
    assert run.result["groups"][0]["cache"]["reused"] == 4
    assert run.result["next_cursor"] is None
    # conflicting options with a cursor
    assert cli("search", "other", "--cursor", cursor).code == 2
    # filters
    run = cli("search", "--key-x", "nobody", "--in", str(campaign), "--json")
    assert run.result["groups"][0]["results"] == []
    run = cli("search", "--category", "arxiv", "q-bio.NC", "--in", str(campaign), "--json")
    assert len(run.result["groups"][0]["results"]) == 3
    assert cli("search", "--in", str(campaign)).code == 2  # no query, no filter
    assert cli("search", "x", "--domain", "lab.example", "--in", str(campaign)).code == 2
    # removed bundle disappears; corrupt cache is rebuilt
    victim = campaign / f"{sorted(ids)[0].removeprefix('sha256:')}.zip"
    victim.unlink()
    (cli.home / "catalog.sqlite").write_bytes(b"garbage")
    run = cli("search", "decoding", "--in", str(campaign), "--json")
    assert run.result["groups"][0]["cache"]["rebuilt"] is True
    assert len(run.result["groups"][0]["results"]) == 2
    # unavailable folder is partial, not empty
    cli("folders", "add", str(campaign))
    gone = cli.cwd / "gone"
    gone.mkdir()
    cli("folders", "add", str(gone))
    gone.rmdir()
    run = cli("search", "decoding", "--in", "@local", "--json")
    assert run.code == 5 and run.json["status"] == "partial"
    assert run.result["groups"][0]["unavailable_folders"]
    assert len(run.result["groups"][0]["results"]) == 2
    # default scope: current folder, offline
    run = Cli(home=cli.home, cwd=campaign)("search", "decoding", "--json")
    assert len(run.result["groups"][0]["results"]) == 2


def test_read_modes_and_ranges(cli: Cli) -> None:
    cli("keygen")
    readme = "".join(f"line {i}\n" for i in range(1, 301))
    draft = write_draft(cli.cwd / "draft", readme=readme)
    publication_id = _sign(cli, draft, cli.cwd / "c")
    bundle = cli.cwd / "c" / f"{publication_id.removeprefix('sha256:')}.zip"
    run = cli("read", str(bundle), "--json")
    text = run.result["text"]
    assert len(text["lines"]) == 200 and text["truncated"] and text["next_line"] == 201
    run = cli("read", str(draft), "--start-line", "295", "--lines", "10", "--json")
    assert run.result["text"]["lines"][0] == "line 295" and not run.result["text"]["truncated"]
    run = cli("read", str(bundle), "--list", "--json")
    assert {f["path"] for f in run.result["files"]} >= {"README.md", "files/data.csv"}
    run = cli("read", str(bundle), "--file", "files/fig.png")
    assert run.code == 2 and "binary_file" in run.stderr
    out = cli.cwd / "fig.png"
    run = cli("read", str(bundle), "--file", "files/fig.png", "--output", str(out), "--json")
    assert run.code == 0 and out.read_bytes() == (draft / "files" / "fig.png").read_bytes()
    assert cli("read", str(bundle), "--file", "files/fig.png", "--output", str(out)).code == 0
    out.write_bytes(b"different")
    assert cli("read", str(bundle), "--file", "files/fig.png", "--output", str(out)).code == 3
    assert cli("read", str(bundle), "--file", "files/missing").code == 3


def test_publish_inputs_dry_run_and_conflicts(cli: Cli) -> None:
    cli("keygen")
    draft = write_draft(cli.cwd / "draft")
    publication_id = _sign(cli, draft, cli.cwd / "c")
    digest = publication_id.removeprefix("sha256:")
    (draft / "manifest.json.sig.tsr").write_bytes(b"")
    target = cli.cwd / "dest"
    run = cli("publish", str(draft), "--to", str(target), "--dry-run", "--json")
    assert run.code == 0 and run.result["plan"][0]["action"] == "write" and not target.exists()
    target.mkdir()
    (target / f"{digest}.zip").write_bytes(b"other bytes")
    run = cli("publish", str(draft), str(cli.cwd / "c"), "--to", str(target), "--json")
    assert run.code == 3
    assert run.result["duplicates"]
    assert run.result["results"][0]["error"]["code"] == "destination_conflict"
    assert (target / f"{digest}.zip").read_bytes() == b"other bytes"
    for bad in (
        "https://gitlab.com/a/b",
        "https://github.com/a/b/tree/main",
        "git@example.com:a/b",
    ):
        assert cli("publish", str(draft), "--to", bad).code == 2
    assert cli("publish", str(draft)).code == 2


def test_claim_revoke_and_settings(cli: Cli) -> None:
    run = cli("keygen", "--json")
    x = run.result["x"]
    run = cli("claim", "domain", "Lab.Example.", "--output", str(cli.cwd / "claims"), "--json")
    assert run.code == 0
    assert run.result["account"] == {"type": "domain", "id": "lab.example"}
    assert (cli.cwd / "claims" / f"{x}.json").exists()
    assert run.result["hosting_locations"][0]["claim"].startswith(
        "https://lab.example/.well-known/"
    )
    run = cli("claim", "github", "Octo", "--output", str(cli.cwd / "claims"), "--json")
    assert run.result["account"]["id"] == "octo" and len(run.result["hosting_locations"]) == 2
    assert cli("claim", "domain", "1.2.3.4", "--output", str(cli.cwd / "claims")).code == 2

    run = cli("revoke", "--output", str(cli.cwd / "rev"), "--json")
    assert run.code == 0 and run.result["x"] == x and run.result["submitted"] is False
    assert cli("revoke", "--output", str(cli.cwd / "rev")).code == 0  # identical kept
    assert cli("revoke").code == 2
    signed = write_draft(cli.cwd / "pub")
    _sign(cli, signed, cli.cwd / "c")
    assert cli("revoke", "--output", str(signed / "inner")).code == 2

    assert cli("index", "--json").result["index"] is None
    run = cli("index", "https://Sciscape.io/", "--json")
    assert run.result["index"] == "https://sciscape.io"
    assert cli("index", "http://sciscape.io").code == 2
    assert cli("index", "https://sciscape.io/path").code == 2
    config = json.loads((cli.home / "config.json").read_text())
    assert config["index"] == "https://sciscape.io"
    run = cli("folders", "remove", str(cli.cwd / "c"), "--json")
    assert run.result["status"] == "removed"
    assert (cli.cwd / "c").exists()
    assert os.listdir(cli.cwd / "c")


def test_interactive_revoke_requires_confirmation(cli: Cli) -> None:
    cli("keygen")
    cli.tty = True
    cli.stdin = "no\n"
    run = cli("revoke", "--output", str(cli.cwd / "rev"))
    assert run.code == 2 and "permanent" in run.stderr
    assert not (cli.cwd / "rev").exists()
    cli.stdin = "revoke\n"
    assert cli("revoke", "--output", str(cli.cwd / "rev")).code == 0
    assert (cli.cwd / "rev" / "revocation.json").exists()


def test_display_files_must_be_baseline_pngs(cli: Cli, tmp_path: Path) -> None:
    draft = write_draft(tmp_path / "draft", license=True)
    figure = draft / "files" / "fig.png"
    metadata = json.loads((draft / "sci.json").read_text())

    good = figure.read_bytes()
    figure.write_bytes(good[:-1] + bytes([good[-1] ^ 1]))
    run = cli("check", str(draft), "--json")
    assert run.code == 0 and run.result["baseline"]["status"] == "eligible"
    assert "does not decode" in run.json["warnings"][0]

    figure.write_bytes(good)
    (draft / "files" / "fig.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    metadata["display"] = [{"path": "files/fig.jpg"}]
    (draft / "sci.json").write_text(json.dumps(metadata))
    run = cli("check", str(draft), "--json")
    assert run.code == 6 and run.json["errors"][0]["code"] == "baseline_display_file"
    assert run.result["baseline"]["reasons"][0]["file"] == "files/fig.jpg"
    assert cli("keygen", "--json").code == 0
    run = cli("sign", str(draft), "--no-timestamp", "--json")
    assert run.code == 6 and run.json["errors"][0]["code"] == "baseline_refused"
