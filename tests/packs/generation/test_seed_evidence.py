"""Seed evidence follows completed fal responses through byte-verified outputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.request import Request

import pytest

from astrid.core._shared.result_manifest import validate_result_manifest
from astrid.core.generation.backends.fal import FalBackend
from astrid.core.util.http import HttpClient
from tests.packs.builtin.generate_image.test_e2e import _build_fal_transport


def _response_transport(monkeypatch, response_seeds, *, fail_after_first=False):
    """Reuse the recorded one-image response, replacing only response evidence.

    fal-ai/flux/dev documents response.seed as the generated image's seed:
    https://fal.ai/models/fal-ai/flux/dev/api . Separate completed N=1 calls
    disclose separate seeds; this fixture invents no per-image response fields.
    """
    import astrid.core.util.http as http_module

    state = _build_fal_transport("flux-dev")
    submitted_seeds = []
    completed = 0

    def transport(request: Request):
        nonlocal completed
        if request.method == "POST" and "/requests/" not in request.full_url:
            submitted_seeds.append(json.loads(request.data)["seed"])
        if request.full_url == state.response_url:
            if fail_after_first and completed:
                raise ValueError("second provider response failed")
            status, body = state._transport(request)
            response = json.loads(body)
            seed = response_seeds[completed]
            if seed is None:
                response.pop("seed", None)
            else:
                response["seed"] = seed
            response["request_id"] = f"recorded-response-{completed}"
            completed += 1
            return status, json.dumps(response).encode()
        return state._transport(request)

    monkeypatch.setattr(http_module, "_default_client", HttpClient(transport=transport))
    # Bypass credential discovery even if the workstation has a configured key.
    monkeypatch.setattr(FalBackend, "_resolve_api_key", lambda self: "fixture-key")
    return submitted_seeds


def _argv(out: Path, count: int) -> list[str]:
    return [
        "--model", "flux-dev", "--mode", "t2i", "--execution", "cloud",
        "--storage-policy-version", "astrid.cloud-t2i.registry.v1",
        "--prompt", "response seed evidence", "--seed", "17",
        "--count", str(count), "--steps", "28", "--size", "1536x1024",
        "--out", str(out),
    ]


@pytest.mark.parametrize(
    "response_seeds,scalar",
    [([901], 901), ([0], 0), ([None], None), ([901, 372], None),
     ([901, None], None), ([33, 33], 33)],
)
def test_attested_seeds_stay_with_verified_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response_seeds, scalar,
) -> None:
    from astrid.packs.generation.actions.generate_image.run import generate_core

    submitted = _response_transport(monkeypatch, response_seeds)
    out = tmp_path / "out"
    result = generate_core(_argv(out, len(response_seeds)))
    manifest = json.loads((out / "manifest.json").read_text())
    verified = validate_result_manifest(manifest, staging_root=out)

    assert submitted == list(range(17, 17 + len(response_seeds)))
    # These remain submitted request values, including bounded batch base seed.
    assert manifest["seed"] == manifest["request"]["seed"] == manifest["inputs"]["seed"] == 17
    assert manifest["seed_used"] == result.seed_used == scalar
    assert [row["seed_used"] for row in manifest["outputs"]] == response_seeds
    assert len(result.image_paths) == len(verified.outputs) == len(response_seeds)
    expected_map = {}
    for ordinal, (row, checked, seed, path) in enumerate(zip(
        manifest["outputs"], verified.outputs, response_seeds, result.image_paths,
    )):
        assert checked.ordinal == row["ordinal"] == ordinal
        assert checked.path == row["path"] == path.relative_to(out).as_posix()
        assert checked.bytes == row["bytes"] == path.stat().st_size
        assert checked.content_hash == row["content_hash"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if seed is not None:
            expected_map[path] = seed
    assert result.output_seeds == expected_map
    # The existing result JSON contract must retain output identity and evidence.
    assert type(result).from_dict(json.loads(json.dumps(result.to_dict()))).output_seeds == expected_map


def test_partial_manifest_keeps_completed_response_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.packs.generation.actions.generate_image.run import generate_core

    submitted = _response_transport(monkeypatch, [901], fail_after_first=True)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="second provider response failed"):
        generate_core(_argv(out, 2))
    manifest = json.loads((out / "manifest.json").read_text())
    verified = validate_result_manifest(manifest, staging_root=out)
    assert submitted == [17, 18]
    assert manifest["seed"] == manifest["request"]["seed"] == 17
    assert manifest["seed_used"] == manifest["outputs"][0]["seed_used"] == 901
    assert len(verified.outputs) == 1
    assert verified.outputs[0].path == manifest["outputs"][0]["path"]
