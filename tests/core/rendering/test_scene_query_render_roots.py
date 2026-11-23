import json
from types import SimpleNamespace

import pytest

from astrid.core.rendering import remotion_runtime, scene_answers


def test_scene_query_uses_owned_roots_and_selected_registry(tmp_path, monkeypatch):
    for name in ("scene-query.mjs", "native-scene.mjs", "src/host-scene.ts"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    monkeypatch.setattr(scene_answers, "remotion_runtime_status", lambda **_: SimpleNamespace(
        available=True, project_dir=tmp_path, node_executable=tmp_path / "node"))
    monkeypatch.setattr(scene_answers, "probe_context", lambda **_: {"node_env": {}})
    monkeypatch.setenv("ASTRID_RENDER_PACK_ROOTS", '[{"root":"/foreign"}]')
    monkeypatch.setenv("ASTRID_RENDER_REGISTRY_ROOT", "/foreign")
    monkeypatch.setenv("ASTRID_REMOTION_VALIDATION_PATH", str(tmp_path / "validation"))
    roots = [{"root": str(tmp_path / "pack"), "scope": "pack-example-elements"}]
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout='[{"frame": 1}]')

    monkeypatch.setattr(scene_answers.subprocess, "run", run)
    monkeypatch.setattr(remotion_runtime, "_validated_runtime", lambda _: {
        "registry": {"mode": "source_settled", "renderer": {"observation": {"render_pack_roots": roots}}}})
    assert scene_answers._query({"props": {}}, [1]) == [{"frame": 1}]
    assert json.loads(calls[-1]["ASTRID_RENDER_PACK_ROOTS"]) == roots
    assert "ASTRID_RENDER_REGISTRY_ROOT" not in calls[-1]

    selected = [{"root": str(tmp_path / "staged"), "scope": "pack-pinned-elements"}]
    scene_answers._query({"props": {}}, [1], render_roots=selected, registry_root=str(tmp_path / "generated"))
    assert json.loads(calls[-1]["ASTRID_RENDER_PACK_ROOTS"]) == selected
    assert calls[-1]["ASTRID_RENDER_REGISTRY_ROOT"] == str(tmp_path / "generated")

    monkeypatch.setattr(remotion_runtime, "_validated_runtime", lambda _: {"registry": {"mode": "provisioned_only"}})
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="validated scene registry roots are unavailable"):
        scene_answers._query({"props": {}}, [1])

    monkeypatch.delenv("ASTRID_REMOTION_VALIDATION_PATH")
    scene_answers._query({"props": {}}, [1])
    assert "ASTRID_RENDER_PACK_ROOTS" not in calls[-1]
    assert "ASTRID_RENDER_REGISTRY_ROOT" not in calls[-1]
