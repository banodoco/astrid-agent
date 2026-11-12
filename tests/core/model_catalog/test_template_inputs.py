"""Catalog input contracts must reach the pinned template's public ports."""

from __future__ import annotations

import pytest

import json

from astrid import console
from astrid.core.generation import cli, entries, preflight
from astrid.core.generation.backends.vibecomfy import (
    VibeComfyBackend,
    _load_pinned_catalog_workflow,
)
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.schema import BackendSpec, ModeSpec
from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource
from vibecomfy.registry.ready import workflow_from_ready


@pytest.mark.parametrize("kind,mode", [("video", "v2v"), ("audio", "sfx")])
def test_video_reference_flag_uses_the_canonical_input(kind, mode):
    parsed = cli._parser().parse_args([kind, "--mode", mode, "--video-ref", "motion.mp4", "--dry-run"])
    assert cli._values(parsed) == {"video_ref": "motion.mp4"}


@pytest.mark.parametrize("entry_id", ["lava-sr", "deepfilternet3"])
def test_promptless_audio_enhancement_dry_run(entry_id, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *a, **k: None)
    rc = console.main([
        "generate", "audio", "--entry", entry_id, "--mode", "sfx",
        "--input", "audio_ref=test-value", "--placement", "runpod", "--dry-run", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload.get("ok", True), payload


def test_catalog_description_uses_the_backend_parameter_map(monkeypatch):
    workflow = VibeWorkflow("mapped", WorkflowSource("mapped"))
    workflow.nodes["1"] = VibeNode("1", "LoadImage", inputs={"image": ""})
    workflow.register_input("source_picture", "1", "image", "", type="STRING", required=True)
    monkeypatch.setattr(
        "astrid.core.generation.backends.vibecomfy._load_pinned_catalog_workflow",
        lambda backend: workflow,
    )
    spec = ModeSpec(
        supports=("image_ref",), requires=("image_ref",),
        backends={"local": BackendSpec(template="image/mapped", param_map={"image_ref": "source_picture"})},
    )
    description = {"inputs": entries._catalog_inputs("image", spec, "i2i")}
    assert entries.validate_inputs(description, {"image_ref": "caller.png"}) == {"source_picture": "caller.png"}
    with pytest.raises(entries.MissingRequiredEntryInputsError, match="source_picture"):
        entries.validate_inputs(description, {})


def test_catalog_required_inputs_match_pinned_template_public_inputs():
    disagreements = []
    for entry in ModelRegistry.load_default().list_all(include_closed=True):
        for mode, spec in entry.modes.items():
            backend = spec.backends.get("local")
            if backend is None or not backend.template:
                continue
            workflow = _load_pinned_catalog_workflow(backend)
            translation = backend.param_map or VibeComfyBackend.DEFAULT_PARAM_MAP.get(mode, {})
            identities = {name: (str(port.node_id), port.field) for name, port in workflow.inputs.items()}
            public_names = {
                spelling: identities[name]
                for name, port in workflow.inputs.items()
                for spelling in (name, *port.aliases)
            }
            labels = {}
            for name, identity in identities.items():
                labels.setdefault(identity, name)
            declared = set()
            for required in spec.requires:
                target = translation.get(required, required)
                if target not in public_names:
                    disagreements.append(f"{entry.id}/{mode}: required {required!r} maps to absent public input {target!r}")
                else:
                    declared.add(public_names[target])
            template_required = {identities[name] for name, port in workflow.inputs.items() if port.required}
            for identity in sorted(template_required - declared):
                disagreements.append(f"{entry.id}/{mode}: template requires undeclared public input {labels[identity]!r}")
    assert not disagreements, "\n".join(disagreements)


def test_ltx_first_last_frames_bind_independently_to_the_template():
    spec = ModelRegistry.load_default().get('ltx-2.3').modes['flf']
    workflow = _load_pinned_catalog_workflow(spec.backends['local'])
    assert (workflow.inputs['start_image'].node_id, workflow.inputs['start_image'].field) == ('45', 'image')
    assert (workflow.inputs['end_image'].node_id, workflow.inputs['end_image'].field) == ('47', 'image')
    for name in ('start_image', 'end_image'):
        assert workflow.inputs[name].required
        assert workflow.inputs[name].media_semantics == 'image'
    description = {'inputs': entries._catalog_inputs('video', spec, 'flf')}
    values = entries.validate_inputs(description, {
        'prompt': 'A transition', 'image_ref': 'first.png', 'image_end_ref': 'last.png',
    })
    assert values['start_image'] == 'first.png'
    assert values['end_image'] == 'last.png'
    workflow.set_input('start_image', values['start_image'])
    workflow.set_input('end_image', values['end_image'])
    api = workflow.compile('api')
    assert api['45']['inputs']['image'] == 'first.png'
    assert api['47']['inputs']['image'] == 'last.png'
    for omitted in ('image_ref', 'image_end_ref'):
        with pytest.raises(entries.MissingRequiredEntryInputsError):
            entries.validate_inputs(description, {
                key: value for key, value in {
                    'prompt': 'A transition', 'image_ref': 'first.png', 'image_end_ref': 'last.png',
                }.items() if key != omitted
            })


@pytest.mark.parametrize('template_id,start,end', [
    ('video/ltx2_3_lightricks_first_last_parity', '1', '2'),
    ('video/ltx2_3_lightricks_first_last_two_stage_lowvram', '1', '2'),
    ('video/ltx2_3_first_last_frame_travel_iclora_control', '5', '6'),
    ('video/ltx2_3_runexx_first_last_raw_video_guide', '6', '7'),
])
def test_other_ready_ltx_first_last_templates_expose_both_frames(template_id, start, end):
    workflow = workflow_from_ready(template_id)
    for name, node_id, value in [('start_image', start, 'first.png'), ('end_image', end, 'last.png')]:
        port = workflow.inputs[name]
        assert (port.node_id, port.field, port.required, port.media_semantics) == (node_id, 'image', True, 'image')
        workflow.set_input(name, value)
    api = workflow.compile('api')
    assert api[start]['inputs']['image'] == 'first.png'
    assert api[end]['inputs']['image'] == 'last.png'
    workflow.set_input('image', 'replacement.png')
    api = workflow.compile('api')
    assert api[start]['inputs']['image'] == 'replacement.png'
    assert api[end]['inputs']['image'] == 'last.png'
