"""G07 bounded request expansion and real OpenAI adapter with fake transport."""
from __future__ import annotations

import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.generation.backends import openai
from astrid.core.generation.storage_policy import OPENAI_IMAGE_STORAGE_POLICY
from astrid.core.model_catalog.registry import ModelRegistry


def _png(color):
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _descriptor(data):
    return {"digest": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
            "filename": "ref.png", "media_type": "image/png"}


def _request(**overrides):
    return {"prompt": "a red triangle", "model": "gpt-image-2", **overrides}


def test_authenticated_expansion_binds_exact_cardinality_and_original_defaults():
    data = b'{"prompt":"first","n":2}\n{"prompt":"second"}\n'
    raw = _request(prompt=None, prompts_file={"digest": "a" * 64}, count=2)
    normalized = openai.normalize_request(raw, prompt_bytes=data)
    assert normalized["count"] == 4
    assert normalized["n"] == 2
    assert [job["prompt"] for job in json.loads(normalized["jobs"])] == ["first", "second"]
    assert openai.normalize_request(normalized, prompt_bytes=data, require_frozen=True) == normalized
    with pytest.raises(ValueError, match="frozen"):
        openai.normalize_request(normalized, prompt_bytes=data.replace(b"first", b"other"), require_frozen=True)
    with pytest.raises(ValueError, match="reserved"):
        openai.normalize_request(normalized, prompt_bytes=data)


@pytest.mark.parametrize("data", [b"", b"\xff", b"{bad", b"[]", b'[1]', b'{"prompt":"one","prompt":"two"}',
                                  b'{"prompt":"x","n":true}', b'{"prompt":"x","n":11}',
                                  b'{"prompt":"x","out":"../../file"}', b"x" * (1024 * 1024 + 1)])
def test_invalid_authenticated_prompt_bytes_refuse(data):
    with pytest.raises((ValueError, TypeError)):
        openai.normalize_request({"prompts_file": {"digest": "a" * 64}}, prompt_bytes=data)


@pytest.mark.parametrize("overrides", [{"count": True}, {"count": 11}, {"n": 1, "count": 2},
    {"quality": "extreme"}, {"quality": []}, {"output_compression": True}, {"size": "1000x1000"},
    {"background": "transparent"}, {"mode": "edit"}, {"seed": 1}, {"execution": "cloud"}])
def test_invalid_request_options_refuse(overrides):
    with pytest.raises(ValueError):
        openai.normalize_request(_request(**overrides))


def test_total_job_and_output_bounds():
    for data in (b'"x"\n' * 33, b'{"prompt":"x","n":10}\n' * 4):
        with pytest.raises(ValueError):
            openai.normalize_request({"prompts_file": "authenticated.jsonl"}, prompt_bytes=data)
    with pytest.raises(ValueError, match="authenticated"):
        openai.normalize_request({"prompts_file": "unread.jsonl"})
    with pytest.raises(ValueError, match="mutually exclusive"):
        openai.normalize_request(_request(prompts_file="authenticated.jsonl"), prompt_bytes=b'"x"')


def test_ordered_reference_projection_and_duplicate_policy():
    first, second = _descriptor(_png("red")), _descriptor(_png("blue"))
    normalized = openai.normalize_request(_request(image_ref=[second, first]))
    assert normalized["mode"] == "edit"
    assert normalized["image_ref_00"] == second
    assert normalized["image_ref_01"] == first
    with pytest.raises(ValueError, match="duplicate"):
        openai.normalize_request(_request(image_ref=[first, first]))
    with pytest.raises(ValueError, match="consecutive"):
        openai.normalize_request(_request(image_ref_01=first))
    with pytest.raises(ValueError):
        openai.normalize_request(_request(image_ref=[first] * 17))
    with pytest.raises(ValueError):
        openai.normalize_request(_request(mode="t2i", image_ref=first))


