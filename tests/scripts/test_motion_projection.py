"""Focused tests of the RN-5b-min projection (no installer or network)."""

import hashlib
import json
import subprocess

import pytest

from scripts import project_astrid_motion as projection


def test_manifest_edits_only_the_reviewed_fields(tmp_path):
    manifest = tmp_path / "pack.yaml"
    original = b'{\n  "schema_version": 3,\n  "id": "astrid_motion",\n  "name": "Motion"\n}\n'
    manifest.write_bytes(original)
    projection._manifest(tmp_path)
    expected = original.replace(
        b'  "id": "astrid_motion",\n',
        b'  "pack_api": "2.0",\n'
        b'  "dependencies": {"python_requirements": ["Pillow"]},\n'
        b'  "id": "astrid_motion",\n',
    )
    assert manifest.read_bytes() == expected
    assert json.loads(expected)["dependencies"] == {"python_requirements": ["Pillow"]}
    with pytest.raises(ValueError, match="Unexpected original"):
        projection._manifest(tmp_path)


def test_alias_projection_is_exact_and_refuses_an_unexpected_source(tmp_path):
    for relative, old in projection.ALIASES.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"import '" + old + b"';\n")
    projection._aliases(tmp_path)
    for relative in projection.ALIASES:
        assert (tmp_path / relative).read_bytes() == b"import '@astrid-rendering/shared/element_contracts';\n"
    with pytest.raises(ValueError, match="exactly one"):
        projection._aliases(tmp_path)


def test_tree_digest_is_content_identity_and_detects_modified_bytes(tmp_path):
    (tmp_path / "pack.yaml").write_bytes(b"first\n")
    expected = hashlib.sha256(
        b"pack.yaml\0" + hashlib.sha256(b"first\n").hexdigest().encode() + b"\n"
    ).hexdigest()
    projection._check_digest(tmp_path, expected, "projection")
    (tmp_path / "pack.yaml").write_bytes(b"second\n")
    with pytest.raises(ValueError, match="projection digest mismatch"):
        projection._check_digest(tmp_path, expected, "projection")


def test_git_object_projection_refuses_wrong_tree_and_preserves_existing_output(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "astrid/packs/astrid_motion"
    source.mkdir(parents=True)
    (source / "pack.yaml").write_bytes(b'{\n  "id": "astrid_motion",\n  "name": "Motion"\n}\n')
    for relative, old in projection.ALIASES.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"import '" + old + b"';\n")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args]).strip().decode()
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD:astrid/packs/astrid_motion")
    staged = tmp_path / "expected"
    import shutil
    shutil.copytree(source, staged)
    raw = projection.pack_digest(staged)
    projection._aliases(staged)
    alias = projection.pack_digest(staged)
    projection._manifest(staged)
    final = projection.pack_digest(staged)
    record = {
        **projection.witness(),
        "upstream_commit": commit,
        "upstream_tree": tree,
        "file_count": 5,
        "upstream_digest": raw,
        "alias_only_digest": alias,
        "tree_digest": final,
    }
    monkeypatch.setattr(projection, "witness", lambda: record)
    output = tmp_path / "astrid_motion"
    assert projection.project(repo, output) == record
    assert projection.pack_digest(output) == final
    assert not (output / "PIN").exists()
    assert not (output.parent / "rendering").exists()
    (output / "user-file").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        projection.project(repo, output)
    assert (output / "user-file").read_text(encoding="utf-8") == "keep"
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    monkeypatch.setattr(projection, "witness", lambda: {**record, "upstream_tree": "0" * 40})
    with pytest.raises(ValueError, match="Unexpected upstream tree"):
        projection.project(repo, wrong / "astrid_motion")
    assert not (wrong / "astrid_motion").exists()
