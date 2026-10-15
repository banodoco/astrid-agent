"""X-32: the universal clip fields, declared once (clip_fields.yaml)."""

from __future__ import annotations

import dataclasses

import jsonschema
import pytest

from astrid.core.timeline import clip_fields as cf
from astrid.core.timeline import banodoco_schema
from astrid.core.timeline.clip_fields import ClipFieldError, FIELDS, FieldSpec
from astrid.core.timeline.validators.timeline import validate_timeline


def _one_clip(entrance):
    clip = {
        "id": "c1",
        "at": 0,
        "track": "v1",
        "clipType": "text",
        "from": 0,
        "to": 2,
        "text": {"content": "Hi"},
    }
    if entrance is not _MISSING:
        clip["entrance"] = entrance
    return {
        "tracks": [{"id": "v1", "kind": "visual", "label": "V"}],
        "clips": [clip],
    }


_MISSING = object()


@pytest.fixture
def _builtin_text_is_not_an_element(monkeypatch):
    """Keep field-only text cases independent of optional pack asset discovery."""
    from astrid.core.timeline.validators import _type_resolve

    resolve = _type_resolve.is_visual_clip_element

    def classify(clip_type, theme, *args, **kwargs):
        if clip_type == "text":
            return False
        return resolve(clip_type, theme, *args, **kwargs)

    monkeypatch.setattr(_type_resolve, "is_visual_clip_element", classify)
    # These cases contain no transitions; catalog lookup is orthogonal to
    # their shared-schema and clip-field assertions and loads unrelated packs.
    monkeypatch.setattr(banodoco_schema, "_transition_ids", lambda: set())


# ---------------------------------------------------------------- coerce forms


def test_opacity_string_and_number_store_the_same_value():
    assert cf.coerce("opacity", "0.6") == ("opacity", 0.6)
    assert cf.coerce("opacity", 0.6) == ("opacity", 0.6)


@pytest.mark.parametrize("raw", ["1.5", -0.1, "lots", True])
def test_opacity_range_and_type_errors(raw):
    with pytest.raises(ClipFieldError, match="opacity"):
        cf.coerce("opacity", raw)


def test_entrance_fade_8f_at_30fps():
    assert cf.coerce("entrance", "fade 8f") == ("entrance", {"type": "fade", "duration": 0.2667})


def test_entrance_fade_seconds_and_bare_seconds():
    assert cf.coerce("entrance", "fade 0.4s") == ("entrance", {"type": "fade", "duration": 0.4})
    assert cf.coerce("entrance", "fade 0.4") == ("entrance", {"type": "fade", "duration": 0.4})


def test_entrance_durationless_and_dict_and_none():
    assert cf.coerce("entrance", "fade") == ("entrance", {"type": "fade"})
    assert cf.coerce("entrance", {"type": "fade", "duration": 0.4}) == (
        "entrance",
        {"type": "fade", "duration": 0.4},
    )
    assert cf.coerce("entrance", "none") == ("entrance", None)


def test_entrance_unknown_animation_lists_the_valid_entrance_ids():
    with pytest.raises(ClipFieldError) as err:
        cf.coerce("entrance", "wobble 8f")
    message = str(err.value)
    assert "wobble" in message
    assert "fade" in message and "fade-up" in message and "scale-in" in message
    assert "slide-up" not in message  # exit-only
    assert "\n" not in message


def test_exit_only_animation_is_refused_as_entrance():
    with pytest.raises(ClipFieldError, match="slide-up"):
        cf.coerce("entrance", "slide-up 8f")
    assert cf.coerce("exit", "slide-up 8f")[1]["type"] == "slide-up"


def test_entrance_dict_with_unknown_key_refused():
    with pytest.raises(ClipFieldError, match="unknown key"):
        cf.coerce("entrance", {"type": "fade", "durationFrames": 8})


def test_fade_in_visual_is_an_entrance_and_audio_is_params_fadein_seconds():
    assert cf.coerce("fade_in", "8f") == ("entrance", {"type": "fade", "duration": 0.2667})
    assert cf.coerce("fade_in", "8f", audio=True) == ("params.fadeIn", 0.2667)


