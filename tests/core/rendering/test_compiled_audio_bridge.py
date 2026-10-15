from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from astrid.core.rendering.compiled_audio import (
    attach_compiled_audio,
    compile_snapshot_audio,
    digest,
)
from astrid.core.rendering.contracts import RenderRequest, SCHEMA_VERSION, FrameWindow
from astrid.core.rendering.envelope_pcm import prepare_envelope_pcm
from astrid.core.timeline.audio_envelope import evaluate


def config():
    return {
        "output": {"fps": 30, "resolution": "32x32", "file": "render.mp4"},
        "tracks": [
            {"id": "vo", "kind": "audio", "label": "Voice"},
            {"id": "music", "kind": "audio", "label": "Music"},
        ],
        "clips": [
            {
                "id": "one:voice",
                "clipType": "media",
                "asset": "voice",
                "track": "vo",
                "at": 1,
                "from": 0,
                "to": 1,
                "app": {"astrid": {"words": [[0, 0.5, "hello"], [0.6, 1, "world"]]}},
            },
            {
                "id": "one:bed",
                "clipType": "media",
                "asset": "bed",
                "track": "music",
                "at": 0,
                "from": 0.5,
                "to": 4.5,
                "params": {"fadeIn": 0.2},
                "app": {
                    "astrid": {
                        "duck": {"under": "voice", "by": "-10dB", "attack": "6f", "release": "12f"}
                    }
                },
            },
        ],
    }


def authority():
    return {"project_id": "p", "timeline_id": "t", "head_event_id": "rev-1", "head_hash": "a" * 64}


def admitted(data):
    pin = authority()
    audio = compile_snapshot_audio(data, pin)
    pin["compiled_audio_digest"] = digest(audio)
    return audio, pin


def request(tmp_path, data=None, audio=None):
    data = config() if data is None else data
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps(data))
    assets = tmp_path / "assets.json"
    assets.write_text(json.dumps({"assets": {}}))
    produced, pin = admitted(data)
    return RenderRequest(
        schema_version=SCHEMA_VERSION,
        timeline_path=str(path),
        assets_registry_path=str(assets),
        output_name="render.mp4",
        compiled_audio=produced if audio is None else audio,
        timeline_authority=pin,
    )


def test_frozen_producer_matches_sdk_and_preserves_documents():
    from astrid.sdk.timeline.audio_envelope import compile_clip_audio

    data = config()
    before = copy.deepcopy(data)
    compiled, pin = admitted(data)
    sdk = SimpleNamespace(
        data=data["clips"][1],
        start=0,
        _tl=SimpleNamespace(
            fps=30,
            words=lambda: [SimpleNamespace(start=1, end=1.5), SimpleNamespace(start=1.6, end=2)],
        ),
    )
    assert compiled["clips"]["one:bed"] == compile_clip_audio(sdk)
    detached = attach_compiled_audio(data, compiled, pin)
    assert detached["clips"][1]["audio"] == compiled["clips"]["one:bed"]
    detached["clips"][1]["audio"]["envelope"]["keyframes"][0]["value"] = 7
    assert data == before
    assert compiled["clips"]["one:bed"]["envelope"]["keyframes"][0]["value"] == 1
    from astrid.core.timeline import Timeline

    saved = Timeline(data).to_json_data()
    assert all("audio" not in c for c in saved["clips"])
    with pytest.raises(Exception):
        Timeline(attach_compiled_audio(data, compiled, pin))


