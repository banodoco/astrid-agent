"""Host-side Remotion project selection binds staging to admitted source bytes."""
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pytest
from tests.packs.rendering.test_pinned_project_elements import admitted, configured_project_pack, _snapshot

from astrid.core.rendering.verified_source import (
    SOURCE_ENV,
    mint_prepared_source,
)


def _source(tmp_path: Path, *, selected: bool = True, config=None):
    attempt = tmp_path / "attempt"
    inputs = attempt / "inputs"
    inputs.mkdir(parents=True)
    project_selection = {
        "realm": "realm-a",
        "project": "project-a",
        "reference": {
            "packId": "pack-a",
            "kind": "effect",
            "id": "fade",
            "revision": "sha256:" + "b" * 64,
        },
        "verified": {"pin": {"document_id": "doc-a"}},
        "snapshot": {"object_id": "object-a", "bytes_base64": "c25hcHNob3Q="},
    }
    config = config if config is not None else {"tracks": [], "clips": []}
    if selected and "app" not in config:
        config["app"] = {"astrid_project_element_snapshots": [project_selection]}
    timeline = inputs / "timeline.json"
    assets = inputs / "assets.json"
    timeline.write_text(json.dumps(config), encoding="utf-8")
    assets.write_text(json.dumps({"assets": {}}), encoding="utf-8")
    authority = {
        "project_id": "project-a",
        "timeline_id": "timeline-a",
        "parent_revision_id": "revision-a",
        "parent_content_digest": "sha256:" + "a" * 64,
    }
    ref = {
        "project_id": "project-a",
        "timeline_id": "timeline-a",
        "revision_id": "revision-a",
        "revision_digest": authority["parent_content_digest"],
        "source_kind": "revision",
    }
    source = mint_prepared_source(
        ref=ref,
        authority=authority,
        snapshot={"config": config, "registry": {"assets": {}}},
        timeline_path=timeline,
        assets_path=assets,
        attempt=attempt,
    )
    return attempt, timeline, assets, source


def _install_host_dependencies(monkeypatch, *, launch_roots, actual_roots):
    state = {"version": 1, "content_hashes": {}}

    class Generator:
        def _element_registry(self):
            return "base-registry"

        def render_pack_roots(self, *, registry, include_aliases):
            assert include_aliases is True
            if registry == "base-registry":
                return actual_roots
            return [*actual_roots, {"root": "/selected/project-pack", "scope": "pack-selected"}]

        def compute_generated_registry_state(self, *, registry):
            assert registry == "base-registry"
            return state

    generator = Generator()
    # The helper imports these collaborators inside the function, so patch
    # their defining modules before calling it.
    from astrid.core.rendering import remotion_runtime, project_elements
    from astrid.packs.rendering.rendering.renderers.remotion import lock, run

    monkeypatch.setattr(remotion_runtime, "_validated_runtime", lambda *_a, **_k: {
        "registry": {"mode": "source_settled", "renderer": {"observation": {
            "render_pack_roots": launch_roots, "state": state,
        }}}
    })
    monkeypatch.setattr(lock, "remotion_render_lock", nullcontext)
    monkeypatch.setattr(run, "gen_effect_registry", generator)
    staged_calls = []

    def stage(config, root, *, registry):
        staged_calls.append((config, Path(root), registry))
        return "selected-registry", [{"root": "/selected/project-pack", "scope": "pack-selected"}]

    monkeypatch.setattr(project_elements, "stage_project_elements", stage)

    def write_selected(root, registry):
        Path(root).mkdir(parents=True)
        (Path(root) / "effects.generated.ts").write_text("generated", encoding="utf-8")
        return {"state": "generated"}

    monkeypatch.setattr(run, "_write_selected_element_registries", write_selected)
    monkeypatch.setattr(run, "_serialize_timeline", lambda path: json.loads(Path(path).read_text()))
    return staged_calls


