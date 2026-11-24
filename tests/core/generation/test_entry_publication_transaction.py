"""RS1.10: actual-node and approval-write refusal must leave saved bytes intact."""
import json
from pathlib import Path

import pytest

from astrid.core.generation import cli, node_approvals as approvals
from vibecomfy.entries import entries_dir, import_as_entry, load_entry

REPO = "https://example.test/retained-rs1-pack.git"
PIN = "a" * 40


def _source(tmp_path, *, name="source", ref=None):
    from vibecomfy.templates import OutputSpec, ReadyMetadata, new_workflow
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate

    with new_workflow(ReadyMetadata.build(template_id="rs1-entry-fixture", capability="image")) as workflow:
        image = EmptyImage(width=16, height=16, batch_size=1, color=0)
        output = SaveImage(images=image.out("IMAGE"), filename_prefix=name)
        workflow.finalize({}, outputs=[OutputSpec(node=output)])
    if ref is not None:
        workflow.requirements.custom_node_refs.append({
            "name": "retained-rs1-pack", "slug": "retained-rs1-pack",
            "source": "git", "url": ref["repo"], "commit": ref["commit"],
        })
    return emit_bundle_with_candidate(workflow, tmp_path / (name + ".py"), Provenance.USER_CONFIRMED,
                                      None, source_format="scratchpad").python_path


def _tree(root):
    return {path.relative_to(root): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


@pytest.fixture
def entry_root(tmp_path, monkeypatch):
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("save-only opened Runtime"))
    return root


def _argv(source, *, replace=False):
    return ["workflow", str(source), "--as-entry", "mine", "--no-run",
            "--placement", "managed", "--json", *(["--replace"] if replace else [])]


def test_retained_unapproved_node_ref_does_not_replace_entry(tmp_path, entry_root, capsys):
    from astrid.core.generation import entries
    from vibecomfy.entries import entry_added_node_refs

    source = _source(tmp_path)
    original = import_as_entry(source, "mine", add=[REPO + "@" + PIN])
    directory = entry_root / "mine"
    assert any(row["commit"] == PIN for row in entry_added_node_refs(original))
    old_nodes = entries.engine_table()["comfyui"].workflow_requirements(
        [entries._load_entry_workflow(original)])["extra"]["nodes"]
    assert any(row["repo"] == REPO and row["commit"] == PIN for row in old_nodes)
    approvals.write_entry_approvals(directory, [approvals.approval_record(
        row, placement_class="local") for row in old_nodes])
    before = _tree(directory)

    # The replacement source has no such declaration. The canonical import
    # retains the old explicit addition, so actual-node admission must refuse.
    assert cli.main(_argv(source, replace=True)) == 2
    error = json.loads(capsys.readouterr().out)
    assert "no matching code approval" in json.dumps(error)
    assert load_entry(directory) == original
    assert _tree(directory) == before


@pytest.mark.parametrize("replace", [False, True], ids=["new", "replacement"])
def test_approval_write_failure_does_not_publish(tmp_path, entry_root, monkeypatch, capsys, replace):
    source = _source(tmp_path)
    directory = entry_root / "mine"
    original = import_as_entry(source, "mine") if replace else None
    if replace:
        approvals.write_entry_approvals(directory, [])
    else:
        entry_root.mkdir()
    before = _tree(entry_root)

    def refuse_write(revision, records):
        assert Path(revision, "entry.json").is_file()
        raise OSError("approval write failed")

    monkeypatch.setattr(approvals, "write_entry_approvals", refuse_write)
    assert cli.main(_argv(source, replace=replace)) == 2
    assert "approval write failed" in capsys.readouterr().out
    assert _tree(entry_root) == before
    if replace:
        assert load_entry(directory) == original
    else:
        assert not directory.exists()