@pytest.mark.parametrize(
    "change,code",
    [
        ("head", "AUDIO_SOURCE_STALE"),
        ("word", "AUDIO_SOURCE_STALE"),
        ("config", "AUDIO_CONFIG_STALE"),
        ("curve", "AUDIO_REGISTRY_STALE"),
        ("unknown", "AUDIO_OCCURRENCE_UNKNOWN"),
        ("missing", "AUDIO_OCCURRENCE_MISMATCH"),
        ("duplicate", "AUDIO_OCCURRENCE_DUPLICATE"),
        ("context", "AUDIO_CONTEXT_MISSING"),
        ("malformed", "AUDIO_PAYLOAD_INVALID"),
        ("tamper", "AUDIO_PAYLOAD_STALE"),
    ],
)
def test_binding_and_membership_fail_closed(change, code):
    data = config()
    audio, pin = admitted(data)
    if change == "head":
        pin["head_event_id"] = "new-head"
    if change == "word":
        data["clips"][0]["app"]["astrid"]["words"][0][1] = 0.4
    if change == "config":
        data["clips"][1]["to"] = 7
    if change == "curve":
        audio["curve_registry_digest"] = "0" * 64
    if change == "unknown":
        audio["clips"]["unknown"] = audio["clips"].pop("one:bed")
    if change == "missing":
        audio["clips"] = {}
    if change == "duplicate":
        data["clips"].append(copy.deepcopy(data["clips"][0]))
    if change == "context":
        pin = None
    if change == "malformed":
        audio["extra"] = 1
    if change == "tamper":
        audio["clips"]["one:bed"]["envelope"]["keyframes"][0]["value"] = 0.7
    with pytest.raises(ValueError, match=code):
        attach_compiled_audio(data, audio, pin)


def test_missing_context_or_missing_payload_never_drops_duck():
    data = config()
    data["clips"][0]["app"] = {}
    with pytest.raises(ValueError, match="DUCK_UNRENDERED.*context"):
        compile_snapshot_audio(data, authority())
    with pytest.raises(ValueError, match="DUCK_UNRENDERED"):
        attach_compiled_audio(config(), None, authority())


