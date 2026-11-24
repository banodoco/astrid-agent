"""WS-0e: user-entry alignment uses the saved bundle's node pins."""
import json
import sys
from types import SimpleNamespace

import pytest

from astrid.core.generation import asset_alignment as alignment, entries
from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource


def test_align_discovers_real_saved_entry(tmp_path, monkeypatch):
    from vibecomfy.entries import import_as_entry
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.workflow_bundle import load_bundle

    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    source = tmp_path / "ui.json"
    source.write_text(json.dumps(load_bundle(workflow_from_ready("image/z_image_turbo")).materialize_ui()))
    import_as_entry(source, "my-align")
    [manifest] = alignment.catalog_manifests("my-align")
    assert manifest["id"] == "my-align"
    assert manifest["provider_only"] is False
    assert manifest["modes"]
    assert all(mode["workflow"].runtime_nodes() for mode in manifest["modes"])

    root = tmp_path / "engine"
    target = alignment.ManagedAssetTarget(root, root / "models", root / "nodes",
        sys.executable, "http://127.0.0.1:8188", "a" * 64, root / "cache.json")
    report = alignment.align(entry_id="my-align", target=target)
    assert report["status"] == "not_ready"
    assert report["applied"] is False
    assert report["unlocked_bytes"] == "unknown"
    mode = report["entries"][0]["modes"][0]
    [issue] = [row for row in mode["unlocked"]
               if row.get("name") == "z_image_turbo_bf16.safetensors"]
    assert issue["kind"] == "model"
    assert issue["code"] == "model_lock_incomplete"
    assert issue["severity"] == "error"
    assert {"sha256", "size_bytes"} <= set(issue["missing"])
    assert "--add-model DEST=URL#sha256" in issue["next_action"]
    assert "astrid generate align --entry my-align --json" in issue["next_action"]
    assert "z_image_turbo_bf16.safetensors" not in {row["name"] for row in mode["models"]}
    assert report["next_action"]
    # Exercise the actual workflow gap projection as well as the real saved
    # entry loader, inventory, pack state, and offline alignment above.
    observed = alignment.inspect_assets([manifest], target, observed={})
    assert observed["entries"][0]["modes"][0]["missing_nodes"]
    assert observed["status"] == "not_ready"
    # The apply body must stop before target verification or any asset writes.
    refused = alignment._align_selected([manifest], target, entry_id="my-align",
                                        apply=True, deadline=0)
    assert refused["status"] == "not_ready"
    assert refused["applied"] is False
    assert any(row.get("code") == "model_lock_incomplete"
               for row in refused["entries"][0]["modes"][0]["unlocked"])
    assert not root.exists()


@pytest.fixture
def user_bundle(tmp_path, monkeypatch):
    # Real entry discovery and real typed bundle lock extraction, with only the
    # workflow loading boundary faked so no novel node code or schema is needed.
    root = tmp_path / "entries"
    folder = root / "bundle-align"
    folder.mkdir(parents=True)
    (folder / "entry.json").write_text(json.dumps({
        "id": "bundle-align", "kind": "audio", "modes": [], "bundle": "workflow.py",
        "needs": [], "binds": {}, "defaults": {},
    }))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    workflow = VibeWorkflow("bundle-align", WorkflowSource("bundle-align"))
    workflow.nodes["1"] = VibeNode("1", "BundleOnlyNode")
    workflow.requirements.custom_nodes.extend(["ComfyUI-HunyuanVideo_Foley", "Unpinned"])
    repo = "https://github.com/example/fork.git"
    revision = "b" * 40
    workflow.requirements.custom_node_refs.append({
        "name": "ComfyUI-HunyuanVideo_Foley", "slug": "ComfyUI-HunyuanVideo_Foley",
        "source": "git", "url": repo, "commit": revision, "class_set": ["BundleOnlyNode"],
    })
    def load(entry):
        assert entry["source"] == "user-entry"
        assert entry["bundle"] == str(folder / "workflow.py")
        return workflow
    monkeypatch.setattr(entries, "_load_entry_workflow", load)
    return SimpleNamespace(workflow=workflow, repo=repo, revision=revision, directory=folder)


