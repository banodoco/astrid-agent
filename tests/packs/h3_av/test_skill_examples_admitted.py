"""The marked skill requests must pass public CPU dry-run admission unchanged."""
from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from PIL import Image

from astrid.core.generation import entries
from astrid.core.generation.preflight import GenerationReadinessError
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.shared.spec import H3SpecError, canonical_spec
from astrid.sdk.generation import generate

pytestmark = pytest.mark.ffmpeg
SKILL = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/docs/SKILL.md"
EXAMPLES = ("temporal-edit", "first-frame-via-still", "regional-mask")


@pytest.fixture
def examples():
    blocks = re.findall(r"```yaml\n# example: ([\w-]+)\n(.*?)\n```", SKILL.read_text(), re.S)
    assert blocks, "no example found in h3 SKILL.md"
    assert len(blocks) == len(EXAMPLES) and {name for name, _ in blocks} == set(EXAMPLES)
    return {name: yaml.safe_load(body) for name, body in blocks}


def _ffmpeg(*args):
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        check=True, capture_output=True, timeout=60,
    ).stdout


@pytest.fixture
def assets(tmp_path, examples):
    # Both the moving source and the still clip have bounded source audio.
    for name, pattern in (("bike.mp4", "testsrc2=s=1024x576:r=24:d=3"),
                          ("bike-still.mp4", "color=c=blue:s=1024x576:r=24:d=3")):
        _ffmpeg("-f", "lavfi", "-i", pattern, "-f", "lavfi", "-i",
                "sine=frequency=440:duration=3", "-t", "3", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", tmp_path / name)
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=1024x576:d=1",
            "-vf", "drawbox=x=384:y=144:w=256:h=288:color=white:t=fill",
            "-frames:v", "1", "-threads", "1", tmp_path / "frame-mask.png")
    return {name: str(tmp_path / name) for name in ("bike.mp4", "bike-still.mp4", "frame-mask.png")}


def _request_file(tmp_path, document):
    path = tmp_path / "request.yaml"
    path.write_text(yaml.safe_dump(document))
    return str(path)


@pytest.mark.parametrize("name", EXAMPLES)
def test_skill_example_is_admitted(name, examples, assets, tmp_path, monkeypatch):
    observed = {}
    real_build = entries.build_entry

    def capture_build(*args, **kwargs):
        # Observe the real hook before the SDK removes its temporary job directory.
        built = real_build(*args, **kwargs)
        observed["graph"] = built["workflow"].compile("api", run_inputs=built["bindings"])
        if "video_mask_0" in built["assets"]:
            observed["mask"] = _ffmpeg(
                "-i", built["assets"]["video_mask_0"], "-t", "3", "-vf", "scale=1:1",
                "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1")
        return built

    monkeypatch.setattr(entries, "build_entry", capture_build)
    result = generate.video(entry="h3", mode="video-edit", placement="runpod", dry_run=True,
                            request=_request_file(tmp_path, examples[name]), asset_map=assets)
    assert result["status"] == "ready", result
    assert result["issues"] == [] and result["fixes"] == []
    graph = observed["graph"]
    native = [node for node in graph.values() if node["class_type"] == "H3V2VGranularFractionalDenoise"]
    assert len(native) == 1
    inputs = native[0]["inputs"]
    assert inputs["mode"] == "spatial" and inputs["crop"] == "disabled"
    assert inputs["outside_strength"] == 0 and inputs["audio_strength"] == 0
    assert graph["99"]["class_type"] == "VHS_LoadVideoFFmpeg"
    assert graph["99"]["inputs"]["frame_load_cap"] == 72
    assert not any(node["class_type"].startswith("LanPaint") for node in graph.values())
    if name == "regional-mask":
        loader = graph[inputs["source_mask"][0]]
        assert loader["class_type"] == "LoadImageMask"
        assert loader["inputs"]["channel"] == "red"
        assert "mask_0" in result["entry"]["bindings"]
        assert "video_mask_0" not in result["entry"]["bindings"]
    else:
        kept = 36 if name == "temporal-edit" else 4
        assert observed["mask"] == bytes(kept) + bytes([255]) * (72 - kept)
        assert "video_mask_0" in result["entry"]["bindings"]
        assert any(node["class_type"] == "ImageToMask" for node in graph.values())