def test_request_roundtrip_backend_projection_identity_and_detachment(tmp_path):
    from astrid.core.rendering.contracts import compute_request_digest

    req = request(tmp_path)
    wire = req.to_dict()
    roundtrip = RenderRequest.from_dict(wire)
    import jsonschema

    schema_path = Path(__file__).parents[3] / "astrid/core/rendering/schemas/v1/request.json"
    schema = json.loads(schema_path.read_text())
    jsonschema.validate(wire, schema)
    malformed = copy.deepcopy(wire)
    malformed["compiled_audio"]["clips"]["one:bed"]["extra"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(malformed, schema)
    assert roundtrip.for_backend("rendering.ffmpeg").compiled_audio == req.compiled_audio
    assert roundtrip.to_dict() == wire
    changed = replace(req, timeline_authority={**authority(), "head_event_id": "other"})
    assert compute_request_digest(req.to_dict()) != compute_request_digest(changed.to_dict())
    wire["compiled_audio"]["clips"].clear()
    assert req.compiled_audio["clips"]


def test_ffmpeg_boundary_attaches_after_load_and_refuses_stale(tmp_path):
    from astrid.packs.rendering.rendering.renderers.ffmpeg.command import (
        resolve_render_command_inputs,
    )
    from astrid.packs.rendering.rendering.renderers.ffmpeg.run import support

    data = config()
    data["tracks"].append({"id": "picture", "kind": "visual", "label": "Picture"})
    data["clips"].append(
        {
            "id": "picture",
            "clipType": "media",
            "asset": "picture",
            "track": "picture",
            "at": 0,
            "from": 0,
            "to": 4,
        }
    )
    req = request(tmp_path, data)
    inputs = resolve_render_command_inputs(req, tmp_path)
    assert inputs.timeline_data["clips"][1]["audio"] == req.compiled_audio["clips"]["one:bed"]
    assert all("audio" not in c for c in json.loads(Path(req.timeline_path).read_text())["clips"])
    stale = replace(req, timeline_authority={**authority(), "head_hash": "f" * 64})
    with pytest.raises(ValueError, match="AUDIO_SOURCE_STALE"):
        resolve_render_command_inputs(stale, tmp_path)
    assert any("AUDIO_SOURCE_STALE" in r for r in support(stale, workspace=tmp_path).reasons)


def test_remotion_support_boundary_validates_after_canonical_load(tmp_path):
    from astrid.packs.rendering.rendering.renderers.remotion.run import support

    req = request(tmp_path)
    report = support(
        replace(req, timeline_authority={**authority(), "head_event_id": "stale"}),
        workspace=tmp_path,
    )
    assert any("AUDIO_SOURCE_STALE" in reason for reason in report.reasons)


def test_window_projection_preserves_sample_clock(tmp_path):
    from astrid.core.rendering.service import RenderService
    from astrid.core.rendering.contracts import RenderSegment

    req = request(tmp_path)
    window = FrameWindow(start_frame=30, end_frame=60, fps_rational=(30, 1))
    candidate = SimpleNamespace(
        id="rendering.remotion", manifest=SimpleNamespace(capabilities={"supports_windows": False})
    )
    segment = SimpleNamespace(window=window, layer=None)
    service = object.__new__(RenderService)
    projected, _ = service._segment_request(
        req, candidate=candidate, segment=segment, index=0, workspace=tmp_path
    )
    sliced = json.loads(Path(projected.timeline_path).read_text())
    attached = attach_compiled_audio(sliced, projected.compiled_audio, projected.timeline_authority)
    old = req.compiled_audio["clips"]["one:bed"]["envelope"]
    new = attached["clips"][1]["audio"]["envelope"]
    assert attached["clips"][1]["id"] == "one:bed_30_60"
    for t in (0, 0.1, 0.5, 0.9):
        assert evaluate(new, t) == pytest.approx(evaluate(old, t + 1))
    assert all("audio" not in c for c in sliced["clips"])


def test_real_encoder_pcm_is_attempt_local_and_gain_only(tmp_path):
    from astrid.core.rendering.assets import AssetMaterializer, InvocationAssetServer

    source = tmp_path / "source.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.5:s=48000:d=3",
            "-c:a",
            "pcm_f32le",
            str(source),
        ],
        check=True,
    )
    data = config()
    data["clips"] = data["clips"][1:]
    data["clips"][0].pop("app")
    data["clips"][0]["speed"] = 2
    data["clips"][0]["params"] = {"fadeIn": 1}
    data["clips"][0]["to"] = 2.5
    audio, pin = admitted(data)
    data = attach_compiled_audio(data, audio, pin)
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "assets": {
                    "bed": {
                        "file": str(source),
                        "type": "audio",
                        "media_id": "source",
                        "digest": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
                    }
                }
            }
        )
    )
    with AssetMaterializer(
        registry, materialized_root=tmp_path, allow_derived_files=True, staging_parent=tmp_path
    ) as materializer:
        with InvocationAssetServer(materializer.serving_root) as server:
            prepare_envelope_pcm(data, materializer, server)
            marker = data["clips"][0]["audio"]["render_source"]
            url = marker["file"]
            path = next(materializer.staging_dir.glob("envelope-*.wav"))
            assert path.is_relative_to(tmp_path)
            assert marker["source_from"] == 0.5 and marker["playback_rate"] == 2
            assert data["clips"][0]["from"] == 0.5 and data["clips"][0]["speed"] == 2
            pcm = subprocess.check_output(
                ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-"]
            )
            samples = np.frombuffer(pcm, dtype="<f4")
            assert len(samples) == 144000  # no trim or rate in preparation
            env = data["clips"][0]["audio"]["envelope"]
            for index in (24000, 48000, 72000, 96000, 120000):
                assert samples[index] == pytest.approx(
                    0.5 * evaluate(env, (index / 48000 - 0.5) / 2), abs=1e-6
                )
            import urllib.request

            assert urllib.request.urlopen(url).read()[:4] == b"RIFF"
        stage = materializer.staging_dir
    assert not stage.exists()
    with pytest.raises(ValueError, match="ENVELOPE_UNRENDERED.*attempt-local"):
        prepare_envelope_pcm(data, SimpleNamespace(assets={}), None)


