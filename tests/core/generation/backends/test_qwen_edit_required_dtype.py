"""Qwen edit must retain required UNET dtype against the shakedown schema."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from astrid.core.generation.backends.comfy_schema import TargetSchemaProvider
from astrid.core.generation.backends.vibecomfy import _load_pinned_catalog_workflow, VibeComfyBackend
from astrid.core.model_catalog.registry import ModelRegistry
from vibecomfy.porting.object_info import get_class
from vibecomfy.workflow_bundle import load_bundle


def test_qwen_edit_repairs_missing_required_dtype_using_live_schema(monkeypatch):
    from vibecomfy.registry import ready
    original = ready.workflow_from_ready
    def materialized_without_dtype(*args, **kwargs):
        workflow = original(*args, **kwargs)
        for node in workflow.runtime_nodes().values():
            if node.class_type == 'UNETLoader':
                node.inputs.pop('weight_dtype', None)
        return workflow
    monkeypatch.setattr(ready, 'workflow_from_ready', materialized_without_dtype)
    entry = ModelRegistry.load_default().get('qwen-image-2.1-turbo-edit')
    workflow = _load_pinned_catalog_workflow(entry.modes['edit'].backends['local'])
    api = workflow.compile('api')
    assert next(node['inputs'] for node in api.values() if node['class_type'] == 'UNETLoader')['weight_dtype'] == 'default'
    info = {}
    for node in workflow.runtime_nodes().values():
        raw = get_class(node.class_type)
        if raw:
            info[node.class_type] = {'input': copy.deepcopy(raw['inputs']),
                'input_order': copy.deepcopy(raw['input_order']),
                'output': [item['type'] for item in raw['outputs']],
                'output_name': [item['name'] for item in raw['outputs']],
                'output_is_list': [item['is_list'] for item in raw['outputs']]}
    fixture = Path(__file__).parents[3] / 'fixtures/comfyui/qwen-image21-autogrow.json'
    info.update(json.loads(fixture.read_text()))
    # Replay the actual managed ComfyUI loader schema captured on shakedown.
    live = fixture.parent / 'live-423cbd20/used-nodes.json'
    info['UNETLoader'] = json.loads(live.read_text())['UNETLoader']
    info['QwenImage21Cache'] = {
        'input': {'required': {'model': ['MODEL'], 'device': [['auto', 'gpu', 'cpu', 'off']],
                               'dtype': [['default', 'int8', 'int4']]}},
        'output': ['MODEL'], 'output_name': ['MODEL'], 'output_is_list': [False]}
    # The shakedown fixture captures the new dynamic encoder. Native loader
    # choices/schema include the captured native UNETLoader contract.
    for node in api.values():
        for field, value in node['inputs'].items():
            if node['class_type'] in {'UNETLoader', 'CLIPLoader', 'VAELoader', 'LoadImage'} and isinstance(value, str):
                raw = info[node['class_type']]['input']['required'].get(field)
                if raw and isinstance(raw[0], list) and value not in raw[0]:
                    raw[0].append(value)
    provider = TargetSchemaProvider.from_object_info(info, server_url='http://127.0.0.1:8188', model_availability='target')
    assert provider.get_schema('UNETLoader').inputs['weight_dtype'].required
    projection = load_bundle(workflow).compile(schema_provider=provider).to_dict()['api_projection']
    assert next(node['inputs'] for node in projection.values() if node['class_type'] == 'UNETLoader')['weight_dtype'] == 'default'
