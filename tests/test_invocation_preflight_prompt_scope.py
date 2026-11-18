"""Which prompt inputs make an empty prompt a refusal.

The emptiness rule guards prompt-driven workflows. An optional hint on a node that
is not a prompt node (Whisper's ``initial_prompt``, compiled as ``prompt=''``) is
not a requirement, so a transcription must still pass the same preflight.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.vibecomfy import invocation_preflight as preflight

TRANSCRIPTION = (
    Path(__file__).resolve().parents[1]
    / "astrid/packs/generation/workflows/transcription/workflow.py"
)


def _loaded(api_nodes, *, declared_inputs=None, reachable=("out",)):
    """A loaded workflow whose compiled API is ``api_nodes`` and whose output reaches ``reachable``."""

    class Workflow:
        inputs = dict(declared_inputs or {})
        outputs = [SimpleNamespace(node_id="out")]
        edges = [SimpleNamespace(from_node=node, to_node="out") for node in reachable if node != "out"]

        def compile(self, _kind, *, run_inputs):
            del run_inputs
            return {node_id: {"class_type": cls, "inputs": dict(fields)}
                    for node_id, (cls, fields) in api_nodes.items()}

    return SimpleNamespace(
        resolved=SimpleNamespace(
            workflow=Workflow(),
            workflow_identity="fixture",
            revision_id="rev-1",
            semantic_digest="sha256:" + "1" * 64,
        ),
        workflow_content_digest="sha256:" + "2" * 64,
    )


def _run(monkeypatch, tmp_path, loaded, **kwargs):
    workflow = tmp_path / "workflow.py"
    workflow.write_text("workflow = 'fixture'", encoding="utf-8")
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.load_workflow_path",
        lambda *_args, **_kwargs: loaded,
    )
    return preflight.preflight_invocation(workflow, run_inputs={}, **kwargs)


def test_shipped_transcription_passes_its_own_preflight_offline():
    receipt = preflight.preflight_invocation(
        TRANSCRIPTION, run_inputs={"audio_ref": "input.wav"}, phase="submission"
    )

    assert "apply_whisper" in receipt["reachable_nodes"]


def test_empty_optional_hint_on_a_non_prompt_node_is_not_a_required_prompt(monkeypatch, tmp_path):
    loaded = _loaded({
        "out": ("SaveText", {"text": "x"}),
        "whisper": ("Apply Whisper", {"prompt": ""}),
    }, reachable=("whisper",))

    receipt = _run(monkeypatch, tmp_path, loaded)

    assert receipt["reachable_nodes"] == ["out", "whisper"]


def test_auto_registered_node_binding_is_not_a_declared_prompt(monkeypatch, tmp_path):
    # ``node:<id>.<field>`` inputs are registered for every schema literal, so one named
    # after a prompt field does not make the entry prompt-driven.
    loaded = _loaded({
        "out": ("SaveText", {"text": "x"}),
        "whisper": ("Apply Whisper", {"prompt": ""}),
    }, declared_inputs={"node:whisper.prompt": SimpleNamespace(node_id="whisper", field="prompt")},
        reachable=("whisper",))

    receipt = _run(monkeypatch, tmp_path, loaded)

    assert receipt["reachable_nodes"] == ["out", "whisper"]


@pytest.mark.parametrize("class_type,field", [("Prompt", "prompt"), ("CLIPTextEncode", "text")])
def test_empty_prompt_is_still_refused_for_a_prompt_driven_workflow(monkeypatch, tmp_path, class_type, field):
    loaded = _loaded({
        "out": ("SaveImage", {"images": "x"}),
        "prompt": (class_type, {field: "   "}),
    }, reachable=("prompt",))

    with pytest.raises(preflight.InvocationPreflightError, match="PROMPT_EMPTY"):
        _run(monkeypatch, tmp_path, loaded)


def test_empty_declared_prompt_input_is_still_refused(monkeypatch, tmp_path):
    loaded = _loaded({
        "out": ("SaveImage", {"images": "x"}),
        "sampler": ("KSampler", {"prompt": ""}),
    }, declared_inputs={"prompt": SimpleNamespace(node_id="sampler", field="prompt")},
        reachable=("sampler",))

    with pytest.raises(preflight.InvocationPreflightError, match="PROMPT_EMPTY"):
        _run(monkeypatch, tmp_path, loaded)


def test_one_non_empty_required_prompt_still_satisfies_the_rule(monkeypatch, tmp_path):
    loaded = _loaded({
        "out": ("SaveImage", {"images": "x"}),
        "negative": ("CLIPTextEncode", {"text": ""}),
        "positive": ("CLIPTextEncode", {"text": "a red square"}),
    }, reachable=("negative", "positive"))

    receipt = _run(monkeypatch, tmp_path, loaded)

    assert "positive" in receipt["reachable_nodes"]
