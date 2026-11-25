import shutil

import pytest

from astrid.core.rendering import _effect_registry, scene_answers
from tests.packs.rendering.test_scene_host import BASIS, host_project, native_host, props


def test_native_scene_compiles_selected_staged_registry(native_host, host_project, tmp_path):
    project, pack = host_project
    staged = tmp_path / "staged"
    (staged / "src").mkdir(parents=True)
    for kind in ("effects", "animations", "transitions"):
        shutil.copyfile(project / "src" / f"{kind}.generated.ts",
                        staged / "src" / f"{kind}.generated.ts")
    roots = _effect_registry.render_pack_roots()
    roots.append({"root": str(pack), "scope": "pack-probe-elements"})
    answers = scene_answers.host_frames_at(props(), BASIS, [0],
                                           _render_roots=roots, _registry_root=str(staged))
    assert answers[0]["frame"] == 0
    with (staged / "src" / "effects.generated.ts").open("a") as generated:
        generated.write("\nimport 'missing-staged-registry-sentinel';\n")
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="missing-staged-registry-sentinel"):
        scene_answers.host_frames_at(props(), BASIS, [0],
                                    _render_roots=roots, _registry_root=str(staged))