def test_actual_managed_and_candidate_admission_use_same_frozen_producer(monkeypatch):
    from astrid.core.timeline.authoring_bundle import (
        open_authoring_bundle,
        preview_authoring_candidate,
    )
    from astrid.sdk.invocation import _prepare_managed_render_inputs
    from tests.timeline.test_authoring_bundle import _closure, OLD, AUDIO

    parent, shots, internals = _closure(shared=False)
    candidate = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=internals
    )
    candidate["parent"]["config"] = {"tracks": config()["tracks"], "clips": []}
    candidate["parent"]["clips"] = []
    for shot in candidate["shots"].values():
        internal = shot["internal_timeline"]
        internal["tracks"] = config()["tracks"]
        internal["clips"] = copy.deepcopy(config()["clips"])
        for clip in internal["clips"]:
            clip["id"] = clip["id"].split(":")[-1]
        internal["registry"] = {
            "assets": {
                "voice": {"media_id": AUDIO, "type": "audio"},
                "bed": {"media_id": OLD, "type": "audio"},
            }
        }
    for placement in candidate["placements"]:
        placement["duration_ms"] = 5000
    frozen = preview_authoring_candidate(candidate)
    publication = frozen["publication"]

    class Runtime:
        def __init__(self, published):
            self.published = published
            self.projects = SimpleNamespace(
                show=lambda _: SimpleNamespace(ok=True, data={"id": "project-1", "slug": "demo"})
            )
            self.timelines = SimpleNamespace(
                list=lambda *a, **k: SimpleNamespace(
                    ok=True, data=[[{"timeline_id": "main", "slug": "main", "version": 7}], None]
                ),
                inspect=lambda *a, **k: SimpleNamespace(
                    ok=True,
                    data={
                        "representation": "canonical_head",
                        "is_current_head": True,
                        "revision_id": publication["parent_revision_id"]
                        if published
                        else "parent-1",
                        "timeline_id": "main",
                    },
                ),
            )
            self.media = SimpleNamespace(
                list=lambda *a, **k: SimpleNamespace(
                    ok=True, data=[[{"media_id": v, "digest": v[7:]} for v in (OLD, AUDIO)], None]
                )
            )

        def get_project_parent_composition_revision(self, *a):
            return (
                {
                    "project_id": "project-1",
                    "timeline_id": "main",
                    "revision_id": publication["parent_revision_id"],
                    "content_digest": publication["content_digest"],
                    "payload": publication["parent_composition"],
                }
                if self.published
                else parent
            )

        def get_project_shot_revision(self, *a):
            assert self.published, "candidate must use frozen changed shot"
            return next(
                r
                for r in publication["shot_revisions"]
                if (r["shot_id"], r["revision_id"]) == a[-2:]
            )

        def get_project_timeline_revision(self, *a):
            assert self.published, "candidate must use frozen changed internal"
            return next(
                r for r in publication["internal_timeline_revisions"] if r["revision_id"] == a[-1]
            )

    before = copy.deepcopy(frozen)
    preview, pin = _prepare_managed_render_inputs(
        {"timeline_ref": "main", "authoring_preview": frozen},
        project="demo",
        _client=Runtime(False),
    )
    published, published_pin = _prepare_managed_render_inputs(
        {"timeline_ref": "main"}, project="demo", _client=Runtime(True)
    )
    assert preview["compiled_audio"]["clips"] == published["compiled_audio"]["clips"]
    assert (
        preview["compiled_audio"]["source_context_digest"]
        != published["compiled_audio"]["source_context_digest"]
    )
    assert preview["timeline_snapshot"]["compiled_audio"] == preview["compiled_audio"]
    assert pin["compiled_audio_digest"] == digest(preview["compiled_audio"])
    assert frozen == before
    assert all("audio" not in c for c in preview["timeline_snapshot"]["config"]["clips"])
    for values, authority in ((preview, pin), (published, published_pin)):
        attached = attach_compiled_audio(
            values["timeline_snapshot"]["config"], values["compiled_audio"], authority
        )
        assert sum("audio" in c for c in attached["clips"]) == len(
            values["compiled_audio"]["clips"]
        )
    # Caller pins cannot create authority, even when copied from a genuine run.
    from astrid.sdk.exceptions import CapabilityValidationError

    with pytest.raises(CapabilityValidationError, match="caller-supplied compiled_audio"):
        _prepare_managed_render_inputs(
            {"timeline_ref": "main", "compiled_audio": preview["compiled_audio"]},
            project="demo",
            _client=Runtime(False),
        )


