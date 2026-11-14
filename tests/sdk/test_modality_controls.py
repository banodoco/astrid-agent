from __future__ import annotations

from astrid.sdk import generation


def test_modality_controls_equal_the_inline_block_they_replace():
    common = dict(engine='comfyui', consent=('user_env',))
    expected = generation._generation_controls(**common)
    expected['align'] = generation._align_mode('trust')
    assert generation._modality_controls(**common, align='trust') == expected
