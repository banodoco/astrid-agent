"""Readable visualize pages (astrid-dev#798): header, legend, silence, numbering, tile words, plan labels."""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.rendering.actions.timeline_visualize import contact_sheet
from astrid.packs.rendering.actions.timeline_visualize.contact_sheet import (
    _tile_caption,
    contact_silence_findings,
    contact_title,
    cut_name,
    static_contact_png,
)
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import (
    _display_title,
    _revision_short,
    provenance_findings,
    short_revision_id,
)
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options
from astrid.packs.rendering.actions.timeline_visualize.motion import audio
from astrid.packs.rendering.actions.timeline_visualize.motion.model import Element


def _element(eid: str, track: str, start: float, end: float, asset: str) -> Element:
    return Element(eid, "media", track, start, end, {}, {"from": 0.0}, audio=True, asset=asset)


def _wav(path: Path, seconds: float, loud: list[tuple[float, float]], rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            t = i / rate
            value = int(12000 * math.sin(2 * math.pi * 440 * t)) if any(a <= t < b for a, b in loud) else 0
            frames += struct.pack("<h", value)
        out.writeframes(bytes(frames))
    return path


# --- 1. header in plain words ---------------------------------------------------------------

def test_header_names_the_film_its_length_cuts_shots_and_short_revision():
    title, facts = contact_title("Rough cut C2", duration_seconds=43.5, cuts=19, shots=9, revision="08dee21c",
                                 tiles=19)
    assert title == "Rough cut C2"
    assert facts == "43.5 s · 19 cuts in 9 shots · revision 08dee21c · one tile per cut"
    _title, thinned = contact_title("Long", duration_seconds=600, cuts=240, shots=None, revision=None, tiles=120,
                                    thinned_from=240)
    assert "120 of 240 cuts shown (thinned to fit)" in thinned


def test_short_revision_and_plain_provenance_findings():
    full = "authoring-parent-revision-08dee21c156b3da1816a656e4c155803edb880b6bb7f3c215b8a2acd1523ae3e"
    assert short_revision_id(full) == "08dee21c"
    snapshot = {"metadata": {"requested_revision_id": full}, "render_run_id": "composed-capture:x", "timeline_id": "T"}
    label = {"status": "uncertified", "label": f"UNCERTIFIED preview (claimed revision {full})"}
    assert _revision_short(label, snapshot) == "08dee21c"
    lines = provenance_findings(snapshot, label, "composed_frame_capture")
    assert lines[0].startswith("REVISION UNCERTIFIED preview") and "no Runtime proof" in lines[0]
    assert lines[1].startswith("SOURCE  composed-capture:x")
    assert _display_title({"timeline_name": "5e9d7eab"}, {"title": "Rough cut C2"}) == "Rough cut C2"
    assert _display_title({"timeline_name": "5e9d7eab"}, {}) == "5e9d7eab"


def test_header_and_options_carry_title_and_shot_plans():
    options = filmstrip_options({"view": "contact", "title": "  Rough   cut C2 ",
                                 "shot_plans": '{"labels": true, "rows": [{"occurrence_id": "o1", "start": 0, '
                                               '"end": 2, "label": "LM+R · planned", "line": true}]}'})
    assert options["title"] == "Rough cut C2"
    assert options["shot_plans"] == {"labels": True, "rows": [
        {"occurrence_id": "o1", "start": 0.0, "end": 2.0, "label": "LM+R · planned", "line": True}]}
    with pytest.raises(ValueError, match="start and end"):
        filmstrip_options({"view": "contact", "shot_plans": {"rows": [{"start": "x"}]}})


# --- 2. silence flagged only where a line is expected ---------------------------------------

def test_silence_is_flagged_only_where_a_line_is_placed(tmp_path):
    # a 3 s logo opening with no audio at all, then a VO clip 3..6 s that is silent 4..5.5 s
    vo = _wav(tmp_path / "vo.wav", 3.0, loud=[(0.0, 1.0), (2.5, 3.0)])
    elements = [_element("v", "vo", 3.0, 6.0, "vo")]
    env = audio.envelope(elements, {"vo": vo}, 0.0, 6.0, bins=300)
    assert env.expected == [(3.0, 6.0)]
    assert any(a <= 0.05 and b >= 2.9 for a, b in env.dead_air())  # the mix IS silent in the opening …
    flagged = env.missing_lines()
    assert len(flagged) == 1 and 3.9 <= flagged[0][0] <= 4.1 and 5.4 <= flagged[0][1] <= 5.6  # … but only the VO gap counts
    lines = contact_silence_findings(env)
    assert any(line.startswith("SILENT  4.") for line in lines)
    assert any(line.startswith("QUIET") and "0.0–3.0s" in line for line in lines)
    assert not any("dead air" in line for line in lines)


def test_scripted_or_planned_lines_count_as_expected():
    spans = audio.expected_line_spans([_element("v", "vo", 3.0, 6.0, "vo")], [(0.0, 2.0), (4.0, 5.0)])
    assert spans == [(0.0, 2.0), (3.0, 6.0)]  # a planned line already covered by a VO clip adds nothing


def test_unreadable_audio_is_unknown_never_silent(tmp_path, monkeypatch):
    bad = tmp_path / "intro.mp4"
    bad.write_bytes(b"not audio")
    monkeypatch.setattr(audio, "_ffmpeg_window", lambda *a, **k: None)
    env = audio.envelope([_element("intro", "vo", 0.0, 4.0, "intro")], {"intro": bad}, 0.0, 4.0, bins=200)
    assert env.unknown == [(0.0, 4.0)]
    assert env.missing_lines() == []
    assert any("audio not readable" in note and "not judged" in note for note in env.notes)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_non_wav_voice_is_decoded_with_ffmpeg(tmp_path):
    wav = _wav(tmp_path / "src.wav", 2.0, loud=[(0.0, 2.0)])
    m4a = tmp_path / "voice.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), str(m4a)], check=True)
    env = audio.envelope([_element("v", "vo", 0.0, 2.0, "v")], {"v": m4a}, 0.0, 2.0, bins=100)
    assert env.unknown == [] and env.level_db("vo", 0.2, 1.8) > -30


# --- 3. one numbering; 4. tile words -------------------------------------------------------

def test_cuts_have_one_name_on_every_page():
    assert cut_name({"index": 8}) == "cut 8"
    assert cut_name({"index": 13, "cut_id": "c09a"}) == "c09a"


def test_tile_words_say_why_they_are_empty():
    assert _tile_caption({"cut": {"say": "You have two options"}}, voice=True) == "You have two options"
    assert _tile_caption({"cut": {}}, voice=False) == "no voice under this cut"
    assert _tile_caption({"cut": {}}, voice=True).startswith("voice here; no words stored")
    assert _tile_caption({"cut": {}}) == "no words stored"


# --- 9. plan labels on the tiles -----------------------------------------------------------

def test_overview_draws_header_legend_wrapped_names_and_plan_labels(tmp_path, monkeypatch):
    cards = []
    for index in range(3):
        name = f"frames/f{index}.png"
        (tmp_path / "frames").mkdir(exist_ok=True)
        Image.new("RGB", (480, 270), (20, 60 + index * 40, 90)).save(tmp_path / name)
        cards.append({"id": f"f{index}", "frame": index * 24, "image": name, "time_seconds": index * 2.0,
                      "cut": {"index": index + 1, "start": index * 2.0, "end": index * 2.0 + 2.0},
                      "clips": [{"shot_name": "02c Blue pill — Morpheus speaks to Neo at length", "occurrence_id": f"o{index}"}],
                      "captions": [], "timed_words": []})
    drawn: list[str] = []
    original = contact_sheet._png_draw_text
    monkeypatch.setattr(contact_sheet, "_png_draw_text",
                        lambda draw, xy, text, font, fill=None: (drawn.append(text), original(draw, xy, text, font, fill=fill))[1])
    pastes = []
    paste = Image.Image.paste
    monkeypatch.setattr(Image.Image, "paste", lambda image, source, box=None, *a, **k: (pastes.append(box), paste(image, source, box, *a, **k))[1])
    occurrences = [{"occurrence_id": f"o{k}", "shot_id": f"s{k}", "shot_name": f"0{k} shot", "start": k * 2.0,
                    "end": k * 2.0 + 2.0} for k in range(3)]
    static_contact_png(cards, tmp_path, columns=10, timeline_name="Rough cut C2", render_run_id="composed-capture:x",
                       render_selection="composed_frame_capture", occurrences=occurrences, duration_seconds=6.0,
                       revision_short="08dee21c", cut_count=3, voice_spans=[(2.0, 4.0)],
                       plan_labels={"o0": "LM+R · planned", "o2": "no plan"})
    assert len(pastes) == 3  # frames only: legend and plan labels are drawn, never pasted
    assert "Rough cut C2" in drawn and "6.0 s · 3 cuts in 3 shots · revision 08dee21c · one tile per cut" in drawn
    assert not any("composed-capture" in text or "UNCERTIFIED" in text or "ch0" in text for text in drawn)
    assert "cut 1  0.00–2.00 s" in drawn
    assert "LM+R · planned" in drawn and "no plan" in drawn
    assert drawn.count("no voice under this cut") == 2 and any(t.startswith("voice here;") for t in drawn)
    names = [t for t in drawn if "Morpheus" in t or "length" in t]
    assert any(t.startswith("02c Blue pill") for t in names) and len(names) >= 2  # wrapped, not cut at one line
