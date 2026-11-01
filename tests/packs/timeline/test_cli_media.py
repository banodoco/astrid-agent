from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.packs.timeline import cli as timeline_cli
from astrid.packs.timeline import cli_sweep
from astrid.sdk.timeline_checkout import Checkout

TINY = Path(__file__).resolve().parents[2] / "fixtures" / "timeline_editing" / "tiny.json"


def _tiny() -> Checkout:
    data = json.loads(TINY.read_text(encoding="utf-8"))
    data["shots"]["A"]["internal_timeline"]["registry"]["assets"]["Q"]["duration"] = 10.0
    return Checkout(data)


def _run(monkeypatch, tl, *argv) -> int:
    monkeypatch.setattr(tl, "save", lambda *_args, **_kwargs: Path("draft.json"))
    monkeypatch.setattr(timeline_cli, "_edit_checkout", lambda _parsed: tl)
    monkeypatch.setattr(cli_sweep, "touched_cuts", lambda *_args: set())
    monkeypatch.setattr(cli_sweep, "auto_sweep_after_edit", lambda *_args, **_kwargs: 0)
    parsed = timeline_cli.build_parser(object()).parse_args(["edit", "--project", "p1", "main", *argv])
    return parsed.handler(parsed)


def test_add_media_adds_a_cut_free_audio_clip_of_the_whole_source(monkeypatch) -> None:
    tl = _tiny()
    assert _run(monkeypatch, tl, "--add-media", "Q", "--track", "music", "--on", "1", "--id", "bed") == 0
    clip = tl.clip("bed")
    assert clip.cut_id is None
    assert clip.start == pytest.approx(1)
    assert (clip.data["from"], clip.data["to"]) == (0, 10)


def test_trim_and_slip_keep_the_film_span(monkeypatch) -> None:
    tl = _tiny()
    # The default keep_end policy extends a source-backed bed to the film end after every edit
    # (see test_a_trimmed_bed_is_extended_to_the_film_end_under_keep_end); this test pins the
    # trim/slip arithmetic, so the bed plays on as authored.
    tl.music = "plays_on"
    assert _run(monkeypatch, tl, "--clip", "a-music", "--trim-in", "1", "--trim-out", "3") == 0
    assert _run(monkeypatch, tl, "--clip", "a-music", "--slip", "2") == 0
    clip = tl.clip("a-music")
    assert clip.start == pytest.approx(0)
    assert clip.duration == pytest.approx(2)
    assert (clip.data["from"], clip.data["to"]) == (3, 5)


def test_a_trimmed_bed_is_extended_to_the_film_end_under_keep_end(monkeypatch) -> None:
    tl = _tiny()
    assert tl.music == "keep_end"
    assert _run(monkeypatch, tl, "--clip", "a-music", "--trim-in", "1", "--trim-out", "3") == 0
    clip = tl.clip("a-music")
    assert clip.end == pytest.approx(tl.duration)  # the bed's source has samples to fill the film
    assert clip.data["from"] == 1


def test_audio_fade_accepts_a_registered_curve_and_persists_it(monkeypatch) -> None:
    tl = _tiny()
    assert _run(monkeypatch, tl, "--clip", "a-music", "--fade-in", "3s", "curve=equal-power") == 0
    clip = tl.clip("a-music")
    assert clip.params["fadeIn"] == 3
    assert clip.params["fadeInCurve"] == "equal-power"
    from astrid.core.timeline import clip_fields as cf

    assert "curve=equal-power" in cf.describe(clip.data, fps=tl.fps)["fade_in"]
    assert _run(monkeypatch, tl, "--clip", "a-music", "--fade-in", "3s") == 0  # no curve token: back to linear
    assert "fadeInCurve" not in tl.clip("a-music").params


def test_unknown_audio_curve_is_refused_and_stored_bad_curves_fail_check() -> None:
    from astrid.sdk.timeline._common import TimelineEditError

    tl = _tiny()
    with pytest.raises(TimelineEditError, match="unknown audio curve"):
        tl.clip("a-music").set_look(fade_in="3s curve=wat")
    clip = tl.clip("a-music")
    clip.params["fadeInCurve"] = "wat"
    assert any("fadeInCurve is invalid" in problem for problem in tl._type_mismatches())


def test_set_refuses_unknown_media_param_without_changing_the_clip(monkeypatch, capsys) -> None:
    import copy

    tl = _tiny()
    before = copy.deepcopy(tl.clip("a-music").data)
    assert _run(monkeypatch, tl, "--clip", "a-music", "--set", "zzbogus=2") == 2
    assert "has no param zzbogus" in capsys.readouterr().err
    assert tl.clip("a-music").data == before


def test_set_accepts_clip_level_volume_on_media(monkeypatch) -> None:
    tl = _tiny()
    assert _run(monkeypatch, tl, "--clip", "a-music", "--set", 'volume="-3dB"') == 0
    clip = tl.clip("a-music")
    assert clip.data["volume"] == 0.7079
    assert "volume" not in clip.params


def test_crossfade_moves_b_and_writes_both_fades(monkeypatch, capsys) -> None:
    import copy

    tl = _tiny()
    a = tl.clip("a-music")
    b_data = copy.deepcopy(a.data)
    b_data.update(id="b-music", at=5.0)
    tl._internal(a.shot_id)["clips"].append(b_data)
    assert _run(monkeypatch, tl, "--crossfade", "a-music", "b-music", "curve=equal-power", "--over", "1") == 0
    b = tl.clip("b-music")
    assert b.start == pytest.approx(3.0)  # A stays 0..4; B moved to overlap it by one second
    assert a.params["fadeOut"] == b.params["fadeIn"] == 1
    assert a.params["fadeOutCurve"] == b.params["fadeInCurve"] == "equal-power"
    assert "moved b-music start" in capsys.readouterr().out
