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

from astrid.packs.h3_av.shared.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
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


def _wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(48000)
        out.writeframes(b"\x00\x00" * int(round(seconds * 48000)))


def _prepared(raw: dict, tmp_path: Path, *, voice_seconds: float = 2.0) -> dict:
    request = normalize_request(raw)
    records = []

    def add(asset: str, path: Path) -> None:
        records.append({"asset": asset, "status": "resolved", "path": str(path), "size": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    clip = tmp_path / "clip.bin"
    clip.write_bytes(b"fixture clip")
    add("clip", clip)
    for name, box in (("lens-mask", LENS_BOX), ("mouth-mask", MOUTH_BOX)):
        path = tmp_path / f"{name}.png"
        _mask(path, box)
        add(name, path)
    face = tmp_path / "old-man.png"
    Image.new("RGB", (64, 64), (90, 70, 60)).save(face)
    add("old-man", face)
    voice = tmp_path / "voice-clip.wav"
    _wav(voice, voice_seconds)
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
    (_with(MOUTH, 0, guides=["voice"]), "video edits support modalities: image"),
    (_with(MOUTH, 1, during=[1.5, 2.5]), "replacement audio length must match the edited span"),
    (_with(MOUTH, 1, during=[1.0, 4.0]), "replacement audio length must match the edited span"),
])
def test_invalid_replacement_audio_is_refused_at_request_validation(raw, message):
    with pytest.raises(H3RequestError, match=message):
        normalize_request(raw)


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


def test_replacement_spans_are_a_list_and_several_are_refused_at_compile(tmp_path):
    raw = copy.deepcopy(MOUTH)
    raw["media"].append({"id": "voice-2", "asset": "voice-clip", "role": "reference", "modality": "audio", "range": [0, 1]})
    raw["media"][0]["edit"].append({"stream": "audio", "during": [3.5, 4.5], "guides": ["voice-2"]})
    normalize_request(raw)  # the request shape admits a list of spans
    with pytest.raises(CompilationError, match="one replacement audio span per request is supported"):
        compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")


def test_regions_with_different_references_need_two_passes(tmp_path):
    raw = copy.deepcopy(COMBINED)
    raw["media"].append({"id": "other", "asset": "old-man", "role": "reference", "modality": "image"})
    raw["media"][0]["edit"][1]["guides"] = ["other"]
    with pytest.raises(CompilationError, match="need two passes"):
        compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")


def test_replacement_clip_is_trimmed_to_its_range(tmp_path):
    raw = copy.deepcopy(MOUTH)
    raw["media"][-1]["range"] = [3, 5]  # a window of a longer voice track
    manifest = compile_preparation(_prepared(raw, tmp_path, voice_seconds=10.0), out_dir=tmp_path / "out")
    assert manifest["capabilities"]["replacement_audio"]["media_range_seconds"] == [3.0, 5.0]
    api = _api(manifest)
    trims = [key for key, row in api.items()
             if row["class_type"] == "TrimAudioDuration" and row["inputs"].get("start_index") == 3.0]
    assert len(trims) == 1
    trim = api[trims[0]]["inputs"]
    assert trim["duration"] == 2.0
    assert api[trim["audio"][0]]["class_type"] == "LoadAudio"


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="clip length can only be measured with ffprobe")
def test_replacement_clip_shorter_than_its_range_is_refused(tmp_path):
    raw = copy.deepcopy(MOUTH)
    raw["media"][-1]["range"] = [3, 5]
    with pytest.raises(CompilationError, match="but its range ends at"):
        compile_preparation(_prepared(raw, tmp_path, voice_seconds=4.0), out_dir=tmp_path / "out")


@pytest.mark.parametrize("ffprobe_available", [True, False])
def test_unreadable_replacement_audio_requires_ffprobe_to_be_absent(tmp_path, monkeypatch, ffprobe_available):
    from astrid.packs.h3_av.shared import compose

    real_which = shutil.which

    def which(command):
        if command == "ffprobe":
            return "/usr/bin/ffprobe" if ffprobe_available else None
        return real_which(command)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(compose, "_probe", lambda path: None)
    prepared = _prepared(MOUTH, tmp_path)
    if ffprobe_available:
        with pytest.raises(CompilationError, match="ffprobe could not read replacement audio 'voice-clip'"):
            compile_preparation(prepared, out_dir=tmp_path / "out")
    else:
        manifest = compile_preparation(prepared, out_dir=tmp_path / "out")
        assert manifest["capabilities"]["replacement_audio"]["length_check"] == "request_range_only"