def test_action_creates_request_with_exact_payload_and_pin(tmp_path, monkeypatch):
    from astrid.packs.rendering.actions.render import run

    req = request(tmp_path)
    seen = []

    def render(request, **kwargs):
        seen.append(request)
        output = kwargs["out_path"]
        output.write_bytes(b"fixture")
        Path(str(output) + ".provenance.json").write_text("{}")
        return output

    monkeypatch.setattr(run, "_default_service", lambda: SimpleNamespace(render=render))
    run.render(
        Path(req.timeline_path),
        Path(req.assets_registry_path),
        tmp_path / "render.mp4",
        selector="rendering.ffmpeg",
        compiled_audio=req.compiled_audio,
        timeline_authority=req.timeline_authority,
    )
    assert seen[0].compiled_audio == req.compiled_audio
    assert seen[0].timeline_authority == req.timeline_authority


def test_remotion_props_boundary_uses_admitted_payload_and_attempt_pcm(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion import run

    data = config()
    source = tmp_path / "source.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.25:s=48000:d=5",
            "-c:a",
            "pcm_f32le",
            str(source),
        ],
        check=True,
    )
    req = request(tmp_path, data)
    registry = {
        "assets": {
            key: {
                "media_id": key,
                "digest": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
                "file": str(source),
                "type": "audio",
            }
            for key in ("voice", "bed")
        }
    }
    Path(req.assets_registry_path).write_text(json.dumps(registry))
    for name, value in {
        "_validate_project_dir": lambda p: SimpleNamespace(
            node_executable=Path("/node"), node_version="test", remotion_cli=Path("/cli")
        ),
        "_regenerate_element_registries": lambda *a: None,
        "_validate_renderer_effect_registry": lambda *a: None,
        "_effective_registry_state": lambda *a: {},
        "_stage_effect_assets_for_timeline": lambda *a, **k: {},
    }.items():
        monkeypatch.setattr(run, name, value)
    real_run = subprocess.run
    captured = []

    class AtEncoder(Exception):
        pass

    def intercept(argv, **kwargs):
        if str(argv[0]) == "ffmpeg":
            return real_run(argv, **kwargs)
        props = json.loads(Path(argv[argv.index("--props") + 1]).read_text())
        captured.append(props)
        clip = props["timeline"]["clips"][1]
        assert clip["audio"]["envelope"] == req.compiled_audio["clips"]["one:bed"]["envelope"]
        import urllib.request

        assert urllib.request.urlopen(clip["audio"]["render_source"]["file"]).read()[:4] == b"RIFF"
        assert clip["from"] == 0.5 and clip.get("speed", 1) == 1
        raise AtEncoder

    monkeypatch.setattr(run.subprocess, "run", intercept)
    with pytest.raises(AtEncoder):
        run._execute_remotion_locked(
            Path(req.timeline_path),
            Path(req.assets_registry_path),
            tmp_path / "out.mp4",
            provenance_out_path=tmp_path / "out.mp4",
            project_dir=tmp_path / "project",
            composition_id="TimelineComposition",
            theme_path=None,
            min_free_gb=None,
            materialized_root=tmp_path,
            staging_parent=tmp_path,
            compiled_audio=req.compiled_audio,
            timeline_authority=req.timeline_authority,
        )
    assert captured
    assert not list(tmp_path.glob("astrid-render-assets-*"))
    assert not (tmp_path / ".remotion-props.json").exists()
    assert all("audio" not in c for c in json.loads(Path(req.timeline_path).read_text())["clips"])