def test_policy_matches_declared_separate_envelope_and_reference_formats():
    assert OPENAI_IMAGE_STORAGE_POLICY.estimate == {"scratch_bytes": 271581184, "output_bytes": 537919488}
    assert openai.validate_reference_bytes(_png("red")) == "image/png"
    for data in (b"not-an-image", b"", b"x" * (16 * 1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            openai.validate_reference_bytes(data)


def test_catalog_explicit_openai_cells_preserve_local_models():
    registry = ModelRegistry.load_default()
    assert registry.backend_available("z-image", "t2i", "local")
    for model in ("gpt-image-2", "gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini"):
        for mode in ("t2i", "edit"):
            entry, mode_spec = registry.get_by_mode(model, mode)
            assert set(mode_spec.backends) == {"openai"}
            assert not registry.backend_available(model, mode, "local")


class _Response:
    def __init__(self, data):
        self.data = json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def read(self, maximum):
        return self.data[:maximum]


@pytest.mark.parametrize("edit", [False, True])
def test_real_adapter_uses_fixed_endpoint_and_retains_ordered_edit_bytes(tmp_path, monkeypatch, edit):
    red, blue = _png("red"), _png("blue")
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    first.write_bytes(red)
    second.write_bytes(blue)
    refs = {"image_ref": [str(second), str(first)]} if edit else {}
    normalized = openai.normalize_request(_request(count=2, **refs))
    calls = []
    output = base64.b64encode(_png("green")).decode()
    def fake_urlopen(request, *, timeout):
        calls.append(request)
        assert timeout == 180
        assert request.get_header("Authorization") == "Bearer fake"
        assert request.full_url == "https://api.openai.com/v1/images/" + ("edits" if edit else "generations")
        if edit:
            assert request.data.index(blue) < request.data.index(red)
            assert request.data.count(b'name="image[]"') == 2
        else:
            assert json.loads(request.data)["n"] == 2
        return _Response({"data": [{"b64_json": output}] * 2, "created": 1})
    monkeypatch.setattr(openai, "urlopen", fake_urlopen)
    def fake_key(**_kwargs):
        # Change the source after validation; multipart must use retained bytes.
        if edit:
            first.write_bytes(b"changed after validation")
        return "fake"
    backend = openai.OpenAIBackend(key_resolver=fake_key)
    entry, _mode = ModelRegistry.load_default().get_by_mode("gpt-image-2", normalized["mode"])
    result = backend.generate(entry, normalized["mode"], normalized, tmp_path / "run" / "images")
    assert len(calls) == 1
    assert result.ok
    assert len(result.image_paths) == 2
    assert [item["ordinal"] for item in result.manifest["outputs"]] == [0, 1]
    assert all(path.read_bytes() == _png("green") for path in result.image_paths)


@pytest.mark.parametrize("response", [{"data": []}, {"data": [{"b64_json": "!!!!"}]},
                                     {"data": [{"url": "https://untrusted.invalid"}]},
                                     {"data": [{"b64_json": ""}]}, {"data": [{"b64_json": "eA=="}] * 2}])
def test_provider_malformed_cardinality_and_decoding_stage_no_outputs(tmp_path, response):
    target = tmp_path / "output.png"
    with pytest.raises(ValueError):
        openai._write_images(response, [target], False)
    assert not target.exists()


@pytest.mark.parametrize("prompt", ["x" * 32_000, "\U0001f30a" * 32_000])
def test_manifest_storage_reservation_is_refused_before_provider(prompt):
    jobs = 17 if prompt.isascii() else 4
    data = json.dumps([{"prompt": prompt}] * jobs, ensure_ascii=False).encode()
    with pytest.raises(ValueError, match="manifest|1 MiB"):
        openai.normalize_request({"prompts_file": "authenticated.jsonl"}, prompt_bytes=data)


@pytest.mark.parametrize("metadata", [{"usage": {"tokens": "x" * 4097}}, {"usage": []},
                                      {"created": {"unexpected": "object"}}, {"created": True}])
def test_provider_metadata_is_bounded_before_output_staging(metadata):
    with pytest.raises(ValueError):
        openai._validate_response_metadata(metadata)


def test_optional_null_file_inputs_are_omitted_from_canonical_t2i():
    normalized = openai.normalize_request(_request(prompts_file=None, image_ref_00=None,
                                                   image_ref_01="", timeout=180))
    assert normalized["mode"] == "t2i"
    assert "prompts_file" not in normalized
    assert not set(openai.REF_PORTS).intersection(normalized)
    assert openai.normalize_request(normalized, require_frozen=True) == normalized


@pytest.mark.parametrize("timeout", [True, 0, -1, "180", 1.5])
def test_invalid_timeout_refuses_before_admission(timeout):
    with pytest.raises(ValueError):
        openai.normalize_request(_request(timeout=timeout))


def test_scalar_null_defaults_match_explicit_managed_transport_defaults():
    normalized = openai.normalize_request(_request(n=None, count=None, timeout=None))
    assert normalized["n"] == normalized["count"] == 1
    assert normalized["timeout"] == 180
    assert normalized == openai.normalize_request(_request(n=1, count=1, timeout=180))
    assert normalized == openai.normalize_request(normalized, require_frozen=True)
