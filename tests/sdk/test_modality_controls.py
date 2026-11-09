from __future__ import annotations

from astrid.sdk import generation


def test_modality_controls_equal_the_inline_block_they_replace():
    # image, video and audio used to build these controls inline; the shared
    # helper must produce the same dict for the same public keywords.
    common = dict(engine='comfyui', consent=('paid',), max_usd=2.5, deadline='2030-01-01T00:00:00Z')
    extra = dict(offer_digest='sha256:' + 'b' * 64, preparation_seconds=30, execution_seconds=60, idle_seconds=5)

    expected = generation._generation_controls(**common)
    expected.update(**extra)
    expected['align'] = generation._align_mode('trust')

    assert generation._modality_controls(**common, **extra, align='trust') == expected
