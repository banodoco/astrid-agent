from pathlib import Path

import pytest
from vibecomfy.security.provenance import Provenance
from vibecomfy.workflow_bundle import load_bundle

from astrid.packs.h3_av.shared.compile import compile_preparation
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request


@pytest.mark.parametrize('with_reference', [False, True])
def test_v1_continuation_binds_explicit_strength_and_sampler(tmp_path: Path, with_reference: bool, valid_h3_av_source: Path):
    assets = {'source.mp4': str(valid_h3_av_source)}
    references = []
    if with_reference:
        from PIL import Image
        image = tmp_path / 'reference.png'
        Image.new('RGB', (32, 32)).save(image)
        assets['reference.png'] = str(image)
        references.append({'asset': 'reference.png', 'purpose': 'appearance'})
    request = normalize_request({
        'version': 1, 'operation': 'continue',
        'source': {'asset': 'source.mp4', 'range': [0, 4]},
        'output': {'duration': 7}, 'content': {'prompt': 'Continue the shot.'},
        'changes': {
            'video': [{'during': [4, 7], 'area': {'full_frame': True}, 'action': 'generate'}],
            'audio': [{'during': [4, 7], 'action': 'generate'}],
        },
        'references': references, 'overrides': {'guidance': 1.0, 'sampler': 'euler'},
    })
    compiled = compile_preparation(prepare_request(request, asset_map=assets), out_dir=tmp_path / 'result')
    graph = load_bundle(compiled['workflow']['workflow.py']['path'], trust=Provenance.USER_CONFIRMED).workflow
    api = graph.compile('api', run_inputs=compiled['workflow_inputs'])
    assert api['935']['inputs']['lora_name'] == 'pika_h3_flex_turbo_8step_hd_ref2va.safetensors'
    assert api['935']['inputs']['strength_model'] == 1.0
    assert api['937']['inputs']['sampler_name'] == 'euler'
    loras = [model for model in graph.requirements.models if model['subdir'] == 'loras']
    assert [model['name'] for model in loras] == ['pika_h3_flex_turbo_8step_hd_ref2va.safetensors']
    assert loras[0]['sha256'] == 'dfe46d972408d1094053ff1f30c720bf16176ec334962009767ab0fff29e6de2'
    assert compiled['effective_settings']['turbo']['strength'] == 1.0
    assert compiled['effective_settings']['sampler'] == 'euler'


def test_default_builder_and_disabled_turbo_requirements():
    from astrid.packs.h3_av.shared.generation import build_generation_workflow

    args = dict(references=['reference.png'], prompt='A scene.', frames=24,
                model='minimax_h3_ref2va_pruned_int8_convrot.safetensors', steps=8, seed=1)
    enabled = build_generation_workflow(**args)
    disabled = build_generation_workflow(**args, turbo_enabled=False)
    enabled_loras = [row['name'] for row in enabled.requirements.models if row['subdir'] == 'loras']
    disabled_loras = [row['name'] for row in disabled.requirements.models if row['subdir'] == 'loras']
    assert enabled_loras == ['pika_h3_flex_turbo_8step_hd_ref2va.safetensors']
    assert disabled_loras == []
