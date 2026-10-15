from __future__ import annotations

import math

from astrid.core.timeline import audio_curves
from scripts.gen_audio_curves import TARGET, render


def test_registry_mirror_is_generated_from_python_registry():
    assert TARGET.read_text(encoding="utf-8") == render()


def test_equal_power_crossfade_preserves_uncorrelated_power():
    for step in range(101):
        progress = step / 100
        power = audio_curves.gain("equal-power", progress) ** 2
        power += audio_curves.gain("equal-power", progress, fade_in=False) ** 2
        db = 10 * math.log10(power)
        assert abs(db) <= 0.5


def test_missing_curve_keeps_legacy_linear_gain():
    for step in range(101):
        progress = step / 100
        assert audio_curves.gain("linear", progress) == progress
        assert audio_curves.gain("linear", progress, fade_in=False) == 1 - progress


def test_exponential_speed_names_match_their_gain_shape():
    assert audio_curves.gain("exponential-fast", 0.5) > 0.5
    assert audio_curves.gain("exponential-slow", 0.5) < 0.5


def test_exponential_moderate_is_registry_driven_and_keeps_curve_endpoints():
    name = "exponential-moderate"
    assert audio_curves.validate(name) == name
    assert name in audio_curves.names()
    assert audio_curves.gain(name, 0) == 0
    assert audio_curves.gain(name, 1) == 1
    assert audio_curves.gain(name, 0, fade_in=False) == 1
    assert audio_curves.gain(name, 1, fade_in=False) == 0
    midpoint = audio_curves.gain(name, 0.5)
    assert audio_curves.gain("exponential-slow", 0.5) < midpoint < 0.5
