from __future__ import annotations

import json
import ssl
import urllib.parse
from pathlib import Path

from astrid.packs.training.actions.search_loras import run as search_loras


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_search_loras_queries_base_model_and_normalizes(monkeypatch) -> None:
    seen = {}

    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)

    def fake_urlopen(request, timeout, *, context):  # noqa: ANN001
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        return _FakeResponse(
            [
                {
                    "id": "latent-consistency/lcm-lora-sdxl",
                    "author": "latent-consistency",
                    "downloads": 17259,
                    "likes": 768,
                    "gated": False,
                    "private": False,
                    "pipeline_tag": "text-to-image",
                    "library_name": "diffusers",
                    "createdAt": "2023-11-09T00:34:02.000Z",
                    "lastModified": "2023-11-24T13:31:08.000Z",
                    "sha": "abc123",
                    "tags": [
                        "diffusers",
                        "lora",
                        "base_model:stabilityai/stable-diffusion-xl-base-1.0",
                        "base_model:adapter:stabilityai/stable-diffusion-xl-base-1.0",
                        "license:openrail++",
                    ],
                    "siblings": [
                        {"rfilename": "README.md"},
                        {"rfilename": "pytorch_lora_weights.safetensors"},
                    ],
                }
            ]
        )

    monkeypatch.setattr(search_loras.urllib.request, "urlopen", fake_urlopen)

    payload = search_loras.search_loras(
        base_model="stabilityai/stable-diffusion-xl-base-1.0",
        query="cinematic",
        limit=7,
        timeout=12,
    )

    parsed = urllib.parse.urlparse(seen["url"])
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/api/models"
    assert params["filter"] == [
        "lora",
        "base_model:stabilityai/stable-diffusion-xl-base-1.0",
    ]
    assert params["search"] == ["cinematic"]
    assert params["limit"] == ["7"]
    assert params["full"] == ["true"]
    assert seen["timeout"] == 12
    assert "Authorization" not in seen["headers"]

    assert payload["count"] == 1
    result = payload["results"][0]
    assert result["id"] == "latent-consistency/lcm-lora-sdxl"
    assert result["url"] == "https://huggingface.co/latent-consistency/lcm-lora-sdxl"
    assert result["base_model_tags"] == [
        "base_model:stabilityai/stable-diffusion-xl-base-1.0",
        "base_model:adapter:stabilityai/stable-diffusion-xl-base-1.0",
    ]
    assert result["license_tags"] == ["license:openrail++"]
    assert result["safetensors_files"] == ["pytorch_lora_weights.safetensors"]


def test_search_loras_applies_local_match_after_broad_fetch(monkeypatch) -> None:
    seen = {}

    def fake_urlopen(request, timeout, *, context):  # noqa: ANN001
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        seen["url"] = request.full_url
        return _FakeResponse(
            [
                {
                    "id": "demo/general-z-image",
                    "tags": [
                        "lora",
                        "base_model:Tongyi-MAI/Z-Image",
                        "base_model:adapter:Tongyi-MAI/Z-Image",
                    ],
                    "siblings": [{"rfilename": "general.safetensors"}],
                },
                {
                    "id": "demo/photography-z-image",
                    "tags": [
                        "lora",
                        "base_model:Tongyi-MAI/Z-Image",
                        "base_model:adapter:Tongyi-MAI/Z-Image",
                    ],
                    "siblings": [{"rfilename": "Z-TURBO_Photography_35mmPhoto_1536.safetensors"}],
                },
            ]
        )

    monkeypatch.setattr(search_loras.urllib.request, "urlopen", fake_urlopen)

    payload = search_loras.search_loras(
        base_model="Tongyi-MAI/Z-Image",
        match=["photography"],
        limit=5,
        fetch_limit=50,
    )

    params = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert "search" not in params
    assert params["limit"] == ["50"]
    assert payload["candidate_count"] == 2
    assert payload["matched_count"] == 1
    assert payload["results"][0]["id"] == "demo/photography-z-image"
    assert payload["results"][0]["match"]["terms"] == ["photography"]
    assert payload["results"][0]["match"]["fields"] == {
        "photography": ["id", "safetensors_files"]
    }


def test_search_loras_supports_any_match_mode(monkeypatch) -> None:
    def fake_urlopen(request, timeout, *, context):  # noqa: ANN001
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        return _FakeResponse(
            [
                {"id": "demo/photo", "tags": ["base_model:demo/base"], "siblings": []},
                {"id": "demo/realism", "tags": ["realism", "base_model:demo/base"], "siblings": []},
                {"id": "demo/other", "tags": ["base_model:demo/base"], "siblings": []},
            ]
        )

    monkeypatch.setattr(search_loras.urllib.request, "urlopen", fake_urlopen)

    payload = search_loras.search_loras(
        base_model="demo/base",
        match=["photo", "realism"],
        match_mode="any",
    )

    assert payload["matched_count"] == 2
    assert {item["id"] for item in payload["results"]} == {"demo/photo", "demo/realism"}


def test_search_loras_falls_back_to_local_query_when_hub_search_is_empty(monkeypatch) -> None:
    seen_urls = []

    def fake_urlopen(request, timeout, *, context):  # noqa: ANN001
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        seen_urls.append(request.full_url)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        if params.get("search") == ["photoreal"]:
            return _FakeResponse([])
        return _FakeResponse(
            [
                {
                    "id": "demo/z-image-photo",
                    "tags": ["base_model:Tongyi-MAI/Z-Image"],
                    "siblings": [{"rfilename": "skin texture Photorealistic style.safetensors"}],
                },
                {
                    "id": "demo/z-image-other",
                    "tags": ["base_model:Tongyi-MAI/Z-Image"],
                    "siblings": [{"rfilename": "other.safetensors"}],
                },
            ]
        )

    monkeypatch.setattr(search_loras.urllib.request, "urlopen", fake_urlopen)

    payload = search_loras.search_loras(
        base_model="Tongyi-MAI/Z-Image",
        query="photoreal",
        limit=10,
    )

    assert len(seen_urls) == 2
    assert payload["fallback_used"] is True
    assert payload["match"] == ["photoreal"]
    assert payload["matched_count"] == 1
    assert payload["results"][0]["id"] == "demo/z-image-photo"
    assert payload["guidance"]["status"] == "ok"
    assert "retried broad base-model search" in payload["guidance"]["messages"][0]


