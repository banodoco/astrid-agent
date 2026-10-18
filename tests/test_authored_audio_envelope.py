"""Authored audio uses the shared gain validator, never render authority."""
from __future__ import annotations

import copy
import json
import math

import jsonschema
import pytest
from banodoco_timeline_schema import load_schema

from astrid.core.timeline import audio_curves
from astrid.core.timeline.audio_envelope import compile_legacy, evaluate
from astrid.core.timeline.banodoco_schema import (
    canonical_timeline_config,
    validate_timeline,
)


def envelope():
    return {"keyframes": [{"at": 0, "value": 2}, {"at": 0.75, "value": 3}],
            "multiply": [{"keyframes": [{"at": 0, "value": 0.8}]}],
            "minimum": [{"keyframes": [{"at": 0, "value": 0.5}]},
                        {"keyframes": [{"at": 0, "value": 0.25}]}], "duck": True}


def config(audio=True):
    clip = {"id": "bed", "at": 2, "track": "music", "clipType": "media", "asset": "tone",
            "from": 0.5, "to": 2.5, "speed": 2, "volume": 0.4,
            "params": {"fadeIn": 0.73, "fadeOut": 0.83, "opaque": [None, {"keep": True}]},
            "app": {"vendor": {"opaque": [1, None, False]}}}
    if audio:
        clip["audio"] = {"envelope": envelope()}
    return {"clips": [clip], "tracks": [{"id": "music", "kind": "audio", "label": "Music"}],
            "app": {"vendor": {"keep": "bytes"}}}


def test_canonical_root_and_standalone_clip_and_container_roundtrip():
    value = config()
    schema = load_schema()
    jsonschema.Draft7Validator(schema).validate(value)
    jsonschema.Draft7Validator({"$ref": "#/definitions/TimelineClip", "definitions": schema["definitions"]}).validate(value["clips"][0])
    assert canonical_timeline_config(json.loads(json.dumps(value))) == value
    assert schema["definitions"]["AudioEnvelopeKeyframe"]["properties"]["curve"]["enum"] == sorted(audio_curves.names())
    env = compile_legacy(value["clips"][0], 30)
    assert env == envelope()  # legacy fades must not be applied again
    assert evaluate(env, 0) == pytest.approx(2 * 0.8 * 0.25)
    assert evaluate(env, 0.75) == pytest.approx(3 * 0.8 * 0.25)
    legacy = config(False)
    assert "audio" not in canonical_timeline_config(legacy)["clips"][0]
    assert compile_legacy(legacy["clips"][0], 30) is not None


BAD = [
    None, {}, {"envelope": None}, {"envelope": {"keyframes": []}},
    *[{"envelope": {"keyframes": [{"at": 0, "value": v}]}} for v in (True, -1, math.nan, math.inf, -math.inf)],
    *[{"envelope": {"keyframes": [{"at": v, "value": 1}]}} for v in (True, math.nan, math.inf)],
    {"envelope": {"keyframes": [{"at": 1, "value": 1}, {"at": 0, "value": 1}]}},
    {"envelope": {"keyframes": [{"at": 0, "value": 1}, {"at": 0, "value": 2}]}},
    {"envelope": {"keyframes": [{"at": 0, "value": 1, "curve": "unknown"}]}},
    {"envelope": {"keyframes": [{"at": 0, "value": 1, "extra": 1}]}},
    *[{"envelope": {"keyframes": [{"at": 0, "value": 1}], "duck": v}} for v in (1, None, "true")],
    {"envelope": {"keyframes": [{"at": 0, "value": 1}], "multiply": [None]}},
    {"envelope": {"keyframes": [{"at": 0, "value": 1}], "minimum": {}}},
    {"envelope": {"keyframes": [{"at": 0, "value": 1}], "digest": "forged"}},
    {"envelope": envelope(), "render_source": {"file": "https://untrusted"}},
    {"envelope": envelope(), "compiled_audio": {"digest": "forged"}},
]


@pytest.mark.parametrize("audio", BAD)
def test_strict_sdk_refuses_nested_malformed_audio(audio):
    value = config()
    value["clips"][0]["audio"] = copy.deepcopy(audio)
    with pytest.raises((ValueError, jsonschema.ValidationError)):
        validate_timeline(value)


@pytest.mark.parametrize("curve", audio_curves.names())
def test_registered_curves_are_authored_without_quantizing_time(curve):
    value = config()
    value["clips"][0]["audio"] = {"envelope": {"keyframes": [{"at": 0.123456789, "value": 1.5, "curve": curve}]}}
    assert canonical_timeline_config(value) == value
