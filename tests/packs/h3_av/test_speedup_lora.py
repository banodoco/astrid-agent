from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from astrid.packs.h3_av.shared import engine_facts
from astrid.packs.h3_av.shared.compile import _continuation_bundle, _v1_native_settings
from astrid.packs.h3_av.shared.generation import PIKA_REF2VA_FILENAME
from astrid.packs.h3_av.shared.request_v2 import H3RequestError, _settings
from vibecomfy.security.provenance import Provenance
from vibecomfy.workflow_bundle import load_bundle

ROOT = Path(__file__).resolve().parents[3]


def test_choice_defaults_and_refusals():
    defaults = _settings({})
    assert defaults['turbo'] == {'enabled': True, 'lora': 'pika_flex_8step', 'strength': 1.0}
    assert [defaults[k] for k in ('steps', 'sampler', 'scheduler', 'cfg', 'video_shift', 'audio_shift')] == [8, 'res_multistep', 'simple', 1.0, 12.0, 3.0]
    override = _settings({'steps': 7, 'sampler': 'euler', 'video_shift': 10, 'audio_shift': 2, 'guidance': 0.7})
    assert [override[k] for k in ('steps', 'sampler', 'video_shift', 'audio_shift')] == [7, 'euler', 10, 2]
    assert override['turbo']['strength'] == 0.7
    with pytest.raises(H3RequestError, match='unknown.*absent'):
        _settings({'turbo': {'lora': 'absent'}})
    with pytest.raises(H3RequestError, match='lightx2v_8step.*no matching variant'):
        _settings({'turbo': {'lora': 'lightx2v_8step'}})
    with pytest.raises(H3RequestError, match='pika_flex_8step.*no matching variant'):
        _settings({'model': 'other-model'})
    assert _v1_native_settings({'guidance': 0.9})['turbo']['strength'] == 0.9
    with pytest.raises(H3RequestError, match='lightx2v_8step.*no matching variant'):
        _v1_native_settings({'turbo_lora': 'lightx2v_8step'})
    with pytest.raises(H3RequestError, match='settings.cfg'):
        _settings({'cfg': 2})


@pytest.mark.parametrize('source', ['native_h3_continuation', 'native_h3_continuation_refs'])
def test_continuation_emits_selected_asset_and_settings(tmp_path, monkeypatch, source):
    rows = json.loads((ROOT / 'astrid/packs/runpod/models.lock.json').read_text())['models']
    locked = {r['destination']: SimpleNamespace(**r) for r in rows}
    monkeypatch.setattr(engine_facts, '_FACTS', SimpleNamespace(locked_models=lambda: locked))
    settings = _settings({'sampler': 'euler', 'video_shift': 9, 'audio_shift': 2, 'turbo': {'strength': 0.7}})
    resource = _continuation_bundle(ROOT / 'astrid/packs/h3_av/workflows' / source, tmp_path, settings)
    workflow = load_bundle(resource / 'workflow.py', trust=Provenance.USER_CONFIRMED).workflow
    graph = workflow.compile('api')
    assert graph['935']['inputs']['lora_name'] == PIKA_REF2VA_FILENAME
    assert graph['935']['inputs']['strength_model'] == 0.7
    assert graph['937']['inputs']['sampler_name'] == 'euler'
    assert graph['5']['inputs']['shift_video'] == 9
    assert graph['5']['inputs']['shift_audio'] == 2
    loras = [r for r in workflow.requirements.models if r['subdir'] == 'loras']
    assert [r['name'] for r in loras] == [PIKA_REF2VA_FILENAME]
    assert loras[0]['sha256'] == locked['loras/' + PIKA_REF2VA_FILENAME].sha256
