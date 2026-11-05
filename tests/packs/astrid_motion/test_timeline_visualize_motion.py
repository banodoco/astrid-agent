import pytest

from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options
from astrid.packs.rendering.actions.timeline_visualize.layers import resolve_layers
from astrid.packs.rendering.actions.timeline_visualize.motion import element_motion
from astrid.packs.rendering.actions.timeline_visualize.motion.rules import resolve_rules


def test_contact_and_motion_options_are_bounded():
    contact = filmstrip_options({"view": "contact"})
    motion = filmstrip_options({"view": "motion", "cut": "1"})

    assert contact["sample"] == "cuts"
    assert contact["columns"] == 10
    assert motion["frame_budget"] == 60
    assert motion["resolution"] == [480, 270]


def test_motion_layer_registry_and_v3_element_lookup():
    assert resolve_layers(["sync"], view="motion")[0].name == "sync"
    modules = element_motion.modules()
    assert "am-presenter" in modules
    assert modules["am-presenter"].as_posix().endswith("rendering/elements/effects/am-presenter/motion.ts")
    assert "animated-media-transform" in modules
    assert modules["animated-media-transform"].as_posix().endswith(
        "rendering/elements/effects/animated-media-transform/motion.ts"
    )


@pytest.mark.integration
def test_transform_at_adapter_preserves_clip_time_and_canvas_units():
    modules = element_motion.modules()
    values = element_motion.evaluate(
        [{
            "id": "animated@0.0000",
            "type": "animated-media-transform",
            "params": {"keyframes": [
                {"at": 0, "x": 10, "y": 20, "width": 1920, "height": 1080, "opacity": 1},
                {"at": 1, "x": 110, "y": 220, "width": 960, "height": 540, "opacity": 0.5},
            ]},
            "fps": 30,
            "frames": 31,
        }],
        {"animated-media-transform": modules["animated-media-transform"]},
    )
    assert values["animated@0.0000"][15] == {
        "x": 60,
        "y": 120,
        "width": 1440,
        "height": 810,
        "opacity": 0.75,
        "visible": 1,
    }


def test_transform_at_adapter_fails_closed_on_invalid_keyframes():
    modules = element_motion.modules()
    with pytest.raises(element_motion.Unavailable):
        element_motion.evaluate(
            [{"id": "bad@0.0000", "type": "animated-media-transform", "params": {"keyframes": []},
              "fps": 30, "frames": 1}],
            {"animated-media-transform": modules["animated-media-transform"]},
        )


def test_rules_do_not_use_worker_cwd(tmp_path, monkeypatch):
    (tmp_path / "astrid-lint.toml").write_text("max_cut_s = 4\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert resolve_rules() is None
    assert resolve_rules(str(tmp_path / "astrid-lint.toml"))["params"]["max_cut_s"] == 4.0
