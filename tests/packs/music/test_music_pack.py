"""Music analysis contracts on small deterministic signals."""

from __future__ import annotations

import numpy as np
import pytest

from astrid.core.contracts.errors import AstridError
from astrid.sdk import discover, get_capability, invoke
from astrid.packs.chiptune.actions._midi import Note
from astrid.packs.music.actions._analysis import FRAME_S, align_audio, align_midi, track_beats


def test_midi_alignment_recovers_negative_offset_and_beats_shuffled_baseline() -> None:
    notes = [Note(0.5 + i * 0.7, 0.8 + i * 0.7, 60 + i % 4, 100) for i in range(20)]
    target = np.zeros(round(18 / FRAME_S), np.float32)
    for note in notes:
        target[round((note.start_s - 0.09) / FRAME_S)] = 1
    # A little width models the onset envelope produced by spectral flux.
    target = np.convolve(target, np.exp(-0.5 * (np.arange(-5, 6) / 2) ** 2), mode="same")
    result = align_midi(notes, target, None, 20)
    assert result["offset_s"] == pytest.approx(-0.09, abs=FRAME_S * 1.5)
    assert result["score"] > result["baseline"] + 0.3


def test_audio_alignment_recovers_anchor_mapping() -> None:
    rng = np.random.default_rng(4)
    reference = rng.normal(size=(700, 12)).astype(np.float32)
    reference /= np.linalg.norm(reference, axis=1, keepdims=True)
    target = rng.normal(size=(850, 12)).astype(np.float32)
    target /= np.linalg.norm(target, axis=1, keepdims=True)
    # A 0.975 ratio and +4 s anchor displacement, on the feature grid.
    anchor = 350
    target_anchor = 530
    for i in range(200, 500):
        j = round(target_anchor + (i - anchor) * 0.975)
        target[j] = reference[i]
    result = align_audio(reference, target, anchor * FRAME_S, 5)
    assert result["matched_target_s"] == pytest.approx(target_anchor * FRAME_S, abs=0.15)
    assert result["tempo_ratio"] == pytest.approx(0.975, abs=0.02)
    assert result["confidence"] > 0.3


def test_beat_grid_has_four_beat_downbeats_and_phrase_bars() -> None:
    onset = np.zeros(round(18 / FRAME_S), np.float32)
    for beat in range(32):
        onset[round((1 + beat * 0.5) / FRAME_S)] = 1.0 if beat % 4 == 0 else 0.4
    grid = track_beats(onset)
    assert grid["bpm"] == pytest.approx(120, abs=5)
    assert len(grid["downbeats"]) >= 6
    assert grid["bars"] == grid["downbeats"][::4]
    assert grid["sections"] == []
    assert grid["downbeats"][0] == pytest.approx(1, abs=0.15)


def test_beat_grid_rejects_silence() -> None:
    with pytest.raises(AstridError, match="no beat accents"):
        track_beats(np.zeros(300, np.float32))


def test_music_actions_use_the_existing_v3_action_route_without_dispatch_branches(tmp_path) -> None:
    action_ids = ("music.align", "music.level", "music.beats")
    discovered = {action.id for action in discover(kind="action").actions}
    inputs = {
        "music.align": {"reference": "reference.wav", "target": "target.wav"},
        "music.level": {"audio": "source.wav", "from_s": 0.0, "to_s": 1.0},
        "music.beats": {"audio": "source.wav"},
    }
    for action_id in action_ids:
        capability = get_capability(action_id, kind="action")
        declaration = capability.definition["metadata"]["action_declaration"]
        assert action_id in discovered
        assert capability.capability_type == "executor"
        assert declaration["invocation"]["kind"] == "command"
        assert declaration.get("kind") is None
        result = invoke(action_id, kind="action", inputs=inputs[action_id],
                        out=tmp_path / action_id.split(".")[-1], dry_run=True)
        assert result.capability_id == action_id and result.ok
        assert result.raw_result["dry_run"] is True
