from __future__ import annotations

from astrid.sdk import generation


def test_image_alias_forwards_every_public_argument_to_the_one_media_body(monkeypatch):
    seen = {}

    def capture(self, modality, /, **arguments):
        seen['modality'] = modality
        seen['arguments'] = arguments
        return 'routed'

    monkeypatch.setattr(generation.GenerationFacade, '_media_request', capture)
    facade = generation.GenerationFacade.__new__(generation.GenerationFacade)
    result = facade.image(model='m', mode='t2i', seed=7, wait=False, prompt='a red fox')

    assert result == 'routed'
    assert seen['modality'] == 'image'
    assert seen['arguments']['model'] == 'm'
    assert seen['arguments']['mode'] == 't2i'
    assert seen['arguments']['wait'] is False
    assert seen['arguments']['prompt'] == 'a red fox'
    assert 'self' not in seen['arguments'] and 'inputs' not in seen['arguments']


def test_video_alias_forwards_its_own_arguments_to_the_one_media_body(monkeypatch):
    seen = {}

    def capture(self, modality, /, **arguments):
        seen['modality'] = modality
        seen['arguments'] = arguments
        return 'routed'

    monkeypatch.setattr(generation.GenerationFacade, '_media_request', capture)
    facade = generation.GenerationFacade.__new__(generation.GenerationFacade)
    result = facade.video(model='m', request={'x': 1}, asset_map={'a': 'b'}, resume=False, prompt='pan')

    assert result == 'routed'
    assert seen['modality'] == 'video'
    assert seen['arguments']['request'] == {'x': 1}
    assert seen['arguments']['asset_map'] == {'a': 'b'}
    assert seen['arguments']['resume'] is False
    assert seen['arguments']['align'] is None
    assert seen['arguments']['prompt'] == 'pan'