def test_prepare_project_selection_stages_digest_bound_source(tmp_path, monkeypatch):
    from astrid.core.execution.remotion_host_project import prepare_project_selection

    attempt, timeline, assets, source = _source(tmp_path)
    base_roots = [{"root": "/launch/base", "scope": "installed"}]
    staged_calls = _install_host_dependencies(
        monkeypatch, launch_roots=base_roots, actual_roots=base_roots,
    )
    project = tmp_path / "remotion"
    project.mkdir()
    environment = {SOURCE_ENV: source._anchor, "TEST_ENV": "preserved"}

    result = prepare_project_selection(project, attempt, environment, source)

    assert result["source"] is source
    assert result["timeline"] == str(timeline.resolve())
    assert result["assets"] == str(assets.resolve())
    assert result["roots"] == [
        *base_roots,
        {"root": "/selected/project-pack", "scope": "pack-selected"},
    ]
    assert result["registry_root"] == str((Path(result["temporary"].name) / "host"))
    assert staged_calls[0][0]["app"]["astrid_project_element_snapshots"]
    assert staged_calls[0][2] == "base-registry"
    assert environment == {SOURCE_ENV: source._anchor, "TEST_ENV": "preserved"}
    result["temporary"].cleanup()


def test_prepare_project_selection_refuses_changed_handed_off_source_bytes(tmp_path, monkeypatch):
    from astrid.core.execution.remotion_host_project import prepare_project_selection

    attempt, timeline, _assets, source = _source(tmp_path)
    base_roots = [{"root": "/launch/base", "scope": "installed"}]
    _install_host_dependencies(monkeypatch, launch_roots=base_roots, actual_roots=base_roots)
    timeline.write_text('{"clips":[]}', encoding="utf-8")

    with pytest.raises(ValueError, match="verified source prepared timeline bytes differ"):
        prepare_project_selection(
            tmp_path / "remotion", attempt, {SOURCE_ENV: source._anchor}, source,
        )


def test_prepare_project_selection_refuses_launch_base_root_mismatch(tmp_path, monkeypatch):
    from astrid.core.execution.remotion_host_project import prepare_project_selection

    attempt, _timeline, _assets, source = _source(tmp_path)
    launch_roots = [{"root": "/launch/expected", "scope": "installed"}]
    actual_roots = [{"root": "/runtime/other", "scope": "installed"}]
    _install_host_dependencies(
        monkeypatch, launch_roots=launch_roots, actual_roots=actual_roots,
    )

    with pytest.raises(ValueError, match="host registry differs from launch validation"):
        prepare_project_selection(
            tmp_path / "remotion", attempt, {SOURCE_ENV: source._anchor}, source,
        )


def test_prepare_project_selection_returns_none_without_verified_source(tmp_path):
    from astrid.core.execution.remotion_host_project import prepare_project_selection

    assert prepare_project_selection(tmp_path / "remotion", tmp_path, {}, None) is None


def test_prepare_project_selection_restages_admitted_project_snapshots(admitted, tmp_path, monkeypatch):
    from astrid.core.element.registry import ElementRegistry
    from astrid.core.execution.remotion_host_project import prepare_project_selection
    from astrid.core.rendering import _effect_registry as generator, remotion_runtime
    from astrid.packs.rendering.rendering.renderers.remotion import lock, run

    runtime, refs, _ = admitted
    config = _snapshot(runtime).config
    attempt, _, _, source = _source(tmp_path, config=config)
    base = ElementRegistry()
    roots = generator.render_pack_roots(registry=base, include_aliases=True)
    state = generator.compute_generated_registry_state(registry=base)
    monkeypatch.setattr(generator, "_element_registry", lambda: base)
    monkeypatch.setattr(run, "gen_effect_registry", generator)
    monkeypatch.setattr(run, "_serialize_timeline", lambda path: json.loads(Path(path).read_text()))
    monkeypatch.setattr(lock, "remotion_render_lock", nullcontext)
    monkeypatch.setattr(lock, "remotion_render_lock_held", lambda: True)
    monkeypatch.setattr(remotion_runtime, "_validated_runtime", lambda *_a, **_k: {
        "registry": {"mode": "source_settled", "renderer": {"observation": {
            "render_pack_roots": roots, "state": state,
        }}}
    })
    result = prepare_project_selection(tmp_path / "remotion", attempt, {SOURCE_ENV: source._anchor}, source)
    try:
        assert len(result["staged"]) == 2
        assert {row["scope"] for row in result["staged"]}.issubset(
            {row["scope"] for row in result["roots"]})
        content = (Path(result["registry_root"]) / "src/effects.generated.ts").read_text()
        for ref in refs:
            assert ref["revision"] in content
    finally:
        result["temporary"].cleanup()