def test_skill_edit_audio_generation_is_rejected(examples, assets, tmp_path):
    document = copy.deepcopy(examples["temporal-edit"])
    document["changes"]["audio"] = [{"during": [1.5, 3.0], "action": "generate"}]
    with pytest.raises(H3SpecError, match="Only video regeneration is supported") as pack_error:
        canonical_spec(normalize_request(document))
    assert pack_error.value.code == "h3.spec.unsupported_op"
    with pytest.raises(GenerationReadinessError, match="Only video regeneration is supported") as public_error:
        generate.video(entry="h3", mode="video-edit", placement="runpod", dry_run=True,
                       request=_request_file(tmp_path, document), asset_map=assets)
    assert public_error.value.code == "input_rejected"


@pytest.mark.parametrize(("filename", "size", "asset"), [
    ("image-guided-speech.json", (512, 288), "speaker.png"),
    ("rectangle-edit.json", (768, 448), "sign.mp4"),
])
def test_dynamic_size_request_file_compiles(filename, size, asset, tmp_path):
    import json

    from PIL import Image
    from astrid.packs.h3_av.shared.compile import compile_preparation
    from astrid.packs.h3_av.shared.prepare import prepare_request

    document = json.loads((SKILL.parent / "examples" / filename).read_text())
    source = tmp_path / asset
    if source.suffix == ".png":
        Image.new("RGB", (64, 64)).save(source)
    else:
        _ffmpeg("-f", "lavfi", "-i", "color=s=768x448:r=24:d=3", "-f", "lavfi",
                "-i", "sine=duration=3", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-shortest", source)

    prepared = prepare_request(normalize_request(document), asset_map={asset: str(source)})
    compiled = compile_preparation(prepared, out_dir=tmp_path / "compiled")
    assert compiled["size"] == {"width": size[0], "height": size[1]}
    if asset == "sign.mp4":
        assert "mask_0" in compiled["workflow_inputs"]


@pytest.mark.parametrize("name", ["start-still-speaker", "mouth-mask-speaker"])
def test_speaker_request_file_is_admitted(name, tmp_path, monkeypatch, capsys):
    request = SKILL.parent / "recipes" / f"{name}.json"
    assert f"(recipes/{name}.json)" in SKILL.read_text()
    start_still = name == "start-still-speaker"
    video = tmp_path / ("speaker-sample.mp4" if start_still else "window-shot.mp4")
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=1024x576:r=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "3" if start_still else "6", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", video)
    assets = {video.name: str(video)}
    if start_still:
        still = tmp_path / "window-portrait.png"
        Image.new("RGB", (1024, 576), "yellow").save(still)
        assets[still.name] = str(still)
    else:
        mouth = tmp_path / "mouth-mask.png"
        mask = Image.new("L", (1024, 576))
        mask.paste(255, (448, 320, 576, 384))
        mask.save(mouth)
        speaker = tmp_path / "speaker-sample.wav"
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=3", speaker)
        assets.update({mouth.name: str(mouth), speaker.name: str(speaker)})

    from astrid.packs.h3_av import cli
    asset_map_path = tmp_path / "asset-map.json"
    asset_map_path.write_text(json.dumps(assets))
    assert cli.main(["plan", "--spec", str(request), "--asset-map", str(asset_map_path),
                     "--out", str(tmp_path / "public-plan")]) == 0
    public_plan = json.loads(capsys.readouterr().out)
    assert public_plan["compilation"]["status"] == "compiled"
    assert public_plan["runtime_submission"] == "eligible"
    assert public_plan["bindings"]["runtime"]

    observed = {}
    real_build = entries.build_entry

    def capture_build(*args, **kwargs):
        built = real_build(*args, **kwargs)
        observed["graph"] = built["workflow"].compile("api", run_inputs=built["bindings"])
        observed["bindings"] = built["bindings"]

        def sample_mask(binding, x, y):
            return _ffmpeg(
                "-i", built["assets"][binding], "-t", "6", "-vf", f"format=gray,crop=1:1:{x}:{y}",
                "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1")

        observed["video_mask_0"] = sample_mask("video_mask_0", 512, 352)
        observed["audio_time_mask"] = sample_mask("audio_time_mask", 0, 0)
        if not start_still:
            observed["outside_mouth"] = sample_mask("video_mask_0", 0, 0)
        return built

    monkeypatch.setattr(entries, "build_entry", capture_build)
    result = generate.video(entry="h3", mode="video-edit", placement="runpod", dry_run=True,
                            request=str(request), asset_map=assets)
    assert result["status"] == "ready" and result["issues"] == [] and result["fixes"] == [], result
    graph, bindings = observed["graph"], observed["bindings"]
    target = next(node["inputs"] for node in graph.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert target["prompt"] == json.loads(request.read_text())["prompt"]
    assert "<Audio 1>" in target["prompt"]
    assert {"source_video", "video_mask_0", "audio_time_mask"} <= set(result["entry"]["bindings"])
    if start_still:
        video_node, slot = target["ref_videos.ref_video_0"]
        assert slot == 0 and target["ref_video_audios.ref_video_audio_0"] == [video_node, 2]
        assert graph[video_node]["inputs"]["video"] == bindings["video_reference_0"]
        assert observed["video_mask_0"] == bytes(1) + bytes([255]) * 143
        assert observed["audio_time_mask"] == bytes([255]) * 144
    else:
        trim = graph[target["ref_audios.ref_audio_0"][0]]
        assert trim["class_type"] == "TrimAudioDuration"
        loader = graph[trim["inputs"]["audio"][0]]
        assert loader["class_type"] == "LoadAudio"
        assert loader["inputs"]["audio"] == bindings["audio_reference_0"]
        window = bytes(48) + bytes([255]) * 48 + bytes(48)
        assert observed["video_mask_0"] == window
        assert observed["outside_mouth"] == bytes(144)
        assert observed["audio_time_mask"] == window


@pytest.mark.parametrize("conflict", ["seed", "guides"])
def test_public_plan_blocks_incompatible_joint_video_grants(conflict, assets, tmp_path, capsys):
    from astrid.packs.h3_av import cli

    edits = [{"stream": "video", "during": [0, 1]}, {"stream": "video", "during": [1, 2]}]
    media = [{"asset": "bike.mp4", "role": "timeline", "modality": "video",
              "at": {"frame": 0}, "range": [0, 3], "edit": edits}]
    if conflict == "seed":
        edits[0]["seed"], edits[1]["seed"] = 1, 2
    else:
        edits[0]["guides"], edits[1]["guides"] = ["a"], ["b"]
        media.extend([{"id": name, "asset": "frame-mask.png", "role": "reference", "modality": "image"}
                      for name in ("a", "b")])
    request = tmp_path / "joint.json"
    request.write_text(json.dumps({"version": 2, "prompt": "Edit", "duration": 3, "media": media}))
    mapping = tmp_path / "assets.json"
    mapping.write_text(json.dumps(assets))
    assert cli.main(["plan", "--spec", str(request), "--asset-map", str(mapping), "--out", str(tmp_path / "plan")]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["runtime_submission"] == "blocked_until_compilation"
    assert "different " + ("seeds" if conflict == "seed" else "reference sets") in str(plan["compilation"])


def test_public_plan_accepts_moving_mask(assets, tmp_path, capsys):
    from astrid.packs.h3_av import cli

    mask = tmp_path / "mask.mkv"
    _ffmpeg("-f", "lavfi", "-i", "nullsrc=s=1024x576:r=24:d=3", "-vf",
            "geq=lum='if(lt(X,512+mod(N,2)*64),255,0)':cb=128:cr=128,format=gray", "-c:v", "ffv1", mask)
    request = tmp_path / "moving.json"
    request.write_text(json.dumps({"version": 2, "prompt": "Edit", "duration": 3, "media": [
        {"asset": "bike.mp4", "role": "timeline", "modality": "video", "at": {"frame": 0},
         "range": [0, 3], "edit": [{"stream": "video", "during": [0, 3], "mask": {"asset": "mask"}}]}]}))
    mapping = tmp_path / "assets.json"
    mapping.write_text(json.dumps({**assets, "mask": str(mask)}))
    assert cli.main(["plan", "--spec", str(request), "--asset-map", str(mapping), "--out", str(tmp_path / "plan")]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["runtime_submission"] == "eligible"
    assert plan["bindings"]["regions"][0]["kind"] == "video"
    assert plan["bindings"]["regions"][0]["frames"] == 72
    assert any(row["binding"].startswith("video_mask") for row in plan["bindings"]["runtime"])
    samples = _ffmpeg("-i", mask, "-vf", "crop=1:1:540:10", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1")
    assert samples[:2] == bytes([0, 255])


def test_near_limit_reference_warnings_and_resources_reach_plan_and_build(assets, tmp_path, capsys):
    import importlib.util
    from astrid.core.generation.engine_facts import comfyui_facts
    from astrid.packs.h3_av import cli

    voice = tmp_path / "voice.wav"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=3", voice)
    asset_map = {**assets, "voice.wav": str(voice)}
    document = {"version": 2, "prompt": "<Video 1> <Audio 1> <Audio 2>", "duration": 3,
                "media": [{"asset": "bike.mp4", "role": "reference", "modality": "video",
                           "range": [0, 2], "audio": True},
                          {"asset": "voice.wav", "role": "reference", "modality": "audio", "range": [0, 2]}]}
    request = tmp_path / "references.json"
    request.write_text(json.dumps(document))
    mapping = tmp_path / "assets.json"
    mapping.write_text(json.dumps(asset_map))
    assert cli.main(["plan", "--spec", str(request), "--asset-map", str(mapping), "--out", str(tmp_path / "plan")]) == 0
    public_plan = json.loads(capsys.readouterr().out)
    hook_path = SKILL.parent.parent / "extensions/vibecomfy.workflows/hooks.py"
    spec = importlib.util.spec_from_file_location("h3_near_limit_hooks", hook_path)
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    built = hooks.build({"request": str(request), "asset_map": asset_map}, {}, tmp_path / "build", engine=comfyui_facts())
    for plan in (public_plan, built["plan"]):
        assert len([row for row in plan["warnings"] if row.get("limit") == "clip_seconds"]) == 2
    refs = built["plan"]["reference_view"]["references"]
    assert {row["modality"]: row["audio_duration_seconds"] for row in refs} == {"video": 2.0, "audio": 2.0}
    assert all("resource_estimate" in row for row in refs)
    assert str(tmp_path) not in json.dumps(built["plan"])


def test_public_plan_and_build_accept_mixed_fixed_and_generated_audio_at_44100(tmp_path, capsys):
    import importlib.util
    from astrid.core.generation.engine_facts import comfyui_facts
    from astrid.packs.h3_av import cli

    source, voice = tmp_path / "source.mkv", tmp_path / "voice.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=1024x576:r=24:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", "-shortest", source)
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=1", voice)
    document = {"version": 2, "duration": 1, "prompt": "Edit", "media": [
        {"asset": "scene", "role": "timeline", "modality": "video", "at": {"frame": 0}, "range": [0, 1],
         "edit": [{"stream": "video", "during": [0, 1], "mask": {"rectangle": [0, 0, 128, 128]}},
                  {"stream": "audio", "during": [0, "1/2"], "guides": ["fixed"]},
                  {"stream": "audio", "during": ["1/2", 1]}]},
        {"id": "fixed", "asset": "voice", "role": "reference", "modality": "audio"}]}
    request, mapping = tmp_path / "mixed.json", tmp_path / "assets.json"
    assets = {"scene": str(source), "voice": str(voice)}
    request.write_text(json.dumps(document))
    mapping.write_text(json.dumps(assets))
    assert cli.main(["plan", "--spec", str(request), "--asset-map", str(mapping), "--out", str(tmp_path / "plan")]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["runtime_submission"] == "eligible"
    assert plan["compilation"]["status"] == "compiled"
    hook_path = SKILL.parent.parent / "extensions/vibecomfy.workflows/hooks.py"
    spec = importlib.util.spec_from_file_location("h3_mixed_audio_hooks", hook_path)
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    built = hooks.build({"request": str(request), "asset_map": assets}, {}, tmp_path / "build", engine=comfyui_facts())
    assert "source_audio_prepared" in built["bindings"]
