"""Fake-only coverage for catalog-backed legacy fal actions."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.core.contracts.errors import AstridValueError
from astrid.core.generation.backends import fal
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.util.generation_helpers import DEFAULT_OUTPUT_MAX_BYTES
from astrid.core.util.http import HttpClient


@pytest.fixture(scope="module")
def catalog() -> ModelRegistry:
    return ModelRegistry.load_default()


@pytest.fixture
def client() -> Mock:
    return Mock(spec=HttpClient)


def _files(tmp_path: Path, *names: str) -> list[Path]:
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.write_bytes(b"fixture")
    return paths


@pytest.mark.parametrize("model,mode,endpoint", [
    ("hunyuan-video-foley", "sfx", "fal-ai/hunyuan-video-foley"),
    ("minimax-h3", "t2v", "minimax/h3/text-to-video"),
    ("minimax-h3", "v2v", "minimax/h3/reference-to-video"),
    ("seedance-2.0", "v2v", "bytedance/seedance-2.0/reference-to-video"),
    ("seedvr2-upscaler", "upscale", "fal-ai/seedvr/upscale/image/seamless"),
    ("lava-sr", "sfx", "fal-ai/lava-sr"),
    ("deepfilternet3", "sfx", "fal-ai/deepfilternet3"),
])
def test_six_families_have_valid_explicit_cloud_metadata(catalog, model, mode, endpoint):
    _, spec = catalog.get_by_mode(model, mode)
    assert set(spec.backends) == {"cloud"}
    assert spec.backends["cloud"].endpoint == endpoint
    assert set(spec.backends["cloud"].param_map) <= set(spec.supports)
    assert not catalog.backend_available(model, mode, "local")


def test_h3_maps_all_ordered_reference_groups_before_one_submit(catalog, client, tmp_path):
    images = _files(tmp_path, "b.png", "a.png")
    videos = _files(tmp_path, "second.mp4", "first.mp4")
    audios = _files(tmp_path, "two.wav", "one.wav")
    uploaded = []
    def upload(actual_client, path, key):
        assert actual_client is client and key == "fake-key"
        uploaded.append(path)
        return f"https://cdn.example/{path.name}"
    submit = Mock(return_value={"video": "https://cdn.example/out.mp4"})
    result = fal.FalBackend(client=client).submit(catalog.get("minimax-h3"), "v2v", {
        "prompt": "ordered references", "duration": 6, "resolution": "2K", "aspect_ratio": "adaptive",
        "image_refs": images, "video_refs": videos, "audio_refs": audios,
        "timeout_seconds": 3600,
    }, api_key="fake-key", uploader=upload, submitter=submit)
    assert uploaded == images + videos + audios
    assert submit.call_count == 1
    assert submit.call_args.args[1] == "minimax/h3/reference-to-video"
    assert submit.call_args.args[2] == {
        "prompt": "ordered references", "duration": 6, "resolution": "2K", "aspect_ratio": "adaptive",
        "reference_image_urls": [f"https://cdn.example/{path.name}" for path in images],
        "reference_video_urls": [f"https://cdn.example/{path.name}" for path in videos],
        "reference_audio_urls": [f"https://cdn.example/{path.name}" for path in audios],
    }
    assert submit.call_args.kwargs == {"max_wait_sec": 3600}
    assert result == submit.return_value
    client.register_secret.assert_called_once_with("fake-key")


def test_seedance_duration_is_string_false_audio_and_zero_seed_are_preserved(catalog, client, tmp_path):
    image, video = _files(tmp_path, "ref.png", "ref.mp4")
    uploader = Mock(side_effect=lambda _c, path, _k: "https://cdn.example/" + path.name)
    submitter = Mock(return_value={"video": {"url": "https://cdn.example/out.mp4"}})
    fal.FalBackend(client=client).submit(catalog.get("seedance-2.0"), "v2v", {
        "prompt": "a scene", "duration": 4, "resolution": "720p", "aspect_ratio": "16:9",
        "generate_audio": False, "seed": 0, "image_refs": [image], "video_refs": [video],
        "timeout_seconds": 900,
    }, api_key="fake-key", uploader=uploader, submitter=submitter)
    assert submitter.call_args.args[2] == {
        "prompt": "a scene", "duration": "4", "resolution": "720p", "aspect_ratio": "16:9",
        "generate_audio": False, "seed": 0,
        "image_urls": ["https://cdn.example/ref.png"], "video_urls": ["https://cdn.example/ref.mp4"],
    }
    assert submitter.call_args.kwargs == {"max_wait_sec": 900}


def test_foley_preserves_inline_video_and_max_wait(catalog, client, tmp_path):
    clip, = _files(tmp_path, "clip.mov")
    upload = Mock(side_effect=AssertionError("Foley inline transport must not upload"))
    submit = Mock(return_value={"output_audio": {"url": "https://cdn.example/out.wav"}})
    fal.FalBackend(client=client).submit(catalog.get("hunyuan-video-foley"), "sfx", {
        "video_ref": clip, "prompt": "", "max_wait_sec": 720,
    }, api_key="fake-key", uploader=upload, submitter=submit)
    assert submit.call_args.args[2] == {
        "video_url": "data:video/quicktime;base64," + base64.b64encode(b"fixture").decode(),
        "text_prompt": "",
    }
    assert submit.call_args.kwargs == {"max_wait_sec": 720}
    upload.assert_not_called()


@pytest.mark.parametrize("model,extras", [
    ("lava-sr", {"audio_format": "wav", "bitrate": "192k"}),
    ("deepfilternet3", {}),
])
def test_speech_repair_routes_local_audio_through_backend(catalog, client, tmp_path, model, extras):
    audio, = _files(tmp_path, "input.wav")
    upload = Mock(return_value="https://cdn.example/input.wav")
    submit = Mock(return_value={"audio_file": {"url": "https://cdn.example/output.wav"}})
    fal.FalBackend(client=client).submit(catalog.get(model), "sfx", {
        "audio_ref": audio, **extras,
    }, api_key="fake-key", uploader=upload, submitter=submit)
    upload.assert_called_once_with(client, audio, "fake-key")
    assert submit.call_args.args[1] == f"fal-ai/{model}"
    assert submit.call_args.args[2] == {"audio_url": "https://cdn.example/input.wav", **extras}
    assert submit.call_args.kwargs == {}


def test_absent_and_empty_reference_lists_are_omitted(catalog, client):
    submit = Mock(return_value={})
    fal.FalBackend(client=client).submit(catalog.get("minimax-h3"), "v2v", {
        "prompt": "scene", "image_refs": [], "video_refs": None,
    }, api_key="fake-key", submitter=submit)
    assert submit.call_args.args[2] == {"prompt": "scene"}


@pytest.mark.parametrize("field,bad_value", [
    ("image_refs", ["https://caller.example/image.png"]),
    ("video_refs", ["missing.mp4"]),
    ("audio_refs", ["https://caller.example/audio.wav"]),
    ("image_refs", "not-a-list.png"),
    ("audio_refs", [{"path": "unresolved.wav"}]),
])
def test_all_references_refuse_before_credentials_or_any_upload(catalog, client, tmp_path, field, bad_value):
    image, = _files(tmp_path, "valid.png")
    backend = fal.FalBackend(client=client)
    backend._resolve_api_key = Mock(side_effect=AssertionError("no credential lookup"))
    upload = Mock(side_effect=AssertionError("no upload"))
    submit = Mock(side_effect=AssertionError("no submit"))
    with pytest.raises(AstridValueError) as exc:
        backend.submit(catalog.get("minimax-h3"), "v2v", {
            "prompt": "scene", "image_refs": [image], field: bad_value,
        }, uploader=upload, submitter=submit)
    assert exc.value.code == "FAL_REFERENCE_INVALID"
    assert exc.value.field == field
    backend._resolve_api_key.assert_not_called()
    upload.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize("model,field", [("lava-sr", "audio_ref"), ("hunyuan-video-foley", "video_ref")])
def test_scalar_reference_urls_refuse_before_credential_lookup(catalog, client, model, field):
    backend = fal.FalBackend(client=client)
    backend._resolve_api_key = Mock(side_effect=AssertionError("no credential lookup"))
    with pytest.raises(AstridValueError, match="host-materialized"):
        backend.submit(catalog.get(model), "sfx", {field: "https://caller.example/ref"})
    backend._resolve_api_key.assert_not_called()


@pytest.mark.parametrize("timeout", [True, 0, -1, "900"])
def test_invalid_timeout_is_control_refusal_before_provider(catalog, client, timeout):
    backend = fal.FalBackend(client=client)
    backend._resolve_api_key = Mock(side_effect=AssertionError("no credential lookup"))
    with pytest.raises(AstridValueError) as exc:
        backend.submit(catalog.get("minimax-h3"), "t2v", {"prompt": "scene", "timeout_seconds": timeout})
    assert exc.value.code == "FAL_TIMEOUT_INVALID"
    backend._resolve_api_key.assert_not_called()


def test_local_selection_never_falls_back_to_fal(catalog, client):
    backend = fal.FalBackend(client=client)
    backend._resolve_api_key = Mock(side_effect=AssertionError("no credential lookup"))
    with pytest.raises(AstridValueError) as exc:
        backend.submit(catalog.get("minimax-h3"), "t2v", {"prompt": "scene", "execution": "local"})
    assert exc.value.code == "FAL_EXECUTION_UNSUPPORTED"
    backend._resolve_api_key.assert_not_called()


def test_default_submitter_is_resolved_at_call_time(catalog, client, monkeypatch):
    submit = Mock(return_value={"video": "https://cdn.example/out.mp4"})
    monkeypatch.setattr(fal, "fal_submit_and_poll", submit)
    result = fal.FalBackend(client=client).submit(catalog.get("minimax-h3"), "t2v", {
        "prompt": "scene",
    }, api_key="fake-key")
    assert result == submit.return_value
    submit.assert_called_once()


@pytest.mark.parametrize("result", [None, [], "https://cdn.example/video.mp4"])
def test_non_object_provider_response_is_coded_refusal(catalog, client, result):
    with pytest.raises(AstridValueError) as exc:
        fal.FalBackend(client=client).submit(catalog.get("minimax-h3"), "t2v", {
            "prompt": "scene",
        }, api_key="fake-key", submitter=Mock(return_value=result))
    assert exc.value.code == "FAL_RESULT_INVALID"


@pytest.mark.parametrize("result", [
    {"video": "https://cdn.example/output"},
    {"video": {"url": "https://cdn.example/output"}},
    {"audio": {"url": "https://cdn.example/output"}},
    {"audio_file": {"url": "https://cdn.example/output"}},
    {"output_audio": {"url": "https://cdn.example/output"}},
])
def test_legacy_response_shapes_are_accepted(result):
    assert fal._extract_asset_urls(result) == ["https://cdn.example/output"]


def test_generate_calls_shared_submission_boundary(catalog, client, tmp_path):
    backend = fal.FalBackend(client=client)
    backend.submit = Mock(return_value={"video": "https://cdn.example/out.mp4"})
    client.get_bytes.return_value = b"video"
    params = {"prompt": "scene"}
    result = backend.generate(catalog.get("minimax-h3"), "t2v", params, tmp_path / "out")
    backend.submit.assert_called_once_with(catalog.get("minimax-h3"), "t2v", params)
    assert result.image_paths[0].read_bytes() == b"video"
    assert result.model_actual == "minimax/h3/text-to-video"


def test_shared_output_download_is_bounded_and_atomic(client, tmp_path):
    client.get_bytes.return_value = b"audio"
    destination = tmp_path / "nested" / "output.wav"
    path, data = fal.FalBackend.download(client, "https://cdn.example/out.wav", destination, 900)
    assert path == destination and data == path.read_bytes() == b"audio"
    client.get_bytes.assert_called_once_with("https://cdn.example/out.wav", timeout=900, max_bytes=DEFAULT_OUTPUT_MAX_BYTES)


@pytest.mark.parametrize("url", ["http://cdn.example/output", "file:///tmp/output", "not-a-url"])
def test_download_refuses_non_https_before_network(client, tmp_path, url):
    with pytest.raises(AstridValueError) as exc:
        fal.FalBackend.download(client, url, tmp_path / "out.mp4", 900)
    assert exc.value.code == "FAL_OUTPUT_URL_INVALID"
    client.get_bytes.assert_not_called()


@pytest.mark.parametrize("model,mode,result,expected", [
    ("hunyuan-video-foley", "sfx", {"audio": {"url": "https://cdn.example/first.wav"},
                                    "output_audio": {"url": "https://cdn.example/second.wav"}}, "first.wav"),
    ("hunyuan-video-foley", "sfx", {"output_audio": {"url": "https://cdn.example/first.wav"},
                                    "video": {"url": "https://cdn.example/second.mp4"}}, "first.wav"),
    ("lava-sr", "sfx", {"audio": {"url": "https://cdn.example/first.wav"},
                          "audio_file": {"url": "https://cdn.example/second.wav"}}, "first.wav"),
    ("deepfilternet3", "sfx", {"audio": {"url": "https://cdn.example/second.wav"},
                                 "audio_file": {"url": "https://cdn.example/first.wav"}}, "first.wav"),
])
def test_catalog_result_priority_matches_legacy_actions(catalog, client, tmp_path, model, mode, result, expected):
    backend = fal.FalBackend(client=client)
    backend.submit = Mock(return_value=result)
    client.get_bytes.return_value = b"media"
    generated = backend.generate(catalog.get(model), mode, {}, tmp_path / model)
    assert generated.source_urls == ["https://cdn.example/" + expected]


@pytest.mark.parametrize("result", [{}, {"video": {}}, {"video": {"url": None}}])
def test_catalog_generation_requires_expected_output_shape(catalog, client, tmp_path, result):
    backend = fal.FalBackend(client=client)
    backend.submit = Mock(return_value=result)
    with pytest.raises(AstridValueError) as exc:
        backend.generate(catalog.get("minimax-h3"), "t2v", {"prompt": "scene"}, tmp_path / "out")
    assert exc.value.code == "FAL_OUTPUT_MISSING"
    client.get_bytes.assert_not_called()


def test_generate_preserves_explicit_download_timeout_and_optional_seed(catalog, client, tmp_path):
    backend = fal.FalBackend(client=client)
    backend.submit = Mock(return_value={"video": "https://cdn.example/out.mp4"})
    client.get_bytes.return_value = b"media"
    result = backend.generate(catalog.get("seedance-2.0"), "v2v", {
        "prompt": "scene", "timeout_seconds": 1800, "seed": None,
    }, tmp_path / "out")
    assert result.seed_used == 0
    assert client.get_bytes.call_args.kwargs["timeout"] == 1800
