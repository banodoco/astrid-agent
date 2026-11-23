"""WS-0e: user-entry alignment uses the saved bundle's node pins."""
import json
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
    return SimpleNamespace(workflow=workflow, repo=repo, revision=revision)


def test_bundle_pin_overrides_same_named_shipped_pin_and_unpinned_is_reported(user_bundle):
    [manifest] = alignment.catalog_manifests("bundle-align")
    [mode] = manifest["modes"]
    assert mode["mode"] == "workflow"
    assert mode["workflow"] is user_bundle.workflow
    assert mode["node_packs"] == [{
        "name": "ComfyUI-HunyuanVideo_Foley", "repository": user_bundle.repo,
        "revision": user_bundle.revision, "classes": ["BundleOnlyNode"],
        "requires_code_consent": True,
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