def test_bundle_pin_overrides_same_named_shipped_pin_and_unpinned_is_reported(user_bundle):
    [manifest] = alignment.catalog_manifests("bundle-align")
    [mode] = manifest["modes"]
    assert mode["mode"] == "workflow"
    assert mode["workflow"] is user_bundle.workflow
    assert mode["node_packs"] == [{
        "name": "ComfyUI-HunyuanVideo_Foley", "repository": user_bundle.repo,
        "revision": user_bundle.revision, "classes": ["BundleOnlyNode"],
        "requires_code_consent": True,
        "requirements_txt": None, "pip_no_deps": [], "code_approvals": [],
    }]
    assert mode["unlocked"] == [{
        "kind": "node_pack", "name": "Unpinned", "missing": ["locked_repository_and_sha"],
    }]


def test_unfamiliar_bundle_pack_apply_refuses_before_mutation(user_bundle, monkeypatch):
    manifests = alignment.catalog_manifests("bundle-align")
    [pack] = manifests[0]["modes"][0]["node_packs"]
    target = SimpleNamespace(cached_object_info=lambda: None)
    monkeypatch.setattr(alignment, "inspect_assets", lambda *args, **kwargs: {
        "entries": [{"modes": [{"models": [], "node_packs": [{**pack, "status": "missing"}]}]}],
    })
    with pytest.raises(alignment.AlignmentError, match="user_env consent does not authorize"):
        alignment._align_selected(manifests, target, entry_id="bundle-align", apply=True, deadline=0)


def test_approved_bundle_pack_uses_controlled_restore_after_state_changes(user_bundle, monkeypatch):
    import contextlib
    import io
    import subprocess
    from pathlib import Path
    from astrid.core.generation.node_approvals import approval_record, write_entry_approvals
    from vibecomfy import node_packs

    recipe = {"requirements_txt": "deps/requirements.txt", "pip_no_deps": ["one==1", "two==2"]}
    user_bundle.workflow.requirements.custom_node_refs[0].update(recipe)
    records = [approval_record({"repo": user_bundle.repo, "commit": user_bundle.revision, **recipe},
                               placement_class="local")]
    write_entry_approvals(user_bundle.directory, records)
    manifests = alignment.catalog_manifests("bundle-align")
    [pack] = manifests[0]["modes"][0]["node_packs"]
    root = user_bundle.directory.parent
    (root / "nodes").mkdir()
    target = alignment.ManagedAssetTarget(root, root / "models", root / "nodes", sys.executable,
        "http://127.0.0.1:8188", "a" * 64, root / "cache.json", vibecomfy_root=root)
    monkeypatch.setattr(alignment.ManagedAssetTarget, "cached_object_info", lambda self: None)
    monkeypatch.setattr(alignment.ManagedAssetTarget, "verify_installation", lambda self, **kwargs: None)
    monkeypatch.setattr(alignment.ManagedAssetTarget, "object_info", lambda self: {})
    monkeypatch.setattr(alignment.ManagedAssetTarget, "cache_object_info", lambda self, observed: None)
    monkeypatch.setattr(alignment, "inspect_assets", lambda *args, **kwargs: {
        "entries": [{"modes": [{"models": [], "node_packs": [{**pack, "status": "verified"}], "missing_nodes": []}]}],
        "disk_check": {"status": "ready"}, "status": "not_ready",
    })
    states = iter(["missing", "verified"])
    monkeypatch.setattr(alignment, "_pack_state", lambda *args: next(states))
    restored = []

    def controlled_restore(entry, **kwargs):
        restored.append((entry, kwargs))
        return node_packs.InstallResult(entry.name, "installed", entry.git_commit_sha, None)

    monkeypatch.setattr(node_packs, "restore_pack", controlled_restore)

    def target_python(argv, **kwargs):
        # Execute the production handoff body against the controlled-restore
        # spy, so a return to install_pack or a dropped recipe cannot pass.
        assert argv[:2] == [sys.executable, "-c"]
        output = io.StringIO()
        with monkeypatch.context() as child, contextlib.redirect_stdout(output):
            child.setattr(sys, "argv", ["-c", *argv[3:]])
            exec(argv[2], {"__name__": "__main__"})
        return subprocess.CompletedProcess(argv, 0, stdout=output.getvalue(), stderr="")

    monkeypatch.setattr(alignment.subprocess, "run", target_python)
    assert "requires_code_consent" not in pack
    assert pack["code_approvals"] == records
    result = alignment._align_selected(manifests, target, entry_id="bundle-align", apply=True, deadline=float("inf"))
    assert result["applied"] is True
    [(entry, options)] = restored
    assert (entry.url, entry.git_commit_sha) == (user_bundle.repo, user_bundle.revision)
    assert entry.requirements_txt == recipe["requirements_txt"] and list(entry.pip_no_deps) == recipe["pip_no_deps"]
    assert options["approvals"] == records and options["placement_class"] == "local"
    assert options["install_root"] == target.custom_nodes_root
    assert options["lockfile_path"] == Path(root / ".astrid-engine/aligned-custom-nodes.lock")


