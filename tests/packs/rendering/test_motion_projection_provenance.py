"""RN-5b: actual selected content, independent Agent witness, unpinned folders."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrid.core.pack.identity import pack_digest, read_pin, write_pin
from astrid.core.rendering.revision_notice import revision_notice_text
from astrid.packs.rendering.rendering.renderers.remotion import pack_revisions

UPSTREAM = "ec0a9f5baef2c31bc5179087b83f044524575182"
PROJECTION = "414a6fd1241111a7efbc11f443ad80adcb1a4963f96b4575d6de4205745d67c4"


def test_shipped_agent_witness_contract():
    witness = pack_revisions._motion_projection_witness()
    assert witness is not None
    assert witness["upstream_commit"] == UPSTREAM
    assert witness["tree_digest"] == PROJECTION
    assert witness["tree_digest"] != witness["upstream_digest"]
    assert witness["digest_rule"] == "pack-tree-v1"
    assert witness["file_count"] == 64


def _stage(element):
    return {"effects": [{"source_pack_id": "astrid_motion", "element_root": str(element)}]}


@pytest.fixture
def motion(tmp_path, monkeypatch):
    # Real digest of a small independent fixture, not a mock of pack_digest.
    # The fixture witness lives under the simulated Agent package root, never
    # inside the selected collection. The actual shipped record is tested above.
    witness = dict(pack_revisions._motion_projection_witness() or {})
    assert witness["tree_digest"] == PROJECTION
    root = tmp_path / "collection" / "astrid_motion"
    element = root / "rendering/elements/effects/am-type"
    element.mkdir(parents=True)
    (root / "pack.yaml").write_text("id: astrid_motion\npack_api: '2.0'\n", encoding="utf-8")
    (element / "component.tsx").write_text("export const colour = '#25241F';\n", encoding="utf-8")
    write_pin(root / "PIN", {
        "version": 1, "source": root.as_uri(), "commit": None,
        "digest_rule": "pack-tree-v1", "digest": None, "origin": "folder",
    })
    agent = tmp_path / "agent"
    resources = agent / "resources"
    resources.mkdir(parents=True)
    witness_path = resources / "astrid_motion_projection.json"
    digest = pack_digest(root)
    witness.update(tree_digest=digest, file_count=2)
    witness_path.write_text(json.dumps(witness), encoding="utf-8")
    monkeypatch.setattr(pack_revisions, "PACKAGE_ROOT", agent)
    monkeypatch.delenv("ASTRID_INSTALL_PROGRAM_ROOT", raising=False)
    return SimpleNamespace(
        root=root, element=element, stage=_stage(element), digest=digest,
        witness=witness, witness_path=witness_path,
        editor={"astrid_motion": UPSTREAM, "astrid_motion_tree": digest},
        pin_bytes=(root / "PIN").read_bytes(),
    )


def _row(motion, editor):
    rows = pack_revisions.resolved_pack_revisions(motion.stage, editor)
    assert len(rows) == 1
    assert rows[0]["pack_id"] == "astrid_motion"
    return rows[0]


def test_selected_folder_projection_matches_by_content_without_pinning(motion):
    row = _row(motion, motion.editor)
    assert row["comparison"] == "match"
    assert row["editor_revision"] == row["resolved_revision"] == motion.digest
    assert row["upstream_revision"] == row["editor_upstream_revision"] == UPSTREAM
    assert row["derived_from"] is None
    assert row["digest_rule"] == "pack-tree-v1"
    assert row["identity_verification"] == "agent projection witness and source digest verified"
    assert (motion.root / "PIN").read_bytes() == motion.pin_bytes
    pin = read_pin(motion.root / "PIN")
    assert pin["origin"] == "folder" and pin["commit"] is None and pin["digest"] is None


def test_same_upstream_different_content_reports_both_digests_and_provenance(motion):
    other = "d" * 64
    row = _row(motion, {**motion.editor, "astrid_motion_tree": other})
    assert row["comparison"] == "mismatch"
    assert row["editor_revision"] == other
    assert row["resolved_revision"] == motion.digest
    assert row["upstream_revision"] == UPSTREAM
    warning = revision_notice_text(row)
    assert other in warning and motion.digest in warning and UPSTREAM in warning
    # Upstream is provenance: changing the editor commit cannot change a
    # comparison of the same independently witnessed content bytes.
    same = _row(motion, {**motion.editor, "astrid_motion": "f" * 40})
    assert same["comparison"] == "match"
    assert same["editor_upstream_revision"] == "f" * 40
    assert same["upstream_revision"] == UPSTREAM


def test_missing_editor_tree_is_not_a_commit_match_for_projection(motion):
    row = _row(motion, {"astrid_motion": UPSTREAM})
    assert row["comparison"] == "unknown"
    assert row["editor_revision"] is None
    assert row["editor_upstream_revision"] == UPSTREAM
    assert row["resolved_revision"] == motion.digest
    assert row["upstream_revision"] == UPSTREAM
    row = _row(motion, None)
    assert row["comparison"] == "editor revision not supplied"
    assert row["resolved_revision"] == motion.digest
    assert row["upstream_revision"] == UPSTREAM


@pytest.mark.parametrize("invalid", ["", UPSTREAM, "A" * 64, "g" * 64, "sha256:" + "a" * 64])
def test_invalid_editor_tree_never_falls_back_to_commit_comparison(motion, invalid):
    row = _row(motion, {**motion.editor, "astrid_motion_tree": invalid})
    assert row["comparison"] == "unknown"
    assert row["resolved_revision"] == motion.digest


def test_modified_selected_bytes_never_inherit_witness_upstream(motion):
    (motion.element / "component.tsx").write_text("tampered", encoding="utf-8")
    actual = pack_digest(motion.root)
    assert actual != motion.digest
    row = _row(motion, motion.editor)
    assert row["comparison"] == "mismatch"
    assert row["resolved_revision"] == actual
    assert row["editor_revision"] == motion.digest
    assert row["upstream_revision"] is None
    assert row["derived_from"] is None
    assert row["identity_verification"] == "unknown"
    # A caller matching the tampered tree still cannot claim the official
    # projection or inherit its upstream provenance.
    same = _row(motion, {**motion.editor, "astrid_motion_tree": actual})
    assert same["comparison"] == "unknown"
    assert same["upstream_revision"] is None
    assert (motion.root / "PIN").read_bytes() == motion.pin_bytes


def test_forged_pin_collection_witness_and_provenance_are_not_authority(motion):
    (motion.root / "render-provenance.json").write_text(
        json.dumps({"derived_from": UPSTREAM}), encoding="utf-8")
    actual = pack_digest(motion.root)
    # A collection-local witness is outside the selected pack's hashed files.
    (motion.root.parent / "astrid_motion_projection.json").write_text(
        json.dumps({**motion.witness, "tree_digest": actual}), encoding="utf-8")
    write_pin(motion.root / "PIN", {
        "version": 1, "source": motion.witness["repo"], "commit": UPSTREAM,
        "digest_rule": "pack-tree-v1", "digest": actual, "origin": "user",
    })
    before = (motion.root / "PIN").read_bytes()
    row = _row(motion, {**motion.editor, "astrid_motion_tree": actual})
    assert row["comparison"] == "unknown"
    assert row["identity_verification"] == "unknown"
    assert row["resolved_revision"] == actual
    assert row["derived_from"] is None and row["upstream_revision"] is None
    assert (motion.root / "PIN").read_bytes() == before


@pytest.mark.parametrize("bad_witness", ["missing", "malformed", "wrong_schema", "wrong_pack", "bad_digest", "wrong_repo"])
def test_missing_or_invalid_agent_witness_cannot_verify_matching_bytes(motion, bad_witness):
    if bad_witness == "missing":
        motion.witness_path.unlink()
    elif bad_witness == "malformed":
        motion.witness_path.write_text("{", encoding="utf-8")
    else:
        replacements = {
            "wrong_schema": {"schema": "unreviewed/v2"},
            "wrong_pack": {"pack_id": "other"},
            "bad_digest": {"tree_digest": "not-a-digest"},
            "wrong_repo": {"repo": "https://example.com/collection"},
        }
        motion.witness_path.write_text(
            json.dumps({**motion.witness, **replacements[bad_witness]}), encoding="utf-8")
    row = _row(motion, motion.editor)
    assert row["resolved_revision"] == motion.digest
    assert row["comparison"] == "unknown"
    assert row["upstream_revision"] is None
    assert row["identity_verification"] == "unknown"


def test_agent_witness_symlink_does_not_follow_collection_claim(motion):
    fake = motion.root.parent / "astrid_motion_projection.json"
    fake.write_text(json.dumps(motion.witness), encoding="utf-8")
    motion.witness_path.unlink()
    try:
        motion.witness_path.symlink_to(fake)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    row = _row(motion, motion.editor)
    assert row["comparison"] == "unknown"
    assert row["upstream_revision"] is None


def test_actual_selected_root_not_another_pack_with_same_id(motion, tmp_path):
    other = tmp_path / "other" / "astrid_motion"
    selected = other / "rendering/elements/effects/am-type"
    selected.mkdir(parents=True)
    (other / "pack.yaml").write_bytes((motion.root / "pack.yaml").read_bytes())
    (selected / "component.tsx").write_text("different selected source", encoding="utf-8")
    row = pack_revisions.resolved_pack_revisions(_stage(selected), motion.editor)[0]
    assert row["comparison"] == "mismatch"
    assert row["resolved_revision"] == pack_digest(other)
    assert row["upstream_revision"] is None
    ambiguous = {"effects": motion.stage["effects"] + _stage(selected)["effects"]}
    row = pack_revisions.resolved_pack_revisions(ambiguous, motion.editor)[0]
    assert row["comparison"] == "resolved upstream revision unavailable"
    assert row["resolved_revision"] is None and row["upstream_revision"] is None


def test_auxiliary_key_only_names_motion_pack_not_a_second_pack(motion):
    rows = pack_revisions.resolved_pack_revisions({}, {"astrid_motion_tree": motion.digest})
    assert len(rows) == 1
    assert rows[0]["pack_id"] == "astrid_motion"
    assert rows[0]["editor_revision"] == motion.digest
    assert rows[0]["resolved_revision"] is None
    assert rows[0]["comparison"] == "resolved upstream revision unavailable"


def test_non_motion_legacy_release_pin_still_uses_commit(tmp_path, monkeypatch):
    root = tmp_path / "custom"
    element = root / "effects/sample"
    element.mkdir(parents=True)
    (root / "pack.yaml").write_text("id: custom\n", encoding="utf-8")
    (element / "component.tsx").write_text("legacy", encoding="utf-8")
    digest = pack_digest(root)
    commit = "e" * 40
    repo = "https://github.com/banodoco/astrid-agent"
    write_pin(root / "PIN", {
        "version": 1, "source": repo, "commit": commit,
        "digest_rule": "pack-tree-v1", "digest": digest, "origin": "default",
    })
    monkeypatch.setattr(pack_revisions, "_installed_release_pins", lambda: {
        "custom": {"repo": repo, "commit": commit, "digest_rule": "pack-tree-v1", "digest": digest},
    })
    stage = {"effects": [{"source_pack_id": "custom", "element_root": str(element)}]}
    rows = pack_revisions.resolved_pack_revisions(stage, {
        "custom": commit, "astrid_motion_tree": PROJECTION,
    })
    assert [row["pack_id"] for row in rows] == ["astrid_motion", "custom"]
    assert rows[1]["comparison"] == "match"
    assert rows[1]["resolved_revision"] == rows[1]["upstream_revision"] == commit
    assert "digest_rule" not in rows[1]


@pytest.fixture
def direct_motion(motion):
    """Small real source tree and independent test witness, not a release pin."""
    helper = motion.element / "helper.ts"
    helper.write_text("export const helper = true;\n", encoding="utf-8")
    descriptor = motion.element / "element.yaml"
    descriptor.write_text("id: am-type\n", encoding="utf-8")
    digest = pack_digest(motion.root)
    record = {
        "schema": "astrid-motion-release-source/v1",
        "pack_id": "astrid_motion",
        "repo": "https://github.com/banodoco/poms_packs",
        "upstream_commit": "c" * 40,
        "upstream_subpath": "astrid_motion",
        "upstream_tree": "d" * 40,
        "digest_rule": "pack-tree-v1",
        "upstream_digest": digest,
        "tree_digest": digest,
        "file_count": 4,
        "transform": "identity",
    }
    path = motion.witness_path.with_name("astrid_motion_release_source.json")
    path.write_text(json.dumps(record), encoding="utf-8")
    return SimpleNamespace(
        root=motion.root, element=motion.element, stage=motion.stage,
        helper=helper, descriptor=descriptor, digest=digest, witness=record,
        witness_path=path, legacy_path=motion.witness_path,
        legacy_witness=motion.witness, pin_bytes=motion.pin_bytes,
        editor={"astrid_motion": record["upstream_commit"], "astrid_motion_tree": digest},
    )


def test_direct_source_requires_witnessed_content_and_exact_upstream(direct_motion):
    row = _row(direct_motion, direct_motion.editor)
    assert row["comparison"] == "match"
    assert row["resolved_revision"] == row["editor_revision"] == direct_motion.digest
    assert row["upstream_revision"] == row["editor_upstream_revision"] == "c" * 40
    assert row["derived_from"] is None
    assert row["identity_verification"] == "agent direct-source witness and source digest verified"
    assert (direct_motion.root / "PIN").read_bytes() == direct_motion.pin_bytes
    wrong = _row(direct_motion, {**direct_motion.editor, "astrid_motion": "f" * 40})
    assert wrong["comparison"] == "mismatch"
    assert wrong["upstream_revision"] == "c" * 40
    assert wrong["editor_upstream_revision"] == "f" * 40


@pytest.mark.parametrize("upstream", [None, "", "C" * 40, "unknown", "c" * 39, "c" * 41])
def test_direct_source_missing_or_invalid_editor_upstream_is_unknown(direct_motion, upstream):
    editor = {**direct_motion.editor, "astrid_motion": upstream}
    assert _row(direct_motion, editor)["comparison"] == "unknown"
    if upstream is None:
        editor.pop("astrid_motion")
        assert _row(direct_motion, editor)["comparison"] == "unknown"


def test_direct_source_without_editor_tree_never_falls_back_to_pin(direct_motion):
    assert _row(direct_motion, {"astrid_motion": "c" * 40})["comparison"] == "unknown"
    assert _row(direct_motion, None)["comparison"] == "editor revision not supplied"


@pytest.mark.parametrize("relative", [
    "pack.yaml", "rendering/elements/effects/am-type/component.tsx",
    "rendering/elements/effects/am-type/helper.ts",
    "rendering/elements/effects/am-type/element.yaml",
])
def test_direct_source_tampered_manifest_component_helper_or_descriptor_is_nonmatch(direct_motion, relative):
    path = direct_motion.root / relative
    path.write_bytes(path.read_bytes() + b"\n# changed source\n")
    actual = pack_digest(direct_motion.root)
    row = _row(direct_motion, direct_motion.editor)
    assert row["comparison"] == "mismatch"
    assert row["resolved_revision"] == actual != direct_motion.digest
    assert row["upstream_revision"] is None and row["derived_from"] is None
    forged = _row(direct_motion, {**direct_motion.editor, "astrid_motion_tree": actual})
    assert forged["comparison"] == "unknown"


@pytest.mark.parametrize("changes", [
    {"schema": "astrid-motion-release-source/v2"},
    {"pack_id": "other"}, {"repo": "https://example.com/pack"},
    {"upstream_subpath": "../astrid_motion"}, {"digest_rule": "other"},
    {"transform": "alias"}, {"alias_only_digest": "a" * 64},
    {"derived_from": "a" * 40}, {"upstream_commit": "a" * 39},
    {"upstream_tree": "A" * 40}, {"tree_digest": "A" * 64},
    {"upstream_digest": "a" * 64}, {"file_count": True},
    {"file_count": 4.0}, {"file_count": 0}, {"file_count": -1},
    {"file_count": 5},
])
def test_direct_source_bad_witness_is_nonmatch(direct_motion, changes):
    direct_motion.witness_path.write_text(
        json.dumps({**direct_motion.witness, **changes}), encoding="utf-8")
    row = _row(direct_motion, direct_motion.editor)
    assert row["comparison"] == "unknown"
    assert row["upstream_revision"] is None and row["derived_from"] is None
    assert row["identity_verification"] == "unknown"


@pytest.mark.parametrize("failure", ["missing", "malformed", "duplicate"])
def test_direct_source_missing_or_ambiguous_json_never_uses_collection_claim(direct_motion, failure):
    claim = direct_motion.root.parent / direct_motion.witness_path.name
    claim.write_text(json.dumps(direct_motion.witness), encoding="utf-8")
    if failure == "missing":
        direct_motion.witness_path.unlink()
    elif failure == "malformed":
        direct_motion.witness_path.write_text("{", encoding="utf-8")
    else:
        raw = json.dumps(direct_motion.witness)
        direct_motion.witness_path.write_text(
            raw[:-1] + ', "file_count": 4}', encoding="utf-8")
    write_pin(direct_motion.root / "PIN", {
        "version": 1, "source": direct_motion.witness["repo"], "commit": "c" * 40,
        "digest_rule": "pack-tree-v1", "digest": direct_motion.digest, "origin": "default",
    })
    row = _row(direct_motion, direct_motion.editor)
    assert row["comparison"] == "unknown"
    assert row["upstream_revision"] is None


def test_direct_source_witness_commit_change_does_not_relabel_editor(direct_motion):
    direct_motion.witness_path.write_text(
        json.dumps({**direct_motion.witness, "upstream_commit": "e" * 40}), encoding="utf-8")
    row = _row(direct_motion, direct_motion.editor)
    assert row["comparison"] == "mismatch"
    assert row["upstream_revision"] == "e" * 40
    assert row["editor_upstream_revision"] == "c" * 40


def test_direct_source_symlink_witness_is_refused(direct_motion):
    claim = direct_motion.root.parent / direct_motion.witness_path.name
    claim.write_bytes(direct_motion.witness_path.read_bytes())
    direct_motion.witness_path.unlink()
    try:
        direct_motion.witness_path.symlink_to(claim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    assert _row(direct_motion, direct_motion.editor)["comparison"] == "unknown"


def test_direct_and_legacy_witnesses_with_same_digest_are_ambiguous(direct_motion):
    direct_motion.legacy_path.write_text(json.dumps({
        **direct_motion.legacy_witness, "tree_digest": direct_motion.digest,
    }), encoding="utf-8")
    row = _row(direct_motion, direct_motion.editor)
    assert row["comparison"] == "unknown"
    assert row["upstream_revision"] is None


def test_direct_source_uses_actual_unambiguous_existing_selected_root(direct_motion, tmp_path):
    import shutil
    other = tmp_path / "other" / "astrid_motion"
    shutil.copytree(direct_motion.root, other)
    element = other / direct_motion.element.relative_to(direct_motion.root)
    stage = {"effects": direct_motion.stage["effects"] + _stage(element)["effects"]}
    row = pack_revisions.resolved_pack_revisions(stage, direct_motion.editor)[0]
    assert row["comparison"] == "resolved upstream revision unavailable"
    assert row["resolved_revision"] is None and row["upstream_revision"] is None
    missing = _stage(direct_motion.element / "nonexistent")
    row = pack_revisions.resolved_pack_revisions(missing, direct_motion.editor)[0]
    assert row["comparison"] == "resolved upstream revision unavailable"
    (element / "component.tsx").write_text("different root", encoding="utf-8")
    row = pack_revisions.resolved_pack_revisions(_stage(element), direct_motion.editor)[0]
    assert row["comparison"] == "mismatch"
    assert row["upstream_revision"] is None


def test_direct_source_uses_canonical_count_after_group_generates_metadata(direct_motion):
    # The real group transaction emits .build/definitions.json. It and PIN
    # retain the exclusions of pack-tree-v1; an extra source file does not.
    generated = direct_motion.root / ".build" / "definitions.json"
    generated.parent.mkdir()
    generated.write_text('{"generated": true}\n', encoding="utf-8")
    assert pack_digest(direct_motion.root) == direct_motion.digest
    assert _row(direct_motion, direct_motion.editor)["comparison"] == "match"
    (direct_motion.root / "extra-source.ts").write_text("new source\n", encoding="utf-8")
    assert _row(direct_motion, direct_motion.editor)["comparison"] == "mismatch"


def test_direct_commit_cannot_borrow_legacy_content_only_match(direct_motion, tmp_path):
    root = tmp_path / "legacy" / "astrid_motion"
    element = root / direct_motion.element.relative_to(direct_motion.root)
    element.mkdir(parents=True)
    (root / "pack.yaml").write_bytes((direct_motion.root / "pack.yaml").read_bytes())
    (element / "component.tsx").write_bytes((direct_motion.element / "component.tsx").read_bytes())
    legacy_digest = pack_digest(root)
    assert legacy_digest == direct_motion.legacy_witness["tree_digest"]
    editor = {"astrid_motion": UPSTREAM, "astrid_motion_tree": legacy_digest}
    row = pack_revisions.resolved_pack_revisions(_stage(element), editor)[0]
    assert row["comparison"] == "match"  # Genuine v1 remains usable.
    row = pack_revisions.resolved_pack_revisions(
        _stage(element), {**editor, "astrid_motion": direct_motion.witness["upstream_commit"]},
    )[0]
    assert row["comparison"] == "unknown"
    assert row["upstream_revision"] is None