def test_fade_out_visual_is_exit_and_audio_is_params_fadeout():
    assert cf.coerce("fade_out", "8f") == ("exit", {"type": "fade", "duration": 0.2667})
    assert cf.coerce("fade_out", "0.5", audio=True) == ("params.fadeOut", 0.5)


def test_fade_in_none_removes():
    assert cf.coerce("fade_in", "none") == ("entrance", None)
    assert cf.coerce("fade_in", "none", audio=True) == ("params.fadeIn", None)


def test_visual_field_refused_on_audio_clip():
    with pytest.raises(ClipFieldError, match="visual field"):
        cf.coerce("opacity", "0.5", audio=True)


def test_crop_store_keys_and_range():
    assert cf.coerce("crop_left", 0.1) == ("cropLeft", 0.1)
    assert cf.coerce("crop_top", "0.2") == ("cropTop", 0.2)
    assert cf.coerce("crop_right", 0) == ("cropRight", 0.0)
    with pytest.raises(ClipFieldError):
        cf.coerce("crop_left", 1.2)


def test_crop_sum_rule_in_coerce_many():
    assert cf.coerce_many({"crop_left": 0.5, "crop_right": 0.49}) == {"cropLeft": 0.5, "cropRight": 0.49}
    with pytest.raises(ClipFieldError, match="0.99"):
        cf.coerce_many({"crop_left": 0.6, "crop_right": 0.5})
    with pytest.raises(ClipFieldError, match="0.99"):
        cf.coerce_many({"crop_top": 0.6, "crop_bottom": 0.5})


def test_speed_with_and_without_x_and_its_bounds():
    assert cf.coerce("speed", "0.5x") == ("speed", 0.5)
    assert cf.coerce("speed", "0.5") == ("speed", 0.5)
    assert cf.coerce("speed", 2) == ("speed", 2.0)
    for bad in ("0", "0x", -1, "11x"):
        with pytest.raises(ClipFieldError, match="speed"):
            cf.coerce("speed", bad)


def test_volume_dB_linear_and_mute_forms():
    assert cf.coerce("volume", "-3dB") == ("volume", 0.7079)
    assert cf.coerce("volume", "0.5") == ("volume", 0.5)
    assert cf.coerce("volume", "-inf") == ("volume", 0.0)
    assert cf.coerce("volume", "mute") == ("volume", 0.0)
    assert cf.coerce("volume", "+6dB") == ("volume", 1.9953)


def test_volume_above_plus_12_db_refused():
    with pytest.raises(ClipFieldError, match="12"):
        cf.coerce("volume", "+13dB")


def test_transition_crossfade_dip_cut():
    assert cf.coerce("transition", "crossfade 10f") == (
        "transition",
        {"id": "cross-fade", "durationFrames": 10},
    )
    assert cf.coerce("transition", "dip 8f") == ("transition", {"id": "fade", "durationFrames": 8})
    assert cf.coerce("transition", "cut") == ("transition", None)


def test_transition_unknown_name_refused():
    with pytest.raises(ClipFieldError, match="unknown"):
        cf.coerce("transition", "swirl 8f")


def test_bad_duration_forms_refused():
    with pytest.raises(ClipFieldError, match="duration"):
        cf.coerce("fade_in", "soon")
    with pytest.raises(ClipFieldError, match="positive"):
        cf.coerce("fade_in", "0f")


def test_unknown_field_refused_and_reserved_undeclared_named():
    with pytest.raises(ClipFieldError, match="unknown clip field"):
        cf.coerce("nonsense", 1)
    with pytest.raises(ClipFieldError, match="reserved but not declared"):
        cf.coerce("mute", True)


# ---------------------------------------------------------------- shape


def test_fields_mapping_is_read_only_and_typed():
    assert set(FIELDS) >= {"opacity", "entrance", "exit", "fade_in", "fade_out", "crop_left", "speed", "volume", "transition"}
    assert isinstance(FIELDS["opacity"], FieldSpec)
    with pytest.raises(TypeError):
        FIELDS["zzz"] = None  # type: ignore[index]
    assert set(cf.fields()) == set(FIELDS)