def test_successful_replacement_publishes_exact_revision_approval(tmp_path, entry_root, capsys):
    from astrid.core.generation import entries

    node_a = {"repo": REPO, "commit": PIN}
    node_b = {**node_a, "commit": "b" * 40}
    source_a = _source(tmp_path, name="first", ref=node_a)
    source_b = _source(tmp_path, name="second", ref=node_b)

    def save_with_consent(source, *, replace=False):
        assert cli.main(_argv(source, replace=replace)) == 2
        warning = json.loads(capsys.readouterr().out)
        assert warning["issues"][0]["code"] == "needs_consent"
        retry = warning["fixes"][0]["instruction"]["then_argv"]
        assert cli.main(retry[2:]) == 0
        assert json.loads(capsys.readouterr().out)["saved"] is True

    save_with_consent(source_a)
    original = load_entry(entry_root / "mine")
    previous = _tree(entry_root / "mine")
    save_with_consent(source_b, replace=True)
    published = entries.find_entry("mine")
    revision = Path(published["bundle"]).parent
    assert revision != Path(original["bundle"]).parent
    actual = entries.engine_table()["comfyui"].workflow_requirements(
        [entries._load_entry_workflow(published)])["extra"]["nodes"]
    assert any(row["commit"] == node_b["commit"] for row in actual)
    records = approvals.read_entry_approvals(revision)
    assert records == [approvals.approval_record(row, placement_class="local") for row in actual]
    for path, value in previous.items():
        if path != Path("entry.json") and value is not None:
            assert (entry_root / "mine" / path).read_bytes() == value
    # The ordinary saved-entry admission must select the same revision.
    assert approvals.entry_approval_directory(published) == revision
    assert approvals.authorize_nodes(actual, placement_class="local", entry=published,
                                     retry_argv=["astrid", "generate", "image"]) == records


def _manifest(bundle, entry_id="mine"):
    return {"id": entry_id, "kind": "image", "modes": ["t2i"], "bundle": bundle,
            "needs": [], "binds": {}, "defaults": {}}


def _write_manifest(directory, bundle, entry_id="mine"):
    (directory / "entry.json").write_text(
        json.dumps(_manifest(bundle, entry_id)), encoding="utf-8")


@pytest.mark.parametrize("bundle", ["bundle", "revision-historical/bundle", "workflow.py"])
def test_legacy_saved_entry_keeps_root_approval_authority(entry_root, bundle):
    directory = entry_root / "mine"
    directory.mkdir(parents=True)
    _write_manifest(directory, bundle)
    if bundle != "workflow.py":
        (directory / bundle).mkdir(parents=True)
    node = {"repo": REPO, "commit": PIN}
    records = [approvals.approval_record(node, placement_class="local")]
    approvals.write_entry_approvals(directory, records)
    saved = dict(load_entry(directory), source="user-entry")
    assert approvals.entry_approval_directory(saved) == directory
    assert approvals.authorize_nodes([node], placement_class="local", entry=saved,
                                     retry_argv=["astrid", "generate", "image"]) == records


def test_unsaved_user_entry_keeps_per_run_consent(entry_root):
    saved = {**_manifest(str(entry_root / "source.py")), "source": "user-entry"}
    assert approvals.entry_approval_directory(saved) is None


@pytest.mark.parametrize("damage", [
    "missing-manifest", "invalid-json", "manifest-directory", "wrong-id",
    "wrong-bundle", "absolute-local-bundle", "absolute-root-bundle",
    "traversal-root-bundle", "revision-symlink", "bundle-symlink",
    "manifest-symlink", "root-manifest-symlink", "legacy-alias-to-v2",
])
def test_v2_revision_path_or_manifest_damage_never_uses_root(tmp_path, entry_root, damage):
    directory = entries_dir() / "mine"
    revision = directory / "revision-v2-fixture"
    bundle = revision / "bundle"
    bundle.mkdir(parents=True)
    _write_manifest(directory, f"{revision.name}/bundle")
    _write_manifest(revision, "bundle")
    node = {"repo": REPO, "commit": PIN}
    approvals.write_entry_approvals(directory, [
        approvals.approval_record(node, placement_class="local")])
    local_manifest = revision / "entry.json"
    root_manifest = directory / "entry.json"

    if damage == "missing-manifest":
        local_manifest.unlink()
    elif damage == "invalid-json":
        local_manifest.write_text("{broken", encoding="utf-8")
    elif damage == "manifest-directory":
        local_manifest.unlink()
        local_manifest.mkdir()
    elif damage == "wrong-id":
        _write_manifest(revision, "bundle", "other")
    elif damage == "wrong-bundle":
        _write_manifest(revision, "another")
    elif damage == "absolute-local-bundle":
        _write_manifest(revision, str(bundle))
    elif damage == "absolute-root-bundle":
        _write_manifest(directory, str(bundle))
    elif damage == "traversal-root-bundle":
        _write_manifest(directory, f"{revision.name}/../{revision.name}/bundle")
    elif damage == "legacy-alias-to-v2":
        alias = directory / "revision-historical"
        alias.symlink_to(revision, target_is_directory=True)
        _write_manifest(directory, f"{alias.name}/bundle")
    else:
        path = {"revision-symlink": revision, "bundle-symlink": bundle,
                "manifest-symlink": local_manifest, "root-manifest-symlink": root_manifest}[damage]
        is_directory = path.is_dir()
        outside = tmp_path / "detached"
        path.rename(outside)
        path.symlink_to(outside, target_is_directory=is_directory)

    selected = dict(load_entry(directory), source="user-entry")
    with pytest.raises(ValueError):
        approvals.entry_approval_directory(selected)