def test_audio_reference_must_be_used_by_an_audio_edit(tmp_path):
    raw = copy.deepcopy(MOUTH)
    raw["media"][0]["edit"] = raw["media"][0]["edit"][:1]
    with pytest.raises(CompilationError, match="Only untimed image references are representable"):
        compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")


def test_more_than_two_edit_references_are_refused(tmp_path):
    raw = copy.deepcopy(LENS)
    for name in ("face-2", "face-3"):
        raw["media"].append({"id": name, "asset": "old-man", "role": "reference", "modality": "image"})
    raw["media"][0]["edit"][0]["guides"] = ["face", "face-2", "face-3"]
    prepared = _prepared(raw, tmp_path)
    with pytest.raises(CompilationError, match="two image-reference slots"):
        compile_preparation(prepared, out_dir=tmp_path / "out")


def test_regions_with_different_intervals_cannot_share_one_edit(tmp_path):
    raw = copy.deepcopy(COMBINED)
    raw["media"][0]["edit"][1]["during"] = [0.5, 2.5]
    with pytest.raises(CompilationError, match="must share one interval"):
        compile_preparation(_prepared(raw, tmp_path), out_dir=tmp_path / "out")


def test_replacement_audio_length_is_checked_against_the_clip_when_probed(tmp_path, monkeypatch):
    from astrid.packs.h3_av.shared import compose

    real_probe, real_coverage = compose._probe, compose._audio_coverage
    is_voice = lambda path: Path(path).name == "voice-clip.wav"  # noqa: E731
    monkeypatch.setattr(compose, "_probe", lambda path: {"streams": [{"codec_type": "audio"}]} if is_voice(path) else real_probe(path))
    monkeypatch.setattr(compose, "_audio_coverage", lambda path: {"duration": 1.0} if is_voice(path) else real_coverage(path))
    with pytest.raises(CompilationError, match="but its range ends at"):
        compile_preparation(_prepared(MOUTH, tmp_path), out_dir=tmp_path / "out")


# --- Compiled graph: each input alone and combined ----------------------------

def test_reference_image_only_edit_advertises_capacity_and_wires_reference(tmp_path):
    manifest = compile_preparation(_prepared(LENS, tmp_path), out_dir=tmp_path / "out")
    caps = manifest["capabilities"]
    assert caps["references"] == 1
    assert caps["reference_capacity"] == 2
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
    assert caps["replacement_audio"]["length_check"] in {"ffprobe", "request_range_only"}
    api = _api(manifest)
    edit = _node(api, "H3V2VGranularFractionalDenoise")
    assert edit["inputs"]["audio_strength"] == 0
    chain = _v2v_source_audio_chain(api)
    loads = [key for key, row in api.items() if row["class_type"] == "LoadAudio"]
    assert len(loads) == 1 and loads[0] in chain
    assert api[loads[0]]["inputs"]["audio"] == manifest["workflow_inputs"]["replacement_audio"]
    trims = [row["inputs"] for row in api.values() if row["class_type"] == "TrimAudioDuration"]
    assert {"start_index": 0.0, "duration": 1.5} in [{k: t[k] for k in ("start_index", "duration")} for t in trims]
    assert {"start_index": 3.5, "duration": 1.5} in [{k: t[k] for k in ("start_index", "duration")} for t in trims]
    assert "replacement_audio" in manifest["workflow_inputs"]


def test_lenses_and_mouth_combined_compile_to_one_union_mask_with_both_inputs(tmp_path):
    manifest = compile_preparation(_prepared(COMBINED, tmp_path), out_dir=tmp_path / "out")
    caps = manifest["capabilities"]
    assert caps["references"] == 1 and caps["reference_capacity"] == 2
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
    changed = sorted(key for key in expected if actual[key] != expected[key])
    if name == "watermark":
        assert changed == []  # no references: manifest is byte-identical too
        return
    # The orchestrator authorized existing reference-bearing requests to advertise
    # the two image-reference slots. Only compilation.json may change, through
    # the added capacity key and the digest computed over the manifest.
    assert changed == ["compilation.json"]
    compiled = json.loads((root / "out" / "compilation.json").read_text())
    assert compiled["capabilities"].pop("reference_capacity") == 2
    baseline = json.loads((FIXTURES / f"baseline_compilation_{name}.json").read_text().replace("<D>", str(root)))
    compiled.pop("compilation_digest")
    baseline.pop("compilation_digest")
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
            _wav(path, 1.5)
        else:
            path.write_bytes(b"fixture clip")
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