def test_search_loras_guides_empty_results(monkeypatch) -> None:
    def fake_urlopen(request, timeout, *, context):  # noqa: ANN001
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        return _FakeResponse([])

    monkeypatch.setattr(search_loras.urllib.request, "urlopen", fake_urlopen)

    payload = search_loras.search_loras(
        base_model="Tongyi-MAI/Z-Image",
        match=["photo", "realism"],
        match_mode="all",
        limit=10,
        fetch_limit=100,
    )

    assert payload["count"] == 0
    assert payload["guidance"]["status"] == "empty"
    assert any("--match-mode any" in command for command in payload["guidance"]["next_commands"])
    assert "photoreal" in payload["guidance"]["suggested_match_terms"]


def test_search_loras_flags_shorthand_base_model(monkeypatch) -> None:
    def fake_urlopen(request, timeout, *, context):  # noqa: ANN001
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        return _FakeResponse([])

    monkeypatch.setattr(search_loras.urllib.request, "urlopen", fake_urlopen)

    payload = search_loras.search_loras(
        base_model="Z-Image-Turbo",
        query="photoreal",
        limit=10,
        fetch_limit=100,
    )

    assert any("owner/name" in message for message in payload["guidance"]["messages"])
    assert any("--base-model-match Z-Image-Turbo" in command for command in payload["guidance"]["next_executor_commands"])


def test_discover_base_models_counts_non_adapter_tags(monkeypatch) -> None:
    def fake_urlopen(request, timeout, *, context):  # noqa: ANN001
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        return _FakeResponse(
            [
                {
                    "id": "demo/a",
                    "tags": [
                        "lora",
                        "base_model:Tongyi-MAI/Z-Image-Turbo",
                        "base_model:adapter:Tongyi-MAI/Z-Image-Turbo",
                    ],
                },
                {
                    "id": "demo/b-realism",
                    "tags": [
                        "realism",
                        "base_model:Tongyi-MAI/Z-Image-Turbo",
                        "base_model:adapter:Tongyi-MAI/Z-Image-Turbo",
                    ],
                },
                {
                    "id": "demo/c",
                    "tags": [
                        "lora",
                        "base_model:Tongyi-MAI/Z-Image",
                        "base_model:adapter:Tongyi-MAI/Z-Image",
                    ],
                },
            ]
        )

    monkeypatch.setattr(search_loras.urllib.request, "urlopen", fake_urlopen)

    payload = search_loras.discover_base_models(match=["realism"], limit=100)

    assert payload["matched_count"] == 1
    assert payload["base_models"] == [
        {
            "id": "Tongyi-MAI/Z-Image-Turbo",
            "count": 1,
            "url": "https://huggingface.co/Tongyi-MAI/Z-Image-Turbo",
        }
    ]
    assert payload["guidance"]["status"] == "ok"
    assert any(
        "--base-model Tongyi-MAI/Z-Image-Turbo" in command
        for command in payload["guidance"]["next_commands"]
    )
    assert any(
        "--match photo --match realism --match 35mm" in command
        for command in payload["guidance"]["next_executor_commands"]
    )


def test_discover_base_models_can_filter_extracted_model_ids(monkeypatch) -> None:
    def fake_urlopen(request, timeout, *, context):  # noqa: ANN001
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        return _FakeResponse(
            [
                {
                    "id": "demo/z-image-helper",
                    "tags": ["base_model:HuggingFaceH4/zephyr-7b-beta"],
                },
                {
                    "id": "demo/z-image-lora",
                    "tags": ["base_model:Tongyi-MAI/Z-Image-Turbo"],
                },
            ]
        )

    monkeypatch.setattr(search_loras.urllib.request, "urlopen", fake_urlopen)

    payload = search_loras.discover_base_models(
        match=["z-image"],
        base_model_match=["z-image"],
        limit=100,
    )

    assert payload["base_models"] == [
        {
            "id": "Tongyi-MAI/Z-Image-Turbo",
            "count": 1,
            "url": "https://huggingface.co/Tongyi-MAI/Z-Image-Turbo",
        }
    ]


def test_discover_base_models_guides_repo_match_without_model_match(monkeypatch) -> None:
    def fake_urlopen(request, timeout, *, context):  # noqa: ANN001
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        return _FakeResponse(
            [
                {
                    "id": "demo/z-image-helper",
                    "tags": ["base_model:HuggingFaceH4/zephyr-7b-beta"],
                }
            ]
        )

    monkeypatch.setattr(search_loras.urllib.request, "urlopen", fake_urlopen)

    payload = search_loras.discover_base_models(match=["z-image"], limit=100)

    assert payload["count"] == 1
    assert any("--base-model-match" in message for message in payload["guidance"]["messages"])


def test_main_writes_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        search_loras,
        "search_loras",
        lambda **kwargs: {"base_model": kwargs["base_model"], "count": 0, "results": []},
    )
    out = tmp_path / "search-loras.json"

    rc = search_loras.main(["--base-model", "demo/base", "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "base_model": "demo/base",
        "count": 0,
        "results": [],
    }