def test_encoder_preparation_command_is_gain_only_and_occurrence_local(tmp_path, monkeypatch):
    """Pure handoff check; the encoder is intercepted, so this is not encoded proof."""
    from astrid.core.rendering.assets import AssetMaterializer

    source = tmp_path / "source.wav"
    source.write_bytes(b"owned synthetic source, no decoding")
    data = config()
    bed = data["clips"][1]
    bed.pop("app")
    bed["speed"] = 2
    second = copy.deepcopy(bed)
    second["id"] = "two:bed"
    second["params"] = {"fadeOut": 0.5}
    data["clips"] = [bed, second]
    compiled, pin = admitted(data)
    detached = attach_compiled_audio(data, compiled, pin)
    registry = tmp_path / "registry.json"
    import hashlib

    registry.write_text(
        json.dumps(
            {
                "assets": {
                    "bed": {
                        "file": str(source),
                        "type": "audio",
                        "media_id": "source",
                        "digest": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                }
            }
        )
    )
    calls = []

    def fake_encoder(argv, **kwargs):
        calls.append(argv)
        Path(argv[-1]).write_bytes(b"RIFFmocked encoder; not playable PCM")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("astrid.core.rendering.envelope_pcm.subprocess.run", fake_encoder)
    with AssetMaterializer(
        registry, materialized_root=tmp_path, allow_derived_files=True, staging_parent=tmp_path
    ) as materializer:
        server = SimpleNamespace(local_url=lambda path: "http://127.0.0.1/attempt/" + path.name)
        prepare_envelope_pcm(detached, materializer, server)
        urls = [c["audio"]["render_source"]["file"] for c in detached["clips"]]
        assert len(set(urls)) == 2
        for clip, argv in zip(detached["clips"], calls):
            assert Path(argv[-1]).is_relative_to(materializer.staging_dir)
            assert "-ss" not in argv and "-t" not in argv and "-ar" not in argv
            expression = argv[argv.index("-af") + 1]
            assert "val(ch)*" in expression and "((t-0.5)/2)" in expression
            assert "atrim" not in expression and "atempo" not in expression
            marker = clip["audio"]["render_source"]
            assert marker["source_from"] == 0.5 and marker["playback_rate"] == 2
            assert clip["from"] == 0.5 and clip["speed"] == 2
        stage = materializer.staging_dir
    assert not stage.exists()
    assert all("audio" not in clip for clip in data["clips"])
    with pytest.raises(ValueError, match="ENVELOPE_UNRENDERED.*attempt-local"):
        prepare_envelope_pcm(detached, SimpleNamespace(assets={}), None)


def test_dropped_admitted_payload_and_duplicate_occurrences_refuse():
    data = config()
    compiled, pin = admitted(data)
    with pytest.raises(ValueError, match="AUDIO_PAYLOAD_MISSING"):
        attach_compiled_audio(data, None, pin)
    data["app"] = {
        "astrid_shot_composition": {
            "occurrences": [{"occurrence_id": "one"}, {"occurrence_id": "one"}]
        }
    }
    with pytest.raises(ValueError, match="AUDIO_OCCURRENCE_DUPLICATE"):
        attach_compiled_audio(data, compiled, pin)


def test_render_export_adapter_uses_snapshot_audio_and_rejects_param_override():
    from astrid.packs.rendering.actions.render.task_adapter import _decode_spec, RenderExportRefused

    data = config()
    compiled, pin = admitted(data)
    spec = {
        "project_slug": "p",
        "params": {},
        "timeline_snapshot": {
            "config": data,
            "registry": {"assets": {}},
            "compiled_audio": compiled,
        },
        "timeline_authority": pin,
    }
    _, _, _, params = _decode_spec(SimpleNamespace(spec=spec))
    assert params["compiled_audio"] == compiled and params["timeline_authority"] == pin
    spec["params"]["compiled_audio"] = compiled
    with pytest.raises(RenderExportRefused, match="snapshot-owned"):
        _decode_spec(SimpleNamespace(spec=spec))


def test_remotion_props_boundary_pure_prepares_served_source_from_admitted_audio(
    tmp_path, monkeypatch
):
    """Exercise the real props handoff with an intercepted PCM encoder, without render proof."""
    from astrid.packs.rendering.rendering.renderers.remotion import run

    source = tmp_path / "source.wav"
    source.write_bytes(b"owned fixture; encoder intercepted")
    req = request(tmp_path)
    import hashlib

    registry = {
        "assets": {
            key: {
                "media_id": key,
                "digest": hashlib.sha256(source.read_bytes()).hexdigest(),
                "file": str(source),
                "type": "audio",
            }
            for key in ("voice", "bed")
        }
    }
    Path(req.assets_registry_path).write_text(json.dumps(registry))
    for name, value in {
        "_validate_project_dir": lambda p: SimpleNamespace(
            node_executable=Path("/node"), node_version="test", remotion_cli=Path("/cli")
        ),
        "_regenerate_element_registries": lambda *a: None,
        "_validate_renderer_effect_registry": lambda *a: None,
        "_effective_registry_state": lambda *a: {},
        "_stage_effect_assets_for_timeline": lambda *a, **k: {},
    }.items():
        monkeypatch.setattr(run, name, value)

    class AtEncoder(Exception):
        pass

    captured = []

    def intercept(argv, **kwargs):
        if str(argv[0]) == "ffmpeg":
            Path(argv[-1]).write_bytes(b"RIFFmocked PCM")
            return subprocess.CompletedProcess(argv, 0)
        props = json.loads(Path(argv[argv.index("--props") + 1]).read_text())
        captured.append(props)
        clip = props["timeline"]["clips"][1]
        assert clip["audio"]["envelope"] == req.compiled_audio["clips"]["one:bed"]["envelope"]
        assert clip["audio"]["render_source"]["file"].startswith("http://127.0.0.1:")
        assert clip["from"] == 0.5
        raise AtEncoder

    monkeypatch.setattr(run.subprocess, "run", intercept)
    with pytest.raises(AtEncoder):
        run._execute_remotion_locked(
            Path(req.timeline_path),
            Path(req.assets_registry_path),
            tmp_path / "out.mp4",
            provenance_out_path=tmp_path / "out.mp4",
            project_dir=tmp_path / "project",
            composition_id="TimelineComposition",
            theme_path=None,
            min_free_gb=None,
            materialized_root=tmp_path,
            staging_parent=tmp_path,
            compiled_audio=req.compiled_audio,
            timeline_authority=req.timeline_authority,
        )
    assert captured
    assert not list(tmp_path.glob("astrid-render-assets-*"))
    assert not (tmp_path / ".remotion-props.json").exists()
    assert all("audio" not in c for c in json.loads(Path(req.timeline_path).read_text())["clips"])


def test_generic_host_materializes_snapshot_without_dropping_audio(tmp_path):
    from astrid.core.execution.generic_host import GenericPackHost

    data = config()
    compiled, pin = admitted(data)
    host = object.__new__(GenericPackHost)
    # Empty synthetic registry: no media or live Runtime lookup is necessary.
    spec = {
        "inputs": {
            "timeline_snapshot": {
                "config": data,
                "registry": {"assets": {}},
                "compiled_audio": compiled,
            },
            "timeline_authority": pin,
        }
    }
    values = host._materialize_inputs(spec, tmp_path / "attempt")
    assert "timeline_snapshot" not in values
    assert values["compiled_audio"] == compiled
    assert values["timeline_authority"] == pin
    assert json.loads(Path(values["timeline"]).read_text()) == data
    assert all("audio" not in c for c in json.loads(Path(values["timeline"]).read_text())["clips"])


def test_replay_bundle_retains_audio_binding_and_request_identity(tmp_path):
    from astrid.core.rendering.service import RenderService
    from astrid.core.rendering.contracts import compute_request_digest

    req = request(tmp_path)
    service = object.__new__(RenderService)
    service._support_reports = {}
    service._transport = SimpleNamespace(last_logs={})
    candidate = SimpleNamespace(
        id="rendering.ffmpeg",
        pack_id="rendering",
        source_kind="builtin",
        manifest_digest="b" * 64,
        manifest=SimpleNamespace(version="test", command=["python"]),
        eligibility=SimpleNamespace(to_dict=lambda: {}, trust_method="test"),
    )
    invocation = SimpleNamespace(
        payload=req,
        candidate=candidate,
        verb="render",
        request_path=tmp_path / "request.json",
        result_path=tmp_path / "result.json",
    )
    bundle = service._build_replay_bundle(
        invocation=invocation, request=req, error=None, success=True
    )
    assert bundle.payload["compiled_audio"] == req.compiled_audio
    assert bundle.payload["timeline_authority"] == req.timeline_authority
    assert bundle.request_digest == compute_request_digest(req.to_dict())
    replay = RenderRequest.from_dict(bundle.payload)
    assert replay.for_backend("rendering.ffmpeg").compiled_audio == req.compiled_audio
