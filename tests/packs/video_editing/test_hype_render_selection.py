"""Composition checks for Hype's pack-owned render step."""

from __future__ import annotations

from pathlib import Path
import wave

from astrid.core.execution.executor.registry import ExecutorRegistry, load_default_registry
from astrid.packs.video_editing.actions.hype import entrypoint as hype


def _args(tmp_path: Path):
    video = Path(__file__).resolve().parents[2] / "fixtures" / "reshape" / "hype_regression" / "main.mp4"
    brief = tmp_path / "brief.md"
    brief.write_text("A short launch film.\n", encoding="utf-8")
    audio = tmp_path / "voice.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    return hype.resolve_args(
        ["--video", str(video), "--audio", str(audio), "--brief", str(brief), "--out", str(tmp_path / "out"), "--render"]
    )


def test_real_hype_sequence_selects_render_and_editor_review_when_ready(tmp_path):
    args = _args(tmp_path)
    registry = load_default_registry()

    steps = hype.select_steps(args)
    names = [step.name for step in steps]
    render_action = registry.get("rendering.render")

    assert {"cut", "render", "editor_review", "validate"}.issubset(names)
    assert "rendered_video" in render_action.graph.provides

    render_step = next(step for step in steps if step.name == "render")
    review_step = next(step for step in steps if step.name == "editor_review")
    assert "astrid.packs.rendering.actions.render.run" in render_step.build_cmd(args)
    assert "astrid.packs.editorial.actions.editor_review.run" in review_step.build_cmd(args)


def test_real_hype_sequence_omits_render_and_review_without_cut_facts(tmp_path, monkeypatch):
    args = _args(tmp_path)
    discovered = load_default_registry()
    # Keep every definition discovered from the real installed pack manifests,
    # while modeling a selected registry in which video_editing.cut is absent.
    # Hype still has its concrete cut Step, but that step cannot contribute
    # timeline/assets facts to the sequence.
    selected_registry = ExecutorRegistry(
        executor for executor in discovered.list() if executor.id != "video_editing.cut"
    )

    monkeypatch.setattr(
        "astrid.core.execution.executor.registry.load_default_registry",
        lambda: selected_registry,
    )

    steps = hype.select_steps(args)
    names = [step.name for step in steps]

    assert "cut" in names
    assert "render" not in names
    assert "editor_review" not in names
    assert "validate" not in names
