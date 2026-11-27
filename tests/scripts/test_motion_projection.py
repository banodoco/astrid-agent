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


@pytest.fixture
def direct_source(tmp_path, monkeypatch):
    """Use complete existing pack fixture bytes and real immutable Git objects."""
    import shutil
    from types import SimpleNamespace

    repo = tmp_path / "poms"
    source = repo / "astrid_motion"
    shutil.copytree(projection.ROOT / "tests/fixtures/packs/astrid_motion", source)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args]).strip().decode()

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    digest = projection.pack_digest(source)
    record = {
        "schema": "astrid-motion-release-source/v1",
        "pack_id": "astrid_motion",
        "repo": "https://github.com/banodoco/poms_packs",
        "upstream_commit": git("rev-parse", "HEAD"),
        "upstream_subpath": "astrid_motion",
        "upstream_tree": git("rev-parse", "HEAD:astrid_motion"),
        "digest_rule": "pack-tree-v1",
        "upstream_digest": digest,
        "tree_digest": digest,
        "file_count": len([p for p in source.rglob("*") if p.is_file()]),
        "transform": "identity",
    }
    resource = tmp_path / "agent-resources" / "astrid_motion_release_source.json"
    resource.parent.mkdir()
    resource.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(projection, "RELEASE_SOURCE_PATH", resource)
    return SimpleNamespace(repo=repo, source=source, record=record, resource=resource, git=git)


def test_direct_profile_exports_exact_git_bytes_without_legacy_transform(direct_source, tmp_path):
    source = direct_source.source
    before = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    # A dirty working tree cannot replace the immutable reviewed Git source.
    (source / "pack.yaml").write_text("uncommitted source", encoding="utf-8")
    output = tmp_path / "astrid_motion"
    assert projection.project(direct_source.repo, output, profile="direct-source") == direct_source.record
    after = {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    assert before == after
    assert projection.pack_digest(output) == direct_source.record["upstream_digest"]
    assert not (output / "PIN").exists()
    assert not (output / "render-provenance.json").exists()
    assert not (output.parent / "rendering").exists()
    assert projection.profile_witness()["schema"] == "astrid-motion-projection/v1"
    assert projection.profile_witness("direct-source")["transform"] == "identity"
    with pytest.raises(FileExistsError):
        projection.project(direct_source.repo, output, profile="direct-source")
    assert before == {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()}


@pytest.mark.parametrize("changes", [
    {"upstream_commit": "0" * 40}, {"upstream_tree": "0" * 40},
    {"file_count": 1},
    {"upstream_digest": "0" * 64, "tree_digest": "0" * 64},
    {"transform": "alias"}, {"alias_only_digest": "0" * 64},
    {"repo": "https://example.com/other"}, {"upstream_subpath": "../astrid_motion"},
])
def test_direct_profile_refuses_unverified_source_without_publishing(direct_source, tmp_path, changes):
    direct_source.resource.write_text(json.dumps({**direct_source.record, **changes}), encoding="utf-8")
    output = tmp_path / "astrid_motion"
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        projection.project(direct_source.repo, output, profile="direct-source")
    assert not output.exists()
    assert not list(tmp_path.glob(".motion-projection-*"))


def test_direct_check_validates_count_and_refuses_unknown_profile(direct_source):
    projection.check_profile(direct_source.source, direct_source.record, "direct-source")
    with pytest.raises(ValueError, match="file count"):
        projection.check_profile(direct_source.source, {**direct_source.record, "file_count": 1}, "direct-source")
    with pytest.raises(ValueError, match="Unknown motion source profile"):
        projection.profile_witness("unreviewed")


def test_direct_cli_profile_uses_the_same_checked_export(direct_source, tmp_path, monkeypatch, capsys):
    import sys
    output = tmp_path / "astrid_motion"
    monkeypatch.setattr(sys, "argv", [
        "project_astrid_motion.py", "--profile", "direct-source", "--upstream-repo",
        str(direct_source.repo), "--output", str(output),
    ])
    assert projection.main() == 0
    assert json.loads(capsys.readouterr().out) == direct_source.record
    monkeypatch.setattr(sys, "argv", [
        "project_astrid_motion.py", "--profile", "direct-source", "--check", "--output", str(output),
    ])
    assert projection.main() == 0
    assert json.loads(capsys.readouterr().out) == direct_source.record
    (output / "pack.yaml").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        projection.main()
