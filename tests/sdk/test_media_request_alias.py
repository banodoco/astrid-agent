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


def test_audio_alias_forwards_to_the_one_media_body(monkeypatch):
    seen = {}

    def capture(self, modality, /, **arguments):
        seen['modality'] = modality
        seen['arguments'] = arguments
        return 'routed'

    monkeypatch.setattr(generation.GenerationFacade, '_media_request', capture)
    facade = generation.GenerationFacade.__new__(generation.GenerationFacade)
    assert facade.audio(model='m', mode='tts', resume=False, prompt='hello') == 'routed'
    assert seen['modality'] == 'audio'
    assert seen['arguments']['mode'] == 'tts'
    assert seen['arguments']['prompt'] == 'hello'


def test_media_kind_rules_cover_each_kind_and_the_documented_differences():
    kinds = generation._MEDIA_KINDS
    assert set(kinds) == {'image', 'video', 'audio'}
    assert kinds['image'].capability == 'generation.generate_image'
    assert kinds['video'].capability == 'generation.generate_video'
    assert kinds['audio'].capability == 'generation.generate_audio'
    # Audio rejects retired inputs before normalising them; the others normalise first.
    assert kinds['audio'].retire_inputs_first is True
    assert kinds['image'].retire_inputs_first is False
    assert kinds['video'].retire_inputs_first is False
    # Only audio routes a mode to the separate speech invoke, and only the TTS mode.
    assert kinds['audio'].speech_mode == 'tts'
    assert kinds['image'].speech_mode is None and kinds['video'].speech_mode is None
    # Audio validation uses the mode's declared requirements (None), image uses none.
    assert kinds['audio'].required_features(resolved_mode='music', inputs={}) is None
    assert kinds['image'].required_features(resolved_mode='t2i', inputs={}) == ()