def test_fieldspec_is_frozen_and_methods_match_coerce():
    spec = FIELDS["entrance"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "x"  # type: ignore[misc]
    assert spec.to_stored("fade 8f", fps=30) == cf.coerce("entrance", "fade 8f", fps=30)[1]
    assert spec.from_stored({"type": "fade", "duration": 0.2667}, fps=30) == "fade 8f"


def test_fadein_fieldspec_exposes_visual_and_audio_paths():
    spec = FIELDS["fade_in"]
    assert spec.stored_at == "entrance"
    assert spec.stored_at_audio == "params.fadeIn"
    assert spec.applies_to == "both"
    assert FIELDS["crop_left"].stored_at == "cropLeft"
    assert FIELDS["volume"].stored_at == "volume"


def test_reserved_list_and_is_reserved():
    names = cf.reserved_names()
    assert names >= {"opacity", "fade_in", "fade_out", "entrance", "exit", "crop_top", "crop_right", "crop_bottom", "crop_left", "speed", "freeze", "pan", "rotate", "volume", "mute"}
    assert cf.is_reserved("volume") and cf.is_reserved("mute")
    assert not cf.is_reserved("caption")
    assert set(FIELDS) <= names


def test_every_stored_key_is_a_clip_allowed_key():
    for spec in FIELDS.values():
        top = spec.stored_at.split(".")[0]
        assert top in banodoco_schema._CLIP_ALLOWED, spec.name
        if spec.stored_at_audio:
            assert spec.stored_at_audio.split(".")[0] in banodoco_schema._CLIP_ALLOWED, spec.name


# ---------------------------------------------------------------- read / write


def test_read_returns_stored_values_and_key_paths():
    clip = {"entrance": {"type": "fade", "duration": 0.2667}, "params": {"fadeIn": 0.2667}, "cropLeft": 0.1}
    assert cf.read(clip, "entrance") == {"type": "fade", "duration": 0.2667}
    assert cf.read(clip, "fade_in") == {"type": "fade", "duration": 0.2667}
    assert cf.read(clip, "fade_in", audio=True) == 0.2667
    assert cf.read(clip, "crop_left") == 0.1
    assert cf.read(clip, "opacity") is None


def test_write_visual_entrance_and_read_back():
    clip: dict = {}
    cf.write(clip, "entrance", "fade 8f")
    assert clip == {"entrance": {"type": "fade", "duration": 0.2667}}
    assert cf.read(clip, "entrance") == {"type": "fade", "duration": 0.2667}


def test_write_audio_fade_nested_in_params():
    clip: dict = {"params": {"content": "x"}}
    cf.write(clip, "fade_in", "8f", audio=True)
    assert clip == {"params": {"content": "x", "fadeIn": 0.2667}}
    assert cf.read(clip, "fade_in", audio=True) == 0.2667


def test_write_none_removes_the_key():
    clip = {"entrance": {"type": "fade", "duration": 0.2667}, "params": {"fadeIn": 0.2667}}
    cf.write(clip, "entrance", None)
    assert "entrance" not in clip
    cf.write(clip, "fade_in", "none", audio=True)
    assert "fadeIn" not in clip["params"]
    cf.write(clip, "transition", "cut")
    assert "transition" not in clip


def test_write_refuses_bad_values_and_crop_sum_against_stored_crop():
    clip = {"cropRight": 0.5}
    with pytest.raises(ClipFieldError):
        cf.write(clip, "crop_left", 0.6)
    assert "cropLeft" not in clip
    with pytest.raises(ClipFieldError):
        cf.write({}, "opacity", "2")


def test_write_volume_and_speed_store_plain_values():
    clip: dict = {}
    cf.write(clip, "volume", "-3dB")
    cf.write(clip, "speed", "0.5x")
    assert clip == {"volume": 0.7079, "speed": 0.5}


# ---------------------------------------------------------------- describe


@pytest.mark.parametrize(
    "name, raw, stored, human",
    [
        ("opacity", "0.6", 0.6, "0.6"),
        ("entrance", "fade 8f", {"type": "fade", "duration": 0.2667}, "fade 8f"),
        ("exit", "fade 8f", {"type": "fade", "duration": 0.2667}, "fade 8f"),
        ("crop_left", "0.1", 0.1, "0.1"),
        ("speed", "0.5x", 0.5, "0.5x"),
        ("volume", "-3dB", 0.7079, "-3.0dB"),
        ("transition", "crossfade 10f", {"id": "cross-fade", "durationFrames": 10}, "crossfade 10f"),
        ("transition", "dip 8f", {"id": "fade", "durationFrames": 8}, "dip 8f"),
    ],
)
def test_describe_round_trips_through_coerce(name, raw, stored, human):
    clip: dict = {}
    cf.write(clip, name, raw)
    assert cf.coerce(name, raw)[1] == stored
    assert cf.describe(clip) == {name: human}
    again: dict = {}
    cf.write(again, name, cf.describe(clip)[name])
    assert again == clip


def test_describe_examples_from_the_brief():
    clip = {
        "opacity": 0.6,
        "entrance": {"type": "fade", "duration": 0.2667},
        "volume": 0.7079,
        "cropLeft": 0.1,
        "params": {"fadeOut": 0.5},
    }
    assert cf.describe(clip, fps=30) == {
        "opacity": "0.6",
        "entrance": "fade 8f",
        "volume": "-3.0dB",
        "crop_left": "0.1",
        "fade_out": "15f",
    }


def test_describe_skips_defaults_and_entrance_round_trip_is_stable():
    assert cf.describe({"opacity": 1, "volume": 1, "speed": 1, "cropTop": 0}) == {}
    clip: dict = {}
    cf.write(clip, "entrance", "fade 8f")
    assert cf.describe(clip)["entrance"] == "fade 8f"
    again: dict = {}
    cf.write(again, "entrance", cf.describe(clip)["entrance"])
    assert again == clip


def test_describe_audio_fade_round_trips_with_audio_flag():
    clip: dict = {"params": {}}
    cf.write(clip, "fade_in", "8f", audio=True)
    assert cf.describe(clip) == {"fade_in": "8f"}
    again: dict = {"params": {}}
    cf.write(again, "fade_in", cf.describe(clip)["fade_in"], audio=True)
    assert again == clip


def test_describe_transition_cut_and_mute_round_trip():
    assert cf.describe({"transition": None}) == {}
    clip: dict = {}
    cf.write(clip, "volume", "mute")
    assert cf.describe(clip) == {"volume": "mute"}


# ---------------------------------------------------------------- render status


@pytest.mark.parametrize(
    "kind, name, version, expected",
    [
        ("am-type", "opacity", "0.0.6", "unrendered"),
        ("am-type", "opacity", "0.0.7", "renders"),
        ("am-snap-plate", "entrance", "0.0.6", "unrendered"),
        ("am-snap-plate", "entrance", "0.0.7", "renders"),
        ("am-footage", "speed", "0.0.6", "unrendered"),
        ("am-footage", "speed", "0.0.7", "renders"),
        ("am-footage", "volume", "0.0.6", "unrendered"),
        ("am-footage", "volume", "0.0.7", "renders"),
        ("am-type", "volume", "0.0.7", "unrendered"),
        ("am-footage-x", "volume", "0.0.7", "unrendered"),
        ("media", "opacity", "0.0.6", "renders"),
        ("media", "crop_left", "0.0.6", "renders"),
        ("media", "entrance", "0.0.6", "unrendered"),
        ("media", "entrance", "0.0.7", "renders"),
        ("media", "exit", "0.0.6", "unrendered"),
        ("audio", "volume", "0.0.6", "renders"),
        ("audio", "fade_in", "0.0.6", "renders"),
        ("audio", "opacity", "0.0.7", "unknown"),
        ("media", "nonsense", "0.0.7", "unknown"),
    ],
)
def test_render_status_per_kind_and_version(kind, name, version, expected):
    assert cf.render_status(kind, name, version) == expected


def test_render_status_clip_type_entry_wins_over_element_kind():
    # am-footage has its own volume entry; the generic element entry stays ignored.
    assert cf.render_status("element", "volume", "0.0.7") == "unrendered"
    assert cf.render_status("am-footage", "volume", "0.0.7") == "renders"
    assert cf.render_status("am-footage", "speed", "0.0.7") == "renders"


def test_render_status_defaults_to_pinned_union_composition():
    # This isolated S0 still pins timeline-composition 0.0.6; 0.0.7 remains
    # an explicit compatibility case above, not the receiving default.
    assert cf.render_status("am-type", "opacity") == "unrendered"
    assert cf.render_status("am-type", "opacity", "0.0.7") == "renders"


# ---------------------------------------------------------------- validator


def test_stored_entrance_shape_passes_validate_timeline(_builtin_text_is_not_an_element):
    validate_timeline(_one_clip({"type": "fade", "duration": 0.2667}), strict=False)
    validate_timeline(_one_clip({"type": "fade", "duration": 0.2667}))


@pytest.mark.parametrize(
    "shape",
    [{"id": "fade", "durationFrames": 8}, "fade"],
)
def test_composition_and_string_entrance_shapes_are_refused(shape, _builtin_text_is_not_an_element):
    with pytest.raises((ValueError, jsonschema.ValidationError)):
        validate_timeline(_one_clip(shape), strict=False)


def test_clip_without_entrance_passes_validate_timeline(_builtin_text_is_not_an_element):
    validate_timeline(_one_clip(_MISSING), strict=False)


# ---------------------------------------------------------------- easing (MO-1)


@pytest.mark.parametrize(
    "name, raw, stored",
    [
        ("entrance", "fade 12f ease=out", {"type": "fade", "duration": 0.4, "params": {"easing": "out"}}),
        ("entrance", "fade ease=in-out", {"type": "fade", "params": {"easing": "in-out"}}),
        ("entrance", "fade 0.4s ease=in", {"type": "fade", "duration": 0.4, "params": {"easing": "in"}}),
        ("entrance", "fade 12f ease=step", {"type": "fade", "duration": 0.4, "params": {"easing": "step"}}),
        ("entrance", "fade 12f ease=linear", {"type": "fade", "duration": 0.4}),
        ("entrance", "fade 12f ease=inOut", {"type": "fade", "duration": 0.4, "params": {"easing": "in-out"}}),
        ("entrance", "fade 12f ease=in_out", {"type": "fade", "duration": 0.4, "params": {"easing": "in-out"}}),
        ("entrance", "fade 12f ease=OUT", {"type": "fade", "duration": 0.4, "params": {"easing": "out"}}),
        ("exit", "fade 12f ease=in", {"type": "fade", "duration": 0.4, "params": {"easing": "in"}}),
        ("entrance", {"type": "fade", "duration": 0.4, "params": {"easing": "out"}},
         {"type": "fade", "duration": 0.4, "params": {"easing": "out"}}),
        ("entrance", {"type": "fade", "params": {"easing": "inOut"}}, {"type": "fade", "params": {"easing": "in-out"}}),
        ("entrance", {"type": "fade", "params": {"easing": "linear"}}, {"type": "fade"}),
        ("fade_in", "8f ease=out", {"type": "fade", "duration": 0.2667, "params": {"easing": "out"}}),
        ("fade_out", "8f ease=in", {"type": "fade", "duration": 0.2667, "params": {"easing": "in"}}),
        ("fade_in", "ease=step", {"type": "fade", "params": {"easing": "step"}}),
    ],
)
def test_easing_forms_store_params_only_when_not_linear(name, raw, stored):
    path = {"entrance": "entrance", "exit": "exit", "fade_in": "entrance", "fade_out": "exit"}[name]
    assert cf.coerce(name, raw) == (path, stored)


@pytest.mark.parametrize("alias, canonical", [
    ("ease-in", "in"), ("ease-out", "out"), ("ease-in-out", "in-out"),
])
@pytest.mark.parametrize("structured", [False, True])
def test_easing_explicit_spec_aliases(alias, canonical, structured):
    raw = {"type": "fade", "params": {"easing": alias}} if structured else f"fade ease={alias}"
    assert cf.coerce("entrance", raw) == ("entrance", {"type": "fade", "params": {"easing": canonical}})


@pytest.mark.parametrize(
    "name, raw",
    [
        ("entrance", "fade 12f ease=bounce"),
        ("entrance", "fade 12f ease="),
        ("entrance", {"type": "fade", "params": {"easing": "elastic"}}),
        ("fade_in", "8f ease=wobble"),
    ],
)
def test_bad_easing_refused_with_the_allowed_list(name, raw):
    with pytest.raises(ClipFieldError) as err:
        cf.coerce(name, raw)
    assert "linear, in, out, in-out, step" in str(err.value)


def test_easing_token_count_and_duplicates_refused():
    with pytest.raises(ClipFieldError, match="ease="):
        cf.coerce("entrance", "fade 12f ease=out ease=in")
    with pytest.raises(ClipFieldError, match="8f or '8f ease=out'"):
        cf.coerce("fade_in", "8f ease=out extra")
    with pytest.raises(ClipFieldError, match="must be '<animation> <duration> \\[ease=E\\]'"):
        cf.coerce("entrance", "fade 12f ease=out extra")


def test_easing_is_refused_on_audio_fades():
    with pytest.raises(ClipFieldError, match="linear volume ramp"):
        cf.coerce("fade_in", "8f ease=out", audio=True)
    with pytest.raises(ClipFieldError, match="linear volume ramp"):
        cf.write({"params": {}}, "fade_out", "8f ease=in", audio=True)


def test_params_easing_is_kept_alongside_other_params():
    stored = cf.coerce("entrance", {"type": "fade-up", "duration": 0.4, "params": {"easing": "out", "distance": 60}})
    assert stored[1] == {"type": "fade-up", "duration": 0.4, "params": {"easing": "out", "distance": 60}}


@pytest.mark.parametrize(
    "name, raw, human",
    [
        ("entrance", "fade 12f ease=out", "fade 12f ease=out"),
        ("entrance", "fade ease=in-out", "fade ease=in-out"),
        ("exit", "fade 12f ease=step", "fade 12f ease=step"),
        ("entrance", "fade 12f ease=linear", "fade 12f"),
    ],
)
def test_easing_describe_round_trips(name, raw, human):
    clip: dict = {}
    cf.write(clip, name, raw)
    described = cf.describe(clip)
    assert described[name] == human
    again: dict = {}
    cf.write(again, name, described[name])
    assert again == clip


@pytest.mark.parametrize(
    "name, raw, key, human",
    [
        ("fade_in", "8f ease=out", "entrance", "fade 8f ease=out"),
        ("fade_out", "8f ease=in", "exit", "fade 8f ease=in"),
    ],
)
def test_visual_fade_with_easing_is_described_as_its_entrance_or_exit(name, raw, key, human):
    clip: dict = {}
    cf.write(clip, name, raw)
    assert cf.describe(clip) == {key: human}
    again: dict = {}
    cf.write(again, name, raw)
    assert again == clip
    cf.write(again, key, human)  # the described form writes the same stored field
    assert again == clip


def test_legacy_inout_is_described_as_in_out_and_round_trips():
    clip = {"entrance": {"type": "fade", "duration": 0.4, "params": {"easing": "inOut"}}}
    assert cf.describe(clip) == {"entrance": "fade 12f ease=in-out"}
    again: dict = {}
    cf.write(again, "entrance", "fade 12f ease=in-out")
    assert again == {"entrance": {"type": "fade", "duration": 0.4, "params": {"easing": "in-out"}}}


def test_easing_stored_shapes_pass_validate_timeline(_builtin_text_is_not_an_element):
    for shape in (
        {"type": "fade", "duration": 0.4, "params": {"easing": "out"}},
        {"type": "fade", "params": {"easing": "step"}},
        {"type": "fade", "duration": 0.4, "params": {"easing": "in-out"}},
    ):
        validate_timeline(_one_clip(shape), strict=False)
        validate_timeline(_one_clip(shape))