@pytest.mark.parametrize("placement,recipe", [
    ("attached", {}),
    ("local", {"pip_no_deps": ["different-package"]}),
])
def test_wrong_placement_or_recipe_approval_does_not_authorize_bundle(user_bundle, placement, recipe):
    from astrid.core.generation.node_approvals import approval_record, write_entry_approvals

    write_entry_approvals(user_bundle.directory, [
        approval_record({"repo": user_bundle.repo, "commit": user_bundle.revision, **recipe},
                        placement_class=placement),
    ])
    [manifest] = alignment.catalog_manifests("bundle-align")
    [pack] = manifest["modes"][0]["node_packs"]
    assert pack["requires_code_consent"]


def test_unapproved_verified_bundle_pack_still_refuses_apply(user_bundle, monkeypatch):
    manifests = alignment.catalog_manifests("bundle-align")
    [pack] = manifests[0]["modes"][0]["node_packs"]
    target = SimpleNamespace(cached_object_info=lambda: None)
    monkeypatch.setattr(alignment, "inspect_assets", lambda *args, **kwargs: {
        "entries": [{"modes": [{"models": [], "node_packs": [{**pack, "status": "verified"}]}]}],
    })
    with pytest.raises(alignment.AlignmentError, match="no matching shipped pin or local code approval"):
        alignment._align_selected(manifests, target, entry_id="bundle-align", apply=True, deadline=0)


def test_inspection_never_calls_unapproved_verified_pack_ready(user_bundle, tmp_path, monkeypatch):
    manifests = alignment.catalog_manifests("bundle-align")
    manifests[0]["modes"][0]["unlocked"] = []
    target = SimpleNamespace(root=tmp_path, models_root=tmp_path, url="http://127.0.0.1:8188",
                             models_lock_sha256="a" * 64)
    monkeypatch.setattr(alignment, "_pack_state", lambda *args: "verified")
    monkeypatch.setattr(alignment, "workflow_asset_gaps", lambda *args: ([], []))
    report = alignment.inspect_assets(manifests, target, observed={})
    assert report["status"] == "not_ready"
    assert "no matching shipped pin or local code approval" in report["next_action"]
    assert "astrid generate align" in report["next_action"]


def test_incomplete_declaration_does_not_relax_strict_provisioning():
    from astrid.core.generation.asset_gaps import declared_model_specs
    from astrid.core.generation.engine_contracts import ModelLockValidationError

    asset = {"name": "example.safetensors", "subdir": "checkpoints",
             "url": "https://example.invalid/model"}
    with pytest.raises(ModelLockValidationError, match="sha256, size_bytes"):
        declared_model_specs([asset])
    incomplete = []
    assert declared_model_specs([asset], incomplete=incomplete) == ()
    assert incomplete == [{"name": "example.safetensors", "subdir": "checkpoints",
                           "missing": ["sha256", "size_bytes"]}]
