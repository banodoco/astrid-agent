from vibecomfy import workflow_bundle
from vibecomfy.registry.ready import workflow_from_ready

from astrid.core.generation import entries, preflight
from astrid.sdk.generation import GenerationFacade


def test_managed_dry_run_canonicalizes_bound_entry_once_per_request(tmp_path, monkeypatch):
    source = tmp_path / "workflow.py"
    workflow_bundle.emit_bundle(workflow_from_ready("image/z_image_turbo"), source, {"operation": "authored"})
    monkeypatch.setattr(entries, "user_entries", lambda: [{
        "id": "test-capsule", "kind": "image", "modes": ["t2i"], "bundle": str(source),
        "binds": {}, "defaults": {}, "needs": [],
    }])
    monkeypatch.setattr(preflight, "select_generation_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *args, **kwargs: None)

    builds = []
    canonicalize = workflow_bundle._canonicalize_for_v2_pair

    def track(workflow, companion, **kwargs):
        # A v2 companion already contains the canonical identity. VibeComfy
        # returns immediately in that case, including for declaration rendering.
        if companion is None or companion.get("format_version") != 2:
            builds.append(kwargs["path"])
        return canonicalize(workflow, companion, **kwargs)

    monkeypatch.setattr(workflow_bundle, "_canonicalize_for_v2_pair", track)
    for request_number in (1, 2):
        preview = GenerationFacade().image(entry="test-capsule", placement="local", prompt="one boat",
                                           seed=42, dry_run=True)
        assert preview["entry"]["requirements"]["models"]
        assert len(builds) == request_number
        assert not builds[-1].exists()
    assert builds[0] != builds[1]
