"""LTX text-only admission must use an image-free pinned template."""

from astrid.core.generation import entries
from astrid.core.generation.backends.vibecomfy import _load_pinned_catalog_workflow
from astrid.core.model_catalog.registry import ModelRegistry


def _ltx_t2v():
    return ModelRegistry.load_default().get('ltx-2.3').modes['t2v']


def test_catalog_t2v_requires_only_text_and_has_no_image_branch():
    mode = _ltx_t2v()
    assert mode.requires == ('prompt',)
    assert 'image_ref' not in mode.supports
    workflow = _load_pinned_catalog_workflow(mode.backends['local'])
    assert not any(port.media_semantics == 'image' for port in workflow.inputs.values())
    api = workflow.compile('api')
    assert not any(node['class_type'] in {'LoadImage', 'LTXVImgToVideoConditionOnly'}
                   for node in api.values())
    assert api['4528']['inputs']['video_latent'] == ['3059', 0]
    assert api['3059']['class_type'] == 'EmptyLTXVLatentVideo'


def test_prompt_only_t2v_is_admitted_by_the_catalog_template():
    description, values = entries.resolve_entry_inputs(
        'ltx-2.3', {'prompt': 'A quiet street at dusk.'}, mode='t2v', kind='video',
    )
    assert description['mode'] == 't2v'
    assert values['prompt'] == 'A quiet street at dusk.'
    assert not {'image', 'image_ref', 'input_image'} & values.keys()
