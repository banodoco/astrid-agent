"""Replay the actual cu130 Qwen schema, including zero or populated image slots."""
import copy
import json
from pathlib import Path

import pytest
from vibecomfy.schema.cache import object_info_payload_checksum
from vibecomfy.schema.provider import TargetSchemaProvider as OriginalProvider
from vibecomfy.schema.validate import validate_api_against_schema, validate_api_link_shapes

from astrid.core.generation.backends.comfy_schema import TargetSchemaProvider

FIXTURE = Path(__file__).parents[3] / 'fixtures/comfyui/qwen-image21-autogrow.json'


def capture():
    data = json.loads(FIXTURE.read_text())
    for name, kind in [('ClipFixture', 'CLIP'), ('ImageFixture', 'IMAGE')]:
        data[name] = {'input': {'required': {}}, 'output': [kind]}
    return data


def prompt():
    return {
        '1': {'class_type': 'ClipFixture', 'inputs': {}},
        '2': {'class_type': 'TextEncodeQwenImage21', 'inputs': {
            'clip': ['1', 0], 'prompt': 'a fox', 'negative_prompt': '', 'resolution': 512}},
    }


def issues(api, provider):
    return [*validate_api_against_schema(api, provider), *validate_api_link_shapes(api, provider)]


def test_actual_zero_minimum_group_accepts_text_only_without_changing_capture():
    data = capture()
    original = copy.deepcopy(data)
    old = OriginalProvider.from_object_info(data, server_url='http://127.0.0.1:8188')
    # Current VibeComfy also understands the empty native group; keep both
    # implementations under the same live compatibility contract.
    assert issues(prompt(), old) == []
    provider = TargetSchemaProvider.from_object_info(data, server_url='http://127.0.0.1:8188')
    assert issues(prompt(), provider) == []
    schema = provider.get_schema('TextEncodeQwenImage21')
    assert schema.inputs['clip'].required
    assert 'images' not in schema.inputs
    assert schema.inputs['images.image_1'].type == 'IMAGE'
    assert not schema.inputs['images.image_1'].required
    assert schema.source_hash == object_info_payload_checksum(data)
    assert provider.object_info() == original == data
    assert provider.schemas()['TextEncodeQwenImage21'] == schema


def test_populated_slots_keep_native_names_and_types():
    provider = TargetSchemaProvider.from_object_info(capture(), server_url='http://127.0.0.1:8188')
    api = prompt()
    api['3'] = {'class_type': 'ImageFixture', 'inputs': {}}
    api['2']['inputs']['images.image_1'] = ['3', 0]
    assert issues(api, provider) == []
    api['2']['inputs']['images.image_1'] = ['1', 0]
    assert issues(api, provider), 'CLIP cannot satisfy the observed IMAGE socket'
    api['2']['inputs'].pop('images.image_1')
    api['2']['inputs']['images.image_17'] = ['3', 0]
    assert any(issue.code == 'unknown_input' for issue in issues(api, provider))


def test_other_required_inputs_still_block():
    provider = TargetSchemaProvider.from_object_info(capture(), server_url='http://127.0.0.1:8188')
    api = prompt()
    api['2']['inputs'].pop('clip')
    assert any(issue.code == 'missing_required_input' and issue.detail.get('input') == 'clip'
               for issue in issues(api, provider))


@pytest.mark.parametrize('minimum', [1, -1, False, None, '0'])
def test_only_explicit_integer_zero_uses_empty_group_contract(minimum):
    data = capture()
    data['TextEncodeQwenImage21']['input']['required']['images'][1]['template']['min'] = minimum
    provider = TargetSchemaProvider.from_object_info(data, server_url='http://127.0.0.1:8188')
    assert any(issue.code == 'missing_required_input' and issue.detail.get('input') == 'images'
               for issue in issues(prompt(), provider))


def test_refresh_rechecks_native_group_minimum():
    data = capture()
    changed = copy.deepcopy(data)
    changed['TextEncodeQwenImage21']['input']['required']['images'][1]['template']['min'] = 1
    provider = TargetSchemaProvider.from_object_info(
        data, server_url='http://127.0.0.1:8188', refresh_object_info=lambda: changed)
    assert issues(prompt(), provider) == []
    provider.refresh()
    assert any(issue.code == 'missing_required_input' for issue in issues(prompt(), provider))