def test_stale_selected_entry_cannot_use_new_revision_approvals(entry_root):
    directory = entry_root / "mine"
    previous = directory / "revision-v2-first"
    current = directory / "revision-v2-second"
    for revision in (previous, current):
        (revision / "bundle").mkdir(parents=True)
        _write_manifest(revision, "bundle")
    _write_manifest(directory, f"{previous.name}/bundle")
    stale = dict(load_entry(directory), source="user-entry")
    _write_manifest(directory, f"{current.name}/bundle")
    with pytest.raises(ValueError, match="saved entry changed"):
        approvals.entry_approval_directory(stale)


@pytest.mark.parametrize("replacement", ["direct-after-root", "direct-after-revision", "deleted-sidecar"])
def test_v2_missing_sidecar_requests_consent_in_alignment_and_generation(
        tmp_path, entry_root, monkeypatch, replacement):
    from astrid.core.generation import asset_alignment as alignment, entries
    from astrid.core.generation.preflight import GenerationReadinessError

    node = {"repo": REPO, "commit": PIN}
    record = approvals.approval_record(node, placement_class="local")
    source = _source(tmp_path, ref=node)
    initial = import_as_entry(source, "mine")
    directory = entry_root / "mine"
    approvals.write_entry_approvals(directory, [record])
    initial_sidecar = (directory / approvals.APPROVALS_FILENAME).read_bytes()
    previous = None

    def attach(staged, revision):
        assert load_entry(revision) == staged
        approvals.write_entry_approvals(revision, [record])

    if replacement != "direct-after-root":
        previous = import_as_entry(source, "mine", replace=True, before_publish=attach)
        prior_revision = Path(previous["bundle"]).parent
        assert approvals.read_entry_approvals(prior_revision) == [record]
        assert approvals.entry_approval_directory(dict(previous, source="user-entry")) == prior_revision

    if replacement == "deleted-sidecar":
        selected = import_as_entry(source, "mine", replace=True, before_publish=attach)
        (Path(selected["bundle"]).parent / approvals.APPROVALS_FILENAME).unlink()
    else:
        selected = import_as_entry(source, "mine", replace=True)

    revision = Path(selected["bundle"]).parent
    assert revision.name.startswith("revision-v2-")
    assert not (revision / approvals.APPROVALS_FILENAME).exists()
    assert (directory / approvals.APPROVALS_FILENAME).read_bytes() == initial_sidecar
    assert Path(initial["bundle"]).is_dir()
    if previous is not None:
        assert approvals.read_entry_approvals(prior_revision) == [record]

    saved = entries.find_entry("mine")
    assert approvals.entry_approval_directory(saved) == revision
    with pytest.raises(GenerationReadinessError) as caught:
        approvals.authorize_nodes([node], placement_class="local", entry=saved,
                                  retry_argv=["astrid", "generate", "image"])
    assert caught.value.readiness["issues"][0]["code"] == "needs_consent"

    [manifest] = alignment.catalog_manifests("mine")
    [pack] = [pack for pack in manifest["modes"][0]["node_packs"]
              if pack["repository"] == REPO]
    assert pack["requires_code_consent"] is True
    assert pack["code_approvals"] == []
    monkeypatch.setattr(alignment, "selected_target",
                        lambda: pytest.fail("unapproved alignment selected a target"))
    before = _tree(directory)
    with pytest.raises(GenerationReadinessError) as caught:
        alignment.align(entry_id="mine", apply=True, consent=("user_env",))
    assert caught.value.readiness["issues"][0]["code"] == "needs_consent"
    assert _tree(directory) == before

    # The canonical alignment reader also recognizes a new valid local record.
    approvals.write_entry_approvals(revision, [record])
    [manifest] = alignment.catalog_manifests("mine")
    [pack] = [pack for pack in manifest["modes"][0]["node_packs"]
              if pack["repository"] == REPO]
    assert "requires_code_consent" not in pack
    assert pack["code_approvals"] == [record]

