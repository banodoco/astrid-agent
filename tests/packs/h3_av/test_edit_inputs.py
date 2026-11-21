"""Masked-edit inputs: reference images, replacement audio, and their combination.

Requests without the new inputs keep their graph, inputs and bundle bytes
(checked against fixtures recorded from the pre-change tree). A request with
reference images adds one ``reference_capacity`` manifest key and nothing else.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import wave
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from astrid.packs.h3_av.shared import prepare
from astrid.packs.h3_av.shared.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.generation import DEFAULT_SIZE
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.shared.request_v2 import H3RequestError as NativeV2Error, normalize_v2
from tests.packs.h3_av.test_native_masked_edit import PILL, WATERMARK, _prepare

FIXTURES = Path(__file__).parent / "fixtures" / "edit_inputs"
SPAN = [1.5, 3.5]  # seconds inside the 5 s source window

LENS = {
    "version": 2, "duration": 5,
    "prompt": "Old man's face reflected in the sunglasses lenses; hands and background unchanged.",
    "media": [
        {"id": "src", "asset": "clip", "role": "timeline", "modality": "video", "at": {"frame": 0}, "range": [90, 95],
         "edit": [{"stream": "video", "during": SPAN, "mask": {"asset": "lens-mask"}, "guides": ["face"]}]},
        {"id": "face", "asset": "old-man", "role": "reference", "modality": "image"},
    ],
}
MOUTH = {
    "version": 2, "duration": 5,
    "prompt": "The man speaks the supplied line; face and head motion unchanged.",
    "media": [
        {"id": "src", "asset": "clip", "role": "timeline", "modality": "video", "at": {"frame": 0}, "range": [90, 95],
         "edit": [
             {"stream": "video", "during": SPAN, "mask": {"asset": "mouth-mask"}},
             {"stream": "audio", "during": SPAN, "guides": ["voice"]},
         ]},
        {"id": "voice", "asset": "voice-clip", "role": "reference", "modality": "audio", "range": [0, 2]},
    ],
}
COMBINED = {
    "version": 2, "duration": 5,
    "prompt": "Sunglasses reflect the old man's face; he speaks the supplied line; everything else unchanged.",
    "media": [
        {"id": "src", "asset": "clip", "role": "timeline", "modality": "video", "at": {"frame": 0}, "range": [90, 95],
         "edit": [
             {"stream": "video", "during": SPAN, "mask": {"asset": "lens-mask"}, "guides": ["face"]},
             {"stream": "video", "during": SPAN, "mask": {"asset": "mouth-mask"}, "guides": ["face"]},
             {"stream": "audio", "during": SPAN, "guides": ["voice"]},
         ]},
        {"id": "face", "asset": "old-man", "role": "reference", "modality": "image"},
        {"id": "voice", "asset": "voice-clip", "role": "reference", "modality": "audio", "range": [0, 2]},
    ],
}
# Two image masks: lens box and mouth box, white = edit.
LENS_BOX = (300, 200, 700, 260)
MOUTH_BOX = (420, 380, 600, 440)


def _mask(path: Path, box: tuple[int, int, int, int]) -> None:
    image = Image.new("L", (1024, 576), 0)
    ImageDraw.Draw(image).rectangle(box, fill=255)
    image.save(path)


def _wav(path: Path, seconds: float, *, patterned: bool = False, sample_base: int = 1) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(48000)
        if patterned:
            import struct
            out.writeframes(b"".join(struct.pack("<h", (index % 10000) + sample_base) for index in range(int(round(seconds * 48000)))))
        else:
            out.writeframes(b"\x00\x00" * int(round(seconds * 48000)))


def _prepared(raw: dict, tmp_path: Path, *, voice_seconds: float = 2.0) -> dict:
    request = normalize_request(raw)
    records = []

    def add(asset: str, path: Path) -> None:
        records.append({"asset": asset, "status": "resolved", "path": str(path), "size": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    clip = tmp_path / "clip.bin"
    # Plan 64 afd3738a7 requires decodable source PCM for pre-splicing.
    _wav(clip, 95.0, patterned=True, sample_base=1)
    add("clip", clip)
    for name, box in (("lens-mask", LENS_BOX), ("mouth-mask", MOUTH_BOX)):
        path = tmp_path / f"{name}.png"
        _mask(path, box)
        add(name, path)
    face = tmp_path / "old-man.png"
    Image.new("RGB", (64, 64), (90, 70, 60)).save(face)
    add("old-man", face)
    voice = tmp_path / "voice-clip.wav"
    _wav(voice, voice_seconds, patterned=True, sample_base=15000)
    add("voice-clip", voice)
    return {"kind": "h3_av_preparation", "status": "prepared", "runtime_submission": "eligible",
            "request": request.value, "request_digest": request.digest, "assets": records}


def _api(manifest: dict) -> dict:
    from vibecomfy.cli_loader import load_workflow_any

    return load_workflow_any(manifest["workflow"]["workflow.py"]["path"]).compile(
        "api", run_inputs=manifest["workflow_inputs"])


def _node(api: dict, class_type: str) -> dict:
    matches = [row for row in api.values() if row["class_type"] == class_type]
    assert len(matches) == 1, (class_type, len(matches))
    return matches[0]


def _ancestors(api: dict, node_id: str) -> set[str]:
    seen, todo = set(), [node_id]
    while todo:
        current = todo.pop()
        for value in api[current]["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and str(value[0]) in api and str(value[0]) not in seen:
                seen.add(str(value[0]))
                todo.append(str(value[0]))
    return seen


def _v2v_source_audio_chain(api: dict) -> set[str]:
    edit_key = next(key for key, row in api.items() if row["class_type"] == "H3V2VGranularFractionalDenoise")
    return _ancestors(api, edit_key)


# --- Request validation -------------------------------------------------------

def _with(raw: dict, edit_index: int, **changes) -> dict:
    document = copy.deepcopy(raw)
    edit = document["media"][0]["edit"][edit_index]
    edit.update(changes)
    return document


@pytest.mark.parametrize(("raw", "message"), [
    (_with(MOUTH, 1, guides=["voice", "voice"]), "requires guides naming exactly one audio reference"),
    # Plan 64 a5ab66014d refuses dual conditioning and replacement use.
    (_with(MOUTH, 0, guides=["voice"]), "cannot be both conditioning and replacement audio"),
    (_with(MOUTH, 1, during=[1.0, 4.0]), "replacement audio length must match or exceed the edited span")  # afd3738a7,
])
def test_invalid_replacement_audio_is_refused_at_request_validation(raw, message):
    with pytest.raises(H3RequestError, match=message):
        normalize_request(raw)


def test_longer_replacement_clip_is_admitted_for_sample_accurate_trim():
    # Plan 64 afd3738a7 trims a longer replacement to the edited span.
    raw = _with(MOUTH, 1, during=[1.5, 2.5])
    request = normalize_request(raw)
    edit = request.value["media"][0]["edit"][1]
    assert edit["resolved"]["range"] == [72000, 120000]


def test_audio_edit_without_guides_keeps_its_existing_path():
    # Dialogue-driven audio generation (no guides) is not a replacement span.
    raw = copy.deepcopy(MOUTH)
    raw["prompt"] = "The speaker says: A new line."
    raw["media"][0]["edit"][1] = {"stream": "audio", "during": SPAN}
    raw["media"] = raw["media"][:1]
    normalize_request(raw)


def test_audio_replacement_guide_must_be_audio():
    raw = copy.deepcopy(MOUTH)
    raw["media"].append({"id": "face", "asset": "old-man", "role": "reference", "modality": "image"})
    raw["media"][0]["edit"][1]["guides"] = ["face"]
    with pytest.raises(H3RequestError, match="audio edits support modalities: audio"):
        normalize_request(raw)


def test_disjoint_replacement_spans_are_spliced_sample_exactly(tmp_path):
    raw = copy.deepcopy(MOUTH)
    raw["media"].append({"id": "voice-2", "asset": "voice-clip", "role": "reference", "modality": "audio", "range": [0, 1]})
    raw["media"][0]["edit"].append({"stream": "audio", "during": [3.5, 4.5], "guides": ["voice-2"]})
    # Plan 64 afd3738a7 supports multiple fixed sample owners.
    manifest = compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")
    from astrid.packs.h3_av.shared.compose import _decode_exact_pcm
    pcm, channels = _decode_exact_pcm(tmp_path / "out" / "source-with-fixed-audio.wav")
    source, _ = _decode_exact_pcm(tmp_path / "clip.bin", expected_channels=channels)
    voice, _ = _decode_exact_pcm(tmp_path / "voice-clip.wav", expected_channels=channels)
    stride = channels * 2
    source = source[90 * 48000 * stride:95 * 48000 * stride]
    assert pcm == source[:72000 * stride] + voice[:96000 * stride] + voice[:48000 * stride] + source[216000 * stride:]
    assert "source_audio_prepared" in manifest["workflow_inputs"]


def test_regions_with_different_references_need_two_passes(tmp_path):
    raw = copy.deepcopy(COMBINED)
    raw["media"].append({"id": "other", "asset": "old-man", "role": "reference", "modality": "image"})
    raw["media"][0]["edit"][1]["guides"] = ["other"]
    # Plan 64 d0bb3a3a4 reports incompatible reference sets as separate passes.
    with pytest.raises(CompilationError, match="different reference sets require separate sequential passes"):
        compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")


def test_replacement_clip_is_trimmed_to_its_range(tmp_path):
    raw = copy.deepcopy(MOUTH)
    raw["media"][-1]["range"] = [3, 5]  # a window of a longer voice track
    manifest = compile_preparation(_prepared(raw, tmp_path, voice_seconds=10.0), out_dir=tmp_path / "out")
    assert manifest["capabilities"]["replacement_audio"]["media_range_seconds"] == [3.0, 5.0]
    # Plan 64 afd3738a7 binds pre-spliced PCM instead of graph trims.
    from astrid.packs.h3_av.shared.compose import _decode_exact_pcm
    pcm, channels = _decode_exact_pcm(tmp_path / "out" / "source-with-fixed-audio.wav")
    voice, _ = _decode_exact_pcm(tmp_path / "voice-clip.wav", expected_channels=channels)
    stride = channels * 2
    source, _ = _decode_exact_pcm(tmp_path / "clip.bin", expected_channels=channels)
    assert pcm[:72000 * stride] == source[90 * 48000 * stride:(90 * 48000 + 72000) * stride]
    assert pcm[168000 * stride:] == source[(90 * 48000 + 168000) * stride:95 * 48000 * stride]
    assert pcm[72000 * stride:168000 * stride] == voice[144000 * stride:240000 * stride]
    assert len(pcm) == 5 * 48000 * stride
    api = _api(manifest)
    chain = _v2v_source_audio_chain(api)
    assert any(row["class_type"] == "LoadAudio" and key in chain and
               row["inputs"]["audio"] == manifest["workflow_inputs"]["source_audio_prepared"]
               for key, row in api.items())


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="clip length can only be measured with ffprobe")
def test_replacement_clip_shorter_than_its_range_is_refused(tmp_path):
    raw = copy.deepcopy(MOUTH)
    raw["media"][-1]["range"] = [3, 5]
    with pytest.raises(CompilationError, match="but its range ends at"):
        compile_preparation(_prepared(raw, tmp_path, voice_seconds=4.0), out_dir=tmp_path / "out")


def test_unreadable_replacement_audio_is_refused(tmp_path):
    # Plan 64 afd3738a7 requires decodable replacement PCM.
    prepared = _prepared(MOUTH, tmp_path)
    voice = tmp_path / "voice-clip.wav"
    voice.write_bytes(b"unreadable audio")
    record = next(row for row in prepared["assets"] if row["asset"] == "voice-clip")
    record.update(size=voice.stat().st_size, sha256=hashlib.sha256(voice.read_bytes()).hexdigest())
    with pytest.raises(CompilationError, match="cannot read replacement audio 'voice-clip'"):
        compile_preparation(prepared, out_dir=tmp_path / "out")


def test_fixed_audio_requires_ffprobe_before_submission(tmp_path, monkeypatch):
    # Plan 64 afd3738a7 requires audio probing before the managed pre-splice.
    prepared = _prepared(MOUTH, tmp_path)
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda command: None if command == "ffprobe" else real_which(command))
    with pytest.raises(CompilationError, match="ffprobe"):
        compile_preparation(prepared, out_dir=tmp_path / "out")


def test_audio_reference_can_condition_an_edit_without_an_audio_grant(tmp_path):
    raw = copy.deepcopy(MOUTH)
    raw["media"][0]["edit"] = raw["media"][0]["edit"][:1]
    # Plan 64 de6ac9b06 admits standalone audio conditioning within its limits.
    manifest = compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")
    assert manifest["capabilities"]["reference_modality_counts"]["audio"] == 1


def test_three_edit_image_references_are_admitted(tmp_path):
    raw = copy.deepcopy(LENS)
    for name in ("face-2", "face-3"):
        raw["media"].append({"id": name, "asset": "old-man", "role": "reference", "modality": "image"})
    raw["media"][0]["edit"][0]["guides"] = ["face", "face-2", "face-3"]
    # Plan 64 de6ac9b06 expands the image slots to the limits table.
    manifest = compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")
    assert manifest["capabilities"]["reference_modality_counts"]["image"] == 3


def test_regions_with_different_intervals_keep_their_masks(tmp_path):
    import subprocess
    raw = copy.deepcopy(COMBINED)
    raw["media"][0]["edit"][1]["during"] = [0.5, 2.5]
    # Plan 64 94a671247b supports independently timed mask grants.
    manifest = compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")
    mask = tmp_path / "out" / "sampler-strength-mask.mkv"
    assert manifest["capabilities"]["mask_binding"] == "per-frame video mask"
    def pixel_frames(x, y):
        result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mask), "-vf", f"crop=1:1:{x}:{y},format=rgb24", "-f", "rawvideo", "-"], check=True, capture_output=True)
        return result.stdout[::3]
    lens = pixel_frames(350, 220)
    mouth = pixel_frames(450, 400)
    assert len(lens) == len(mouth) == 120
    assert (lens[11], lens[12], lens[35], lens[36], lens[59], lens[60], lens[83], lens[84]) == (0, 0, 0, 255, 255, 255, 255, 0)
    assert (mouth[11], mouth[12], mouth[35], mouth[36], mouth[59], mouth[60], mouth[83], mouth[84]) == (0, 255, 255, 255, 255, 0, 0, 0)


def test_replacement_audio_length_is_checked_against_the_clip_when_probed(tmp_path):
    # Plan 64 afd3738a7 checks the actual decoded selection, not metadata alone.
    with pytest.raises(CompilationError, match="but its range ends at"):
        compile_preparation(_prepared(MOUTH, tmp_path, voice_seconds=1.0), out_dir=tmp_path / "out")


# --- Compiled graph: each input alone and combined ----------------------------

def test_reference_image_only_edit_advertises_capacity_and_wires_reference(tmp_path):
    manifest = compile_preparation(_prepared(LENS, tmp_path), out_dir=tmp_path / "out")
    caps = manifest["capabilities"]
    assert caps["references"] == 1
    # Plan 64 de6ac9b06 expands image-reference capacity to nine.
    assert caps["reference_capacity"] == 9
    assert "replacement_audio" not in caps
    api = _api(manifest)
    target = _node(api, "MiniMaxH3ReferenceToVideo")
    assert any(key.endswith("ref_image_0") for key in target["inputs"])
    assert not any(key.endswith("ref_image_1") for key in target["inputs"])
    assert "replacement_audio" not in manifest["workflow_inputs"]
    assert not any(row["class_type"] == "LoadAudio" for row in api.values())


def test_replacement_audio_only_edit_splices_clip_and_holds_audio_strength(tmp_path):
    manifest = compile_preparation(_prepared(MOUTH, tmp_path), out_dir=tmp_path / "out")
    caps = manifest["capabilities"]
    assert caps["references"] == 0
    assert "reference_capacity" not in caps
    assert caps["replacement_audio"]["during_seconds"] == SPAN
    # Plan 64 afd3738a7 validates exact decoded PCM.
    assert caps["replacement_audio"]["length_check"] == "decoded_pcm"
    api = _api(manifest)
    edit = _node(api, "H3V2VGranularFractionalDenoise")
    assert edit["inputs"]["audio_strength"] == 0
    chain = _v2v_source_audio_chain(api)
    loads = [key for key, row in api.items() if row["class_type"] == "LoadAudio"]
    assert len(loads) == 1 and loads[0] in chain
    # Plan 64 afd3738a7 sends pre-spliced PCM to the graph.
    assert api[loads[0]]["inputs"]["audio"] == manifest["workflow_inputs"]["source_audio_prepared"]
    from astrid.packs.h3_av.shared.compose import _decode_exact_pcm
    pcm, channels = _decode_exact_pcm(tmp_path / "out" / "source-with-fixed-audio.wav")
    voice, _ = _decode_exact_pcm(tmp_path / "voice-clip.wav", expected_channels=channels)
    stride = channels * 2
    source, _ = _decode_exact_pcm(tmp_path / "clip.bin", expected_channels=channels)
    assert len(pcm) == 5 * 48000 * stride
    assert pcm[:72000 * stride] == source[90 * 48000 * stride:(90 * 48000 + 72000) * stride]
    assert pcm[168000 * stride:] == source[(90 * 48000 + 168000) * stride:95 * 48000 * stride]
    assert pcm[72000 * stride:168000 * stride] == voice[:96000 * stride]
    assert "source_audio_prepared" in manifest["workflow_inputs"]


def test_lenses_and_mouth_combined_compile_to_one_union_mask_with_both_inputs(tmp_path):
    manifest = compile_preparation(_prepared(COMBINED, tmp_path), out_dir=tmp_path / "out")
    caps = manifest["capabilities"]
    # Plan 64 de6ac9b06 expands image-reference capacity to nine.
    assert caps["references"] == 1 and caps["reference_capacity"] == 9
    assert caps["replacement_audio"]["during_seconds"] == SPAN
    assert caps["mask_binding"].startswith("union of 2 static image masks")
    assert any("share one reference set" in item for item in manifest["limitations"])
    mask = Image.open(Path(manifest["workflow"]["workflow.py"]["path"]).parent.parent / "resolved-mask.png").convert("L")
    assert mask.size == (1024, 576)
    assert mask.getpixel(((LENS_BOX[0] + LENS_BOX[2]) // 2, (LENS_BOX[1] + LENS_BOX[3]) // 2)) == 255
    assert mask.getpixel(((MOUTH_BOX[0] + MOUTH_BOX[2]) // 2, (MOUTH_BOX[1] + MOUTH_BOX[3]) // 2)) == 255
    assert mask.getpixel((10, 10)) == 0
    api = _api(manifest)
    edit = _node(api, "H3V2VGranularFractionalDenoise")
    assert edit["inputs"]["audio_strength"] == 0 and edit["inputs"]["inside_strength"] == 1.0
    assert any(key.endswith("ref_image_0") for key in _node(api, "MiniMaxH3ReferenceToVideo")["inputs"])
    assert any(row["class_type"] == "LoadAudio" for row in api.values())


def test_mouth_only_edit_without_references_keeps_manifest_shape(tmp_path):
    raw = copy.deepcopy(MOUTH)
    raw["media"] = [item for item in raw["media"] if item["role"] == "timeline"] + [raw["media"][-1]]
    manifest = compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")
    assert manifest["capabilities"]["references"] == 0
    assert "reference_capacity" not in manifest["capabilities"]


# --- Defaults stay byte-identical ---------------------------------------------

def _digests(root: Path) -> dict[str, str]:
    """Per-file digests of ``root/out``; the temp root is normalized to <D>."""
    files = {}
    for path in sorted((root / "out").rglob("*")):
        if not path.is_file():
            continue
        key = str(path.relative_to(root / "out"))
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                body = "".join(f"{item.filename}:{hashlib.sha256(archive.read(item)).hexdigest()}\n"
                               for item in sorted(archive.infolist(), key=lambda item: item.filename))
            files[key] = hashlib.sha256(body.encode()).hexdigest()
        elif path.suffix in {".py", ".json", ".yaml", ".lock", ".txt", ".md"} or path.name.endswith(".lock"):
            files[key] = hashlib.sha256(path.read_bytes().replace(str(root).encode(), b"<D>")).hexdigest()
        else:
            files[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _two_refs() -> dict:
    raw = copy.deepcopy(PILL)
    raw["media"].append({"id": "red", "asset": "red-ref", "role": "reference", "modality": "image"})
    raw["media"][0]["edit"][0]["guides"] = ["blue", "red"]
    return raw


BASELINE = json.loads((FIXTURES / "baseline_digests.json").read_text())


@pytest.mark.parametrize("name, raw", [("watermark", WATERMARK), ("pill", PILL), ("two_refs", _two_refs())])
def test_existing_requests_compile_byte_identically_to_baseline(name, raw, tmp_path):
    root = tmp_path
    compile_preparation(_prepare(raw, root), out_dir=root / "out")
    actual = _digests(root)
    expected = BASELINE[name]
    assert set(actual) == set(expected)
    # Plan 64 a5ab66014d changes intent identity; de6ac9b06 adds reference
    # conditioning metadata, and d0bb3a3a4 records sampler-mask capability.
    # a31023ac1 binds size; 2152c9168 and 7365f1be5 add permission custody.
    assert actual == expected
    compiled = json.loads((root / "out" / "compilation.json").read_text())
    baseline = json.loads((FIXTURES / f"baseline_compilation_{name}.json").read_text().replace("<D>", str(root)))
    assert compiled == baseline


# --- Documented examples stay valid ---------------------------------------------

def _doc_examples() -> list[dict]:
    import re

    skill = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/docs/SKILL.md"
    blocks = re.findall(r"```json\n(.*?)\n```", skill.read_text(), re.S)
    assert len(blocks) == 3, "SKILL.md must document the lens, mouth and combined edit inputs"
    return [json.loads(block) for block in blocks]


@pytest.mark.parametrize("index", [0, 1, 2], ids=["lens_reference", "mouth_replacement_audio", "combined"])
def test_documented_edit_inputs_are_schema_valid_and_normalize(index):
    import jsonschema

    schema = json.loads((Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/schemas/request.v2.json").read_text())
    document = _doc_examples()[index]
    jsonschema.validate(document, schema)
    normalize_request(document)


@pytest.mark.parametrize("index", [0, 1, 2], ids=["lens_reference", "mouth_replacement_audio", "combined"])
def test_documented_edit_inputs_compile(index, tmp_path):
    document = _doc_examples()[index]
    assets = []
    for name in sorted({item["asset"] for item in document["media"]} | {
            edit["mask"]["asset"] for item in document["media"] for edit in item.get("edit", []) if "asset" in edit.get("mask", {})}):
        path = tmp_path / name
        if name.endswith(".png") and "mask" in name:
            _mask(path, MOUTH_BOX)
        elif name.endswith(".png"):
            Image.new("RGB", (64, 64), (90, 70, 60)).save(path)
        elif name.endswith(".wav"):
            _wav(path, 1.5, patterned=True, sample_base=15000)
        else:
            # Plan 64 afd3738a7 requires decodable source PCM.
            _wav(path, 3.0, patterned=True, sample_base=1)
        assets.append({"asset": name, "status": "resolved", "path": str(path), "size": path.stat().st_size,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    request = normalize_request(document)
    prepared = {"kind": "h3_av_preparation", "status": "prepared", "runtime_submission": "eligible",
                "request": request.value, "request_digest": request.digest, "assets": assets}
    manifest = compile_preparation(prepared, out_dir=tmp_path / "out")
    assert manifest["status"] == "compiled"

@pytest.mark.parametrize("key", ["text", "dialogue", "latent_pin"])
def test_unbound_v2_fields_are_refused(key):
    raw = copy.deepcopy(MOUTH)
    item = raw["media"][0]
    if key == "latent_pin":
        item[key] = True
    else:
        item["edit"][1][key] = "A literal line."
    with pytest.raises(H3RequestError, match=rf"unsupported field.*{key}"):
        normalize_request(raw)


def _size_request(*, output=None, continuation=False):
    request = {"version": 2, "prompt": "A scene", "duration": 5, "media": [],
               "settings": {}, "continuation": continuation}
    if output is not None:
        request["output"] = output
    return request


@pytest.mark.parametrize("size", [(32, 32), (32, 64), (512, 288), (768, 448),
                                   (16384, 16384), (16384, 32)])
def test_native_output_size_accepts_per_dimension_limits(size):
    request = normalize_v2(_size_request(output={"size": list(size)}))
    assert request.value["canvas"] == {"width": size[0], "height": size[1]}


@pytest.mark.parametrize("size", [
    [256, 144], [768, 432], [0, 32], [16416, 32], [512.5, 288], ["512", 288],
    [True, 288], [512], [512, 288, 32], (512, 288),
])
def test_native_output_size_rejects_invalid_sizes_with_rule(size):
    with pytest.raises(NativeV2Error, match="output.size"):
        normalize_v2(_size_request(output={"size": size}))


def test_native_output_size_default_identity_and_readback():
    from astrid.packs.h3_av.shared.request_v2 import read_prepared_request
    from astrid.packs.h3_av.shared.generation import generation_inputs_from_bindings

    implicit = normalize_v2(_size_request())
    explicit = normalize_v2(_size_request(output={"size": list(DEFAULT_SIZE)}))
    smaller = normalize_v2(_size_request(output={"size": [512, 288]}))
    assert implicit.value["canvas"] == {"width": 1024, "height": 576}
    assert implicit.value == explicit.value
    assert implicit.digest == explicit.digest
    assert implicit.digest != smaller.digest
    restored = read_prepared_request(smaller.value, smaller.digest, require_normalized_v2=True)
    assert restored.value["canvas"] == {"width": 512, "height": 288}
    prepared = prepare.prepare_request(smaller)
    assert (prepared["media"]["width"], prepared["media"]["height"]) == (512, 288)
    with pytest.raises(prepare.PreparationError, match="cannot override the normalized request canvas"):
        prepare.prepare_request(smaller, width=768, height=448)
    confirmed = prepare.prepare_request(smaller, width=512, height=288)
    assert (confirmed["media"]["width"], confirmed["media"]["height"]) == (512, 288)
    inputs = generation_inputs_from_bindings(
        references=[], prompt="A scene", frames=124, model="model", steps=8, seed=1,
        canvas_size=(restored.value["canvas"]["width"], restored.value["canvas"]["height"]),
    )
    assert inputs.canvas == {"width": 512, "height": 288, "frames": 124}


def test_native_continuation_refuses_nondefault_size_from_lowering_limit():
    with pytest.raises(NativeV2Error, match="continuation_canvas"):
        normalize_v2(_size_request(output={"size": [512, 288]}, continuation=True))


def test_legacy_persisted_canvas_override_cannot_change_request_identity():
    from astrid.packs.h3_av.shared.request_v2 import read_prepared_request

    normalized = normalize_v2(_size_request())
    legacy = dict(normalized.value)
    del legacy["canvas"]
    restored = read_prepared_request(legacy, normalized.digest, require_normalized_v2=True)
    with pytest.raises(prepare.PreparationError, match="cannot override the normalized request canvas"):
        prepare.prepare_request(restored, width=512, height=288)
    prepared = prepare.prepare_request(restored, width=DEFAULT_SIZE[0], height=DEFAULT_SIZE[1])
    assert prepared["request_digest"] == normalized.digest
    assert (prepared["media"]["width"], prepared["media"]["height"]) == DEFAULT_SIZE
