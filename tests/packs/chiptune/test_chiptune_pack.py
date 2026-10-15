from __future__ import annotations

import hashlib
import json
import re
import wave
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from astrid.core.contracts.errors import AstridError
from astrid.core.pack.validate import validate_pack
from astrid.packs.chiptune.actions import _synth as syn
from astrid.packs.chiptune.actions.compose import run as compose_run
from astrid.packs.chiptune.actions.sfx import run as sfx_run
from astrid.sdk import get_capability
from astrid.sdk.media_handles import resolve_media_handle
from astrid.sdk.results import InvocationResult

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_ROOT = REPO_ROOT / "astrid" / "packs" / "chiptune"
SR = 48_000
SFX_KINDS = ("stamp", "flip", "blip", "whoosh", "chime", "error", "coin", "typewriter_tick", "wipe", "thud")
KEYWORD = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _read_wav(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as handle:
        channels, rate, frames = handle.getnchannels(), handle.getframerate(), handle.getnframes()
        raw = handle.readframes(frames)
    data = np.frombuffer(raw, dtype="<i2").reshape(-1, channels).astype(np.float64) / 32768.0
    return data.T, rate, channels


def _compose(tmp_path: Path, name: str, *, duration_s: float = 20.0, bpm: float = 132,
             key: str = "A minor", seed: int = 7, sections=None, hits: str | None = None,
             duck: str | None = None, vo_mask=None, mutes=None, duck_db: float = -9.0,
             master_db: float = -16.0, style: str = "nes") -> Path:
    out = tmp_path / name
    if sections is None:
        sections = [{"start_s": 0, "end_s": 10, "energy": 0.6, "mood": "bright"},
                    {"start_s": 10, "end_s": 20, "energy": 0.9, "mood": "tense"}]
    argv = ["--duration-s", str(duration_s), "--bpm", str(bpm), "--key", key, "--seed", str(seed),
            "--sections", json.dumps(sections), "--master-db", str(master_db), "--duck-db", str(duck_db),
            "--style", style, "--out", str(out)]
    if hits is not None:
        argv += ["--hits", hits]
    if duck is not None:
        argv += ["--duck", duck]
    if vo_mask is not None:
        argv += ["--vo-mask", json.dumps(vo_mask)]
    if mutes is not None:
        argv += ["--mutes", json.dumps(mutes)]
    assert compose_run.main(argv) == 0
    return out


def _sfx(tmp_path: Path, name: str, *extra: str) -> Path:
    out = tmp_path / name
    assert sfx_run.main([*extra, "--out", str(out)]) == 0
    return out


def _tempo_beat_level(mono: np.ndarray, hop: int = 240) -> float:
    """Onset-flux autocorrelation at 5 ms frames, beat prior 90-180 BPM, parabolic refinement."""
    win = np.hanning(1024)
    frames = 1 + (len(mono) - 1024) // hop
    idx = np.arange(1024)[None, :] + hop * np.arange(frames)[:, None]
    mag = np.abs(np.fft.rfft(mono[idx] * win[None, :], axis=1))
    env = np.maximum(np.diff(np.log1p(100.0 * mag), axis=0), 0).sum(axis=1)
    env = env - env.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    ac = ac / ac[0]
    fps = SR / hop
    bpm = 60.0 * fps / np.maximum(np.arange(len(ac)).astype(float), 1.0)
    valid = (bpm >= 90) & (bpm <= 180)
    valid[0] = False
    best = int(np.argmax(np.where(valid, ac, -np.inf)))
    a, b, c = ac[best - 1], ac[best], ac[best + 1]
    denom = a - 2 * b + c
    shift = 0.5 * (a - c) / denom if denom else 0.0
    return 60.0 * fps / (best + shift)


def _edge_jump(x: np.ndarray) -> float:
    """Value at the very first and last sample, relative to the peak.

    Silence sits on both sides of a one-shot, so a hard start or stop shows as a value
    near 1 here. A 10 ms ramp keeps it at zero.
    """
    peak = max(float(np.max(np.abs(x))), 1e-12)
    return float(max(abs(x[0]), abs(x[-1])) / peak)


# ---------------------------------------------------------------------------
# manifests, action dependencies and parsing
# ---------------------------------------------------------------------------


def test_pack_validates_statically() -> None:
    errors, _warnings = validate_pack(PACK_ROOT)
    assert errors == []


def test_actions_declare_ports_and_keyword_rules() -> None:
    pack = yaml.safe_load((PACK_ROOT / "pack.yaml").read_text())
    for slug, outputs in (("compose", {"music", "beats"}), ("sfx", {"sfx", "sfx_meta", "sfx_batch"})):
        action = pack["actions"][slug]
        assert {port["name"] for port in action["outputs"]} == outputs
        assert action["metadata"]["runtime_module"] == f"astrid.packs.chiptune.actions.{slug}.run"
        assert action["metadata"]["output_result_manifest"] is True
        assert action["isolation"]["network"] is False
        assert len(action["description"]) <= 500
        assert len(action["short_description"]) <= 120
        for keyword in action["keywords"]:
            assert KEYWORD.fullmatch(keyword), keyword
    assert len(pack["description"]) <= 500
    for keyword in pack["keywords"]:
        assert KEYWORD.fullmatch(keyword), keyword


def test_pack_declares_cpu_numpy_dependency() -> None:
    pack = yaml.safe_load((PACK_ROOT / "pack.yaml").read_text())
    assert pack["dependencies"]["python_requirements"] == ["numpy"]
    assert all(
        action["isolation"]["network"] is False
        for action in pack["actions"].values()
    )


def test_structured_inputs_use_v3_json_command_bindings() -> None:
    pack = yaml.safe_load((PACK_ROOT / "pack.yaml").read_text())
    compose = pack["actions"]["compose"]
    command = compose["invocation"]["command"]
    assert "{sections}" not in command["argv"]
    mappings = {item["input"]: item for item in command["input_args"]}
    assert mappings["sections"] == {"input": "sections", "flag": "--sections", "optional": False}
    for name in ("sections", "hits", "duck", "vo_mask", "mutes"):
        port = next(item for item in compose["inputs"] if item["name"] == name)
        assert port["type"] == "json"


def test_structured_inputs_survive_runner_joining() -> None:
    assert syn.parse_number_list("4.5,60.0", "hits") == [4.5, 60.0]
    assert syn.parse_number_list("[1, 2.5]", "hits") == [1.0, 2.5]
    assert syn.parse_structured("[[2.0, 9.5]]", "vo_mask") == [[2.0, 9.5]]
    assert syn.parse_key("F# major") == (6, "major")
    assert syn.parse_key("Bb minor") == (10, "minor")
    with pytest.raises(AstridError):
        syn.parse_key("H dorian")
    with pytest.raises(AstridError):
        syn.validate_sections([{"start_s": 0, "end_s": 5, "energy": 0.5, "mood": "frantic"}], 10)
    with pytest.raises(AstridError):
        syn.validate_sections([{"start_s": 0, "end_s": 6, "energy": 0.5, "mood": "bright"},
                               {"start_s": 5, "end_s": 9, "energy": 0.5, "mood": "tense"}], 10)


# ---------------------------------------------------------------------------
# chiptune.compose
# ---------------------------------------------------------------------------


def test_compose_same_seed_is_byte_identical(tmp_path: Path) -> None:
    first = _compose(tmp_path, "a")
    second = _compose(tmp_path, "b")
    assert (first / "music.wav").read_bytes() == (second / "music.wav").read_bytes()
    assert (first / "beats.json").read_bytes() == (second / "beats.json").read_bytes()
    other = _compose(tmp_path, "c", seed=8)
    assert (first / "music.wav").read_bytes() != (other / "music.wav").read_bytes()


def test_compose_admission_assets_and_sdk_media_handles(tmp_path: Path) -> None:
    capability = get_capability("chiptune.compose", kind="action")
    declaration = capability.definition["metadata"]["action_declaration"]
    assert capability.id == "chiptune.compose" and capability.capability_type == "executor"
    assert declaration["kind"] == "generator"
    assert declaration["invocation"]["kind"] == "command"
    assert all(output["type"] == "file" and output.get("path_template")
               for output in declaration["outputs"])
    declared_inputs = {port.name: port.type for port in capability.inputs}
    declared_outputs = {port.name: port.artifact_type for port in capability.outputs}
    assert declared_inputs == {
        "duration_s": "number", "bpm": "integer", "key": "string", "seed": "integer",
        "sections": "json", "hits": "json", "duck": "json", "vo_mask": "json",
        "mutes": "json", "duck_db": "number", "master_db": "number", "style": "string",
    }
    assert declared_outputs == {"music": "audio", "beats": "application/json"}

    inputs = {
        "duration_s": 2.0,
        "bpm": 132,
        "key": "A minor",
        "seed": 17,
        "sections": [{"start_s": 0, "end_s": 2, "energy": 0.4, "mood": "bright"}],
        "hits": [],
        "duck": [],
        "vo_mask": [[0.5, 0.75]],
        "mutes": [[1.25, 1.5]],
        "duck_db": -10.0,
        "master_db": -16.0,
        "style": "nes",
    }
    assert set(declared_inputs) == set(inputs)
    compose_inputs = {
        **inputs,
        "hits": json.dumps(inputs["hits"]),
        "duck": json.dumps(inputs["duck"]),
    }
    first = _compose(tmp_path, "generator-first", **compose_inputs)
    second = _compose(tmp_path, "generator-second", **compose_inputs)
    for filename in ("music.wav", "beats.json"):
        assert (first / filename).read_bytes() == (second / filename).read_bytes()

    manifest = json.loads((first / "manifest.json").read_text())
    emitted = {output["name"]: output for output in manifest["outputs"]}
    assert {name: emitted[name]["artifact_type"] for name in emitted} == declared_outputs
    assert emitted["music"]["media_type"] == "audio/wav"
    assert emitted["beats"]["media_type"] == "application/json"

    digests = {
        name: "sha256:" + hashlib.sha256((first / emitted[name]["path"]).read_bytes()).hexdigest()
        for name in emitted
    }
    managed_outputs = [
        {
            "run_id": "fixture-run",
            "task_id": "fixture-task",
            "output_port": name,
            "ordinal": 0,
            "role": emitted[name]["role"],
            "digest": digests[name],
            "filename": Path(emitted[name]["path"]).name,
            "media_type": emitted[name]["media_type"],
            "size": (first / emitted[name]["path"]).stat().st_size,
        }
        for name in ("music", "beats")
    ]
    result = InvocationResult(
        "chiptune.compose", "executor", "external", True,
        run_id="fixture-run", outputs={"managed_outputs": managed_outputs},
    )
    printed = str(result)
    assert "run:fixture-run/music#0" in printed
    assert "run:fixture-run/beats#0" in printed

    def show(project: str, digest: str) -> SimpleNamespace:
        assert project == "generator-scratch"
        assert digest in digests.values()
        name = next(name for name, value in digests.items() if value == digest)
        return SimpleNamespace(ok=True, data={
            "media_type": emitted[name]["media_type"],
            "filename": Path(emitted[name]["path"]).name,
            "size": (first / emitted[name]["path"]).stat().st_size,
        })

    for name in ("music", "beats"):
        descriptor, lineage = resolve_media_handle(
            SimpleNamespace(media=SimpleNamespace(show=show)),
            "generator-scratch",
            result.output(name),
        )
        assert descriptor["digest"] == digests[name]
        assert lineage["handle"] == f"run:fixture-run/{name}#0"


def test_compose_duration_is_exact_stereo_48k(tmp_path: Path) -> None:
    out = _compose(tmp_path, "d", duration_s=23.37)
    data, rate, channels = _read_wav(out / "music.wav")
    assert (rate, channels) == (SR, 2)
    assert data.shape[1] == round(23.37 * SR)
    assert json.loads((out / "beats.json").read_text())["duration_s"] == pytest.approx(23.37, abs=1e-6)


def test_compose_no_clipping_rms_target_and_no_dc(tmp_path: Path) -> None:
    out = _compose(tmp_path, "e", hits="5.0,12.25")
    data, _, _ = _read_wav(out / "music.wav")
    peak_db, rms_db = syn.measure_db(data)
    assert peak_db < -1.0
    assert -16.5 <= rms_db <= -15.5
    assert np.all(np.abs(data.mean(axis=1)) < 1e-4)
    manifest = json.loads((out / "manifest.json").read_text())
    primary = next(item for item in manifest["outputs"] if item.get("is_primary"))
    assert primary["name"] == "music" and primary["role"] == "result"
    assert {item["name"] for item in manifest["outputs"]} == {"music", "beats"}


def test_compose_beat_grid_and_exact_section_edges(tmp_path: Path) -> None:
    out = _compose(tmp_path, "f")
    beats = json.loads((out / "beats.json").read_text())
    bar_s = 4 * 60 / 132
    assert beats["bpm"] == 132
    assert len(beats["beats"]) == 44  # two sections of 10 s, each with its own beat grid
    # Each section starts its own bar grid: downbeats sit on the section start plus whole bars.
    for origin in (0.0, 10.0):
        local = [d - origin for d in beats["downbeats"] if origin <= d < origin + 10.0 - 1e-9]
        assert all(abs(d / bar_s - round(d / bar_s)) < 1e-4 for d in local)
    sections = beats["sections"]
    assert sections[0]["start_s"] == 0.0 and sections[-1]["end_s"] == pytest.approx(20.0)
    assert sections[0]["end_s"] == sections[1]["start_s"] == 10.0
    for sec in sections:
        assert sec["peak_dbfs"] < -1.0
        assert sec["rms_dbfs"] > -60.0


def test_compose_edges_are_exact_and_rest_is_one_bar(tmp_path: Path) -> None:
    """A one-bar rest before a chapter edge, cut exactly at the requested time."""
    bar_s = 4 * 60 / 132
    rest_end = 12.0
    rest_start = rest_end - bar_s
    sections = [{"start_s": 0, "end_s": rest_start, "energy": 0.8, "mood": "tense"},
                {"start_s": rest_start, "end_s": rest_end, "energy": 0.0, "mood": "silent"},
                {"start_s": rest_end, "end_s": 20, "energy": 0.8, "mood": "triumphant"}]
    out = _compose(tmp_path, "rest", duration_s=20.0, sections=sections)
    data, _, _ = _read_wav(out / "music.wav")
    gap = data[:, int((rest_start + 0.02) * SR): int((rest_end - 0.02) * SR)]
    assert syn.measure_db(gap)[0] < -100.0
    # The tense section stops at its edge: nothing rings across the cut.
    tail = data[:, int((rest_start - 0.2) * SR): int(rest_start * SR) + 1]
    assert _edge_jump(tail[0]) < 0.1
    assert syn.measure_db(data[:, int((rest_end + 0.3) * SR): int((rest_end + 1.0) * SR)])[1] > -40.0
    beats = json.loads((out / "beats.json").read_text())
    assert [s["start_s"] for s in beats["sections"]] == pytest.approx([0.0, rest_start, rest_end], abs=1e-6)


def test_compose_cadence_resolves_to_tonic(tmp_path: Path) -> None:
    sections = [{"start_s": 0, "end_s": 12, "energy": 0.45, "mood": "wistful", "cadence_bars": 3}]
    out = _compose(tmp_path, "cad", duration_s=12.0, sections=sections)
    chords = json.loads((out / "beats.json").read_text())["sections"][0]["chords"]
    assert chords[-3:] == ["E", "Am", "Am"]


def test_compose_typed_hits_and_bed_return_on_long_gap(tmp_path: Path) -> None:
    hits = json.dumps([4.0, {"t": 8.0, "kind": "thud"}])
    out = _compose(tmp_path, "hits", duration_s=12.0, hits=hits,
                   vo_mask=[[1.0, 3.0], [3.5, 5.0]])  # gap 0.5 s
    beats = json.loads((out / "beats.json").read_text())
    assert beats["hits"] == [{"t": 4.0, "kind": "stab"}, {"t": 8.0, "kind": "thud"}]
    with pytest.raises(AstridError):
        syn.normalize_hits([{"t": 4.0, "kind": "gong"}], 12.0)
    with pytest.raises(AstridError):
        syn.normalize_hits([13.0], 12.0)


def test_mute_is_exact_silence_and_accents_still_sound(tmp_path: Path) -> None:
    """A hard mute silences the bed exactly; an accent placed inside it still sounds."""
    out = tmp_path / "mute"
    sections = [{"start_s": 0, "end_s": 12, "energy": 0.8, "mood": "tense"}]
    argv = ["--duration-s", "12.0", "--bpm", "132", "--key", "A minor", "--seed", "7",
            "--sections", json.dumps(sections), "--hits", json.dumps([{"t": 5.0, "kind": "blip"}]),
            "--mutes", json.dumps([[4.0, 6.0]]), "--out", str(out)]
    assert compose_run.main(argv) == 0
    data, _, _ = _read_wav(out / "music.wav")
    assert syn.measure_db(data[:, int(4.03 * SR): int(4.97 * SR)])[0] < -100.0
    assert syn.measure_db(data[:, int(5.0 * SR): int(5.2 * SR)])[0] > -40.0
    beats = json.loads((out / "beats.json").read_text())
    assert beats["mutes"] == [[4.0, 6.0]]
    assert syn.normalize_hits([{"t": 1.0, "kind": "blip"}], 2.0) == [(1.0, "blip")]


def test_duck_returns_to_full_in_gaps_of_point_six_seconds() -> None:
    gain = syn._duck_gain(int(20 * SR), [(2.0, 4.0, -10.0), (4.6, 6.0, -10.0), (9.0, 10.0, -10.0)])
    assert gain is not None
    ducked = 10 ** (-10 / 20)
    assert gain[int(3.0 * SR)] == pytest.approx(ducked, rel=1e-3)
    # 0.6 s gap: the release (0.30 s) and the next attack (0.12 s) fit, so the bed reaches full between.
    assert gain[int(4.3 * SR)] == pytest.approx(1.0, rel=1e-3)
    assert gain[int(15.0 * SR)] == pytest.approx(1.0)


def test_compose_tempo_matches_bpm(tmp_path: Path) -> None:
    out = _compose(tmp_path, "g", duration_s=30.0, sections=[{"start_s": 0, "end_s": 30, "energy": 0.8, "mood": "bright"}])
    data, _, _ = _read_wav(out / "music.wav")
    measured = _tempo_beat_level(data.mean(axis=0))
    assert abs(measured - 132.0) / 132.0 < 0.01, measured


def test_compose_silent_section_is_silent(tmp_path: Path) -> None:
    sections = [{"start_s": 0, "end_s": 8, "energy": 0.7, "mood": "tense"},
                {"start_s": 8, "end_s": 16, "energy": 0.5, "mood": "silent"},
                {"start_s": 16, "end_s": 24, "energy": 0.7, "mood": "wistful"}]
    out = _compose(tmp_path, "h", duration_s=24.0, sections=sections)
    data, _, _ = _read_wav(out / "music.wav")
    assert syn.measure_db(data[:, int(8.5 * SR): int(15.0 * SR)])[1] < -100.0
    beats = json.loads((out / "beats.json").read_text())
    assert [s["mood"] for s in beats["sections"]] == ["tense", "silent", "wistful"]


def test_compose_vo_mask_ducks_music(tmp_path: Path) -> None:
    base, ducked = _compose(tmp_path, "i0"), _compose(tmp_path, "i1", vo_mask=[[6.0, 12.0]], duck_db=-9.0)
    open_, _, _ = _read_wav(base / "music.wav")
    duck, _, _ = _read_wav(ducked / "music.wav")
    inside, outside = slice(int(7.0 * SR), int(11.0 * SR)), slice(int(13.0 * SR), int(19.0 * SR))
    drop_ducked = syn.measure_db(duck[:, inside])[1] - syn.measure_db(duck[:, outside])[1]
    drop_open = syn.measure_db(open_[:, inside])[1] - syn.measure_db(open_[:, outside])[1]
    assert drop_ducked - drop_open == pytest.approx(-9.0, abs=2.0)


def test_compose_file_edges_are_ramped(tmp_path: Path) -> None:
    out = _compose(tmp_path, "j")
    data, _, _ = _read_wav(out / "music.wav")
    assert _edge_jump(data[0]) < 1e-3 and _edge_jump(data[1]) < 1e-3


# ---------------------------------------------------------------------------
# click detector on envelope edges (positive and negative controls)
# ---------------------------------------------------------------------------


def test_click_detector_catches_unramped_control_and_passes_voices() -> None:
    n = int(0.2 * SR)
    control = 0.7 * np.sign(np.cos(2 * np.pi * 440.0 * np.arange(n) / SR))  # hard start at full level
    assert _edge_jump(control) > 0.9  # positive control: the detector flags a hard edge
    for kind in SFX_KINDS:
        sig = syn.render_sfx(kind, variant=3)
        assert _edge_jump(sig) < 1e-3, kind  # negative control: every one-shot ramps in and out


def test_ten_millisecond_ramp_is_monotone_and_zero_ended() -> None:
    n = int(0.2 * SR)
    ramp = int(0.010 * SR)
    env = syn._ramped(n)
    assert env[0] == 0.0 and env[-1] == 0.0
    assert np.all(np.diff(env[:ramp]) >= 0) and np.all(np.diff(env[-ramp:]) <= 0)
    assert env[ramp] == pytest.approx(1.0)
    note = syn._oscillate("pulse", 0.25, np.full(n, 440.0)) * env
    assert _edge_jump(note) < 1e-3


# ---------------------------------------------------------------------------
# chiptune.sfx
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", SFX_KINDS)
def test_sfx_one_shot_is_exact_mono_and_clean(tmp_path: Path, kind: str) -> None:
    out = _sfx(tmp_path, kind, "--kind", kind, "--variant", "4")
    data, rate, channels = _read_wav(out / "sfx.wav")
    assert (rate, channels) == (SR, 1)
    assert data.shape[1] == round(syn.SFX_DEFAULT_SECONDS[kind] * SR)
    assert syn.measure_db(data)[0] < -1.0
    assert abs(float(data.mean())) < 1e-3
    meta = json.loads((out / "sfx.json").read_text())
    assert meta["kind"] == kind and meta["sample_count"] == data.shape[1]
    manifest = json.loads((out / "manifest.json").read_text())
    primary = next(item for item in manifest["outputs"] if item.get("is_primary"))
    assert primary["name"] == "sfx" and primary["role"] == "result"


def test_sfx_exact_duration_variant_and_pitch(tmp_path: Path) -> None:
    a = _sfx(tmp_path, "a", "--kind", "coin", "--variant", "1", "--duration-s", "0.5")
    b = _sfx(tmp_path, "b", "--kind", "coin", "--variant", "1", "--duration-s", "0.5")
    c = _sfx(tmp_path, "c", "--kind", "coin", "--variant", "2", "--duration-s", "0.5")
    d = _sfx(tmp_path, "d", "--kind", "coin", "--variant", "1", "--duration-s", "0.5", "--pitch", "5")
    assert (a / "sfx.wav").read_bytes() == (b / "sfx.wav").read_bytes()
    assert (a / "sfx.wav").read_bytes() != (c / "sfx.wav").read_bytes()
    assert (a / "sfx.wav").read_bytes() != (d / "sfx.wav").read_bytes()
    assert _read_wav(a / "sfx.wav")[0].shape[1] == round(0.5 * SR)


def test_sfx_batch_zip_is_deterministic_and_complete(tmp_path: Path) -> None:
    kinds = ",".join(SFX_KINDS)
    first = _sfx(tmp_path, "x", "--kinds", kinds, "--variant", "1")
    second = _sfx(tmp_path, "y", "--kinds", kinds, "--variant", "1")
    assert (first / "sfx-batch.zip").read_bytes() == (second / "sfx-batch.zip").read_bytes()
    with zipfile.ZipFile(first / "sfx-batch.zip") as archive:
        names = archive.namelist()
    assert len(names) == 2 * len(SFX_KINDS)
    assert sum(name.endswith(".wav") for name in names) == len(SFX_KINDS)
    manifest = json.loads((first / "manifest.json").read_text())
    primary = next(item for item in manifest["outputs"] if item.get("is_primary"))
    assert primary["name"] == "sfx_batch" and primary["role"] == "result"


def test_sfx_rejects_unknown_kind() -> None:
    with pytest.raises(AstridError):
        syn.render_sfx("laser")
