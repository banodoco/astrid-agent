from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from tests.lanes import unavailable

from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.vibecomfy.asset_manifest import read_archive
from astrid.sdk.pagination import paged_rows
from tests.helpers.runtime import locate_runtime


def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)




@pytest.mark.integration
def test_installed_cpu_profile_binds_actual_product_roots(monkeypatch):
    import sys
    from astrid_runtime.bootstrap import SourceProfile
    from tests.helpers import h3_cpu_runtime as helper

    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    root = helper._INSTALLED_EXECUTION_ROOT
    selected = SourceProfile("astrid", str(root), str(root), runtime_environment=sys.prefix)
    assert helper._selected_execution_roots(selected, python=Path(sys.executable)) == (root, root)


@pytest.mark.parametrize("difference", ["source", "runtime", "environment", "command", "python", "profile", "alias"])
@pytest.mark.integration
def test_installed_cpu_profile_rejects_execution_drift(tmp_path, monkeypatch, difference):
    import sys
    from dataclasses import replace
    from astrid_runtime.bootstrap import SourceProfile
    from tests.helpers import h3_cpu_runtime as helper

    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    root = helper._INSTALLED_EXECUTION_ROOT
    selected = SourceProfile("astrid", str(root), str(root), runtime_environment=sys.prefix)
    python = Path(sys.executable)
    if difference == "source": selected = replace(selected, source_checkout=str(helper.ASTRID_ROOT))
    if difference == "runtime": selected = replace(selected, runtime_checkout=str(helper.RUNTIME_ROOT))
    if difference == "environment": selected = replace(selected, runtime_environment=str(tmp_path))
    if difference == "command": selected = replace(selected, runtime_command=(str(python),))
    if difference == "python": python = tmp_path / "different-python"
    if difference == "profile": monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "unrecognized")
    if difference == "alias":
        alias = tmp_path / "product-alias"
        alias.symlink_to(root, target_is_directory=True)
        selected = replace(selected, source_checkout=str(alias), runtime_checkout=str(alias))
    with pytest.raises(RuntimeError):
        helper._selected_execution_roots(selected, python=python)


@pytest.mark.integration
def test_source_cpu_profile_roots_remain_source_bound(monkeypatch):
    import sys
    from astrid_runtime.bootstrap import SourceProfile
    from tests.helpers import h3_cpu_runtime as helper

    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "source")
    selected = SourceProfile("astrid", str(helper.RUNTIME_ROOT), str(helper.ASTRID_ROOT), runtime_environment=sys.prefix)
    assert helper._selected_execution_roots(selected) == (helper.RUNTIME_ROOT, helper.ASTRID_ROOT)


@pytest.mark.installed
def test_installed_cpu_origins_are_real_and_reject_mixed_source():
    import sys
    from astrid.sdk.host_bootstrap import _installed_execution_origins, PackHostBootstrapError
    from tests.helpers import h3_cpu_runtime as helper

    origins = _installed_execution_origins(helper._INSTALLED_EXECUTION_ROOT, sys.executable)
    assert set(origins) == {
        "host", "child", "action", "client", "sdk",
        "runtime_protocol", "runtime_protocol.platform", "astrid_runtime",
    }
    assert all(Path(path).is_relative_to(helper._INSTALLED_EXECUTION_ROOT) for path in origins.values())
    with pytest.raises(PackHostBootstrapError):
        _installed_execution_origins(helper.ASTRID_ROOT, sys.executable)


@pytest.mark.installed
def test_installed_cpu_actions_retain_candidate_content_pins(tmp_path):
    from tests.helpers import h3_cpu_runtime as helper

    # The candidate manifest pins each executable action by the SHA-256 of the checkout's bytes; the installed copy
    # must carry the same bytes (a wheel built from this checkout does). The manifest is written here from the checkout.
    pinned = (
        "astrid/packs/h3_av/pack.yaml",
        "astrid/packs/h3_av/actions/transform/run.py",
        "astrid/packs/h3_av/actions/transform/operation.py",
        "astrid/packs/vibecomfy/pack.yaml",
        "astrid/packs/vibecomfy/actions/run/run.py",
    )
    manifest = {
        "schema": helper.CANDIDATE_SOURCE_MANIFEST_SCHEMA,
        "synthetic_inference": True,
        "synthetic_inference_classification": "synthetic-inference-only",
        "source_files": {name: hashlib.sha256((helper.ASTRID_ROOT / name).read_bytes()).hexdigest() for name in pinned},
    }
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(manifest))
    assert helper.candidate_manifest_path_from_environment({"ASTRID_H3_CANDIDATE_MANIFEST": str(candidate)}) == candidate.resolve()
    helper._verify_installed_candidate_actions(candidate)
    manifest["source_files"]["astrid/packs/h3_av/actions/transform/run.py"] = "0" * 64
    drift = tmp_path / "candidate-drift.json"
    drift.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="installed action differs"):
        helper._verify_installed_candidate_actions(drift)


@pytest.mark.installed
def test_installed_cpu_renderer_uses_real_provisioned_outputs():
    import sys
    from astrid.core.rendering import _registry_origin
    from astrid.packs.rendering.rendering.renderers.remotion import run
    from tests.helpers import h3_cpu_runtime as helper

    assert Path(run.__file__).is_relative_to(helper._INSTALLED_EXECUTION_ROOT)
    assert run.gen_effect_registry is not None
    carrier = Path(run.gen_effect_registry.__file__).resolve()
    assert carrier == helper._INSTALLED_EXECUTION_ROOT / "astrid/core/rendering/_effect_registry.py"
    assert not carrier.is_relative_to(helper.ASTRID_ROOT)
    project = Path(os.environ["ASTRID_REMOTION_PROJECT_DIR"])
    paths = run._registry_output_paths(project)
    assert len(paths) == 3
    assert {p.name for p in paths} == {"effects.generated.ts", "animations.generated.ts", "transitions.generated.ts"}
    # The installed Composition release is a built, immutable package. Host
    # registries belong beside the host entry point, so an installed run must
    # work without the retired package TypeScript tree.
    assert not (project / "node_modules/@banodoco/timeline-composition/typescript/src").exists()
    assert run.gen_effect_registry.HOST_SRC == project / "src"
    assert set(run.gen_effect_registry.OUTPUTS.values()) == set(paths)
    state_path = project / run._REGISTRY_STATE_PATH
    assert state_path.is_file() and state_path.stat().st_size > 0
    argv = _registry_origin.installed_argv(sys.executable, run.remotion_runtime.__file__, generate=True)
    operation = argv[argv.index("-c") + 1]
    assert argv[argv.index("-c") + 2] == str(helper._INSTALLED_EXECUTION_ROOT)
    assert "from astrid.core.rendering._effect_registry import main" in operation
    assert "scripts/gen_effect_registry.py" not in operation
    assert str(helper.ASTRID_ROOT / "scripts" / "gen_effect_registry.py") not in argv
    run._regenerate_element_registries(project, None)
    state = run._read_registry_state(project)
    assert state == run.gen_effect_registry.compute_generated_registry_state()
    assert run._registry_outputs_match_state(project, state)
    assert all(p.is_file() and p.stat().st_size > 0 for p in paths)



def _attested_parent_clone(tmp_path, monkeypatch):
    """The detached pinned VibeComfy clone the helper itself builds, with the parent import state cleared.

    The parent process imports VibeComfy from the selected checkout; the attested clone is a second,
    separate root, so the parent's modules are removed here (the runtime under test restores them).
    """
    import sys
    from tests.helpers import h3_cpu_runtime as helper

    monkeypatch.setattr(helper, "_HOST_EXECUTION_PROFILE", "installed")
    checkout = helper._prepare_clean_vibecomfy_checkout(tmp_path)
    prior_path = tuple(sys.path)
    prior_modules = {name: module for name, module in sys.modules.items()
                     if name == "vibecomfy" or name.startswith("vibecomfy.")}
    for name in prior_modules:
        del sys.modules[name]
    runtime = helper.H3CpuRuntime.__new__(helper.H3CpuRuntime)
    runtime._prior_sys_path = prior_path
    runtime._prior_vibecomfy_modules = prior_modules
    runtime._prior_environment = {}
    return helper, checkout, runtime, prior_path, prior_modules


@pytest.mark.integration
def test_installed_cpu_parent_vibe_origin_and_exact_restoration(tmp_path, monkeypatch):
    import sys

    helper, checkout, runtime, prior_path, prior_modules = _attested_parent_clone(tmp_path, monkeypatch)
    try:
        helper._expose_owned_vibecomfy_checkout(checkout)
        import vibecomfy.runtime.session as session
        assert Path(session.__file__).resolve().is_relative_to(checkout)
        assert callable(session._session_composite_ownership_verified)
        assert sys.path[0] == str(checkout)
    finally:
        runtime._restore_parent_state()
    assert tuple(sys.path) == prior_path
    assert {name: module for name, module in sys.modules.items()
            if name == "vibecomfy" or name.startswith("vibecomfy.")} == prior_modules


@pytest.mark.integration
def test_installed_cpu_parent_vibe_rejects_foreign_origin_and_alias(tmp_path, monkeypatch):
    import sys

    helper, checkout, runtime, before, prior_modules = _attested_parent_clone(tmp_path, monkeypatch)
    try:
        helper._expose_owned_vibecomfy_checkout(checkout)
        selected_path = tuple(sys.path)
        with pytest.raises(RuntimeError, match="module differs"):
            helper._expose_owned_vibecomfy_checkout(helper.VIBECOMFY_SOURCE_ROOT)
        assert tuple(sys.path) == selected_path
    finally:
        runtime._restore_parent_state()
    alias = tmp_path / "vibe-alias"
    alias.symlink_to(checkout, target_is_directory=True)
    try:
        with pytest.raises(RuntimeError, match="unavailable or aliased"):
            helper._expose_owned_vibecomfy_checkout(alias)
        assert tuple(sys.path) == before
    finally:
        runtime._restore_parent_state()


def _boundary_blockers() -> list[str]:
    checkout = Path(__file__).resolve().parents[3]
    receiving_root = checkout.parents[1]
    runtime_root = locate_runtime()
    vibecomfy_root = Path(
        os.environ.get("ASTRID_VIBECOMFY_CHECKOUT") or receiving_root / "repos" / "vibecomfy"
    )
    profile = (
        Path(os.environ.get("ASTRID_H3_T9_READINESS_PROFILE"))
        if os.environ.get("ASTRID_H3_T9_READINESS_PROFILE")
        else receiving_root
        / "repos"
        / "Astrid"
        / ".otto/runs/h3-transform-pack-20260923/fixtures/t9-public-h3-20260925-9/support/runtime/t9-vibecomfy-readiness.json"
    )
    blockers = []
    if runtime_root is None:
        blockers.append("Runtime checkout unavailable: set ASTRID_RUNTIME_CHECKOUT")
    if not vibecomfy_root.is_dir():
        blockers.append(f"VibeComfy checkout unavailable at {vibecomfy_root}")
    if not profile.is_file():
        blockers.append(f"approved T9 readiness profile unavailable at {profile}")
    return blockers


@pytest.mark.integration
def test_public_cpu_boundary_preflight_does_not_require_a_prior_qualification_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "synthetic-t9-template.json"
    profile.write_text(json.dumps({"profile_template": "test-only"}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_H3_T9_READINESS_PROFILE", str(profile))
    monkeypatch.delenv("ASTRID_H3_CHARACTER_SWAP_CPU_QUALIFICATION_RECEIPT", raising=False)

    assert _boundary_blockers() == []


@pytest.mark.integration
def test_h3_public_workspace_helper_pin_matches_current_shared_fixture() -> None:
    from tests.helpers import h3_cpu_runtime

    fixture = h3_cpu_runtime.PUBLIC_WORKSPACE_FIXTURE
    assert fixture.is_file() and not fixture.is_symlink()
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == (
        h3_cpu_runtime.PUBLIC_WORKSPACE_FIXTURE_SHA256
    )






def test_h3_synthetic_readiness_profile_uses_digest_bound_model_root(tmp_path: Path) -> None:
    from astrid.core.generation.model_root import model_root_binding_from_profile
    from tests.helpers.h3_cpu_runtime import _synthetic_model_root_binding

    model_root = tmp_path / "models"
    model_paths = (
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae/minimax_h3_video_vae_int8_convrot.safetensors",
        "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "loras/h3_character_swap_pro4500_1000.safetensors",
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
    )
    for relative in model_paths:
        model = model_root / relative
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(f"synthetic model fixture: {relative}".encode())

    payload = _synthetic_model_root_binding(model_root, model_paths)
    binding = model_root_binding_from_profile({"launch": {"model_root": payload}})

    assert payload["schema_version"] == 1
    assert binding.path == model_root.resolve()
    assert len(binding.inventory) == len(model_paths)
    assert binding.qualification_identity is None




def test_candidate_manifest_resolves_from_environment_without_runtime_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.helpers.h3_cpu_runtime import (
        CANDIDATE_SOURCE_MANIFEST_SCHEMA,
        candidate_manifest_path_from_environment,
    )

    manifest_path = tmp_path / "candidate.json"
    manifest_path.write_text(json.dumps({
        "schema": CANDIDATE_SOURCE_MANIFEST_SCHEMA,
        "synthetic_inference": True,
        "synthetic_inference_classification": "synthetic-inference-only",
    }), encoding="utf-8")
    monkeypatch.setenv("ASTRID_H3_CANDIDATE_MANIFEST", str(manifest_path))

    assert candidate_manifest_path_from_environment() == manifest_path.resolve()


@pytest.mark.parametrize("bad_value", [None, "relative-candidate.json"])
def test_candidate_manifest_rejects_missing_or_relative_environment_path(
    bad_value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.helpers.h3_cpu_runtime import candidate_manifest_path_from_environment

    if bad_value is None:
        monkeypatch.delenv("ASTRID_H3_CANDIDATE_MANIFEST", raising=False)
    else:
        monkeypatch.setenv("ASTRID_H3_CANDIDATE_MANIFEST", bad_value)

    with pytest.raises(RuntimeError, match="ASTRID_H3_CANDIDATE_MANIFEST"):
        candidate_manifest_path_from_environment()


def test_candidate_manifest_rejects_symlink_environment_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.helpers.h3_cpu_runtime import candidate_manifest_path_from_environment

    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema": "astrid-h3-candidate-source-manifest/v1",
        "synthetic_inference": True,
        "synthetic_inference_classification": "synthetic-inference-only",
    }), encoding="utf-8")
    link = tmp_path / "candidate-link.json"
    link.symlink_to(target)
    monkeypatch.setenv("ASTRID_H3_CANDIDATE_MANIFEST", str(link))

    with pytest.raises(RuntimeError, match="non-symlink"):
        candidate_manifest_path_from_environment()




@pytest.mark.integration
def test_character_swap_digest_override_is_source_and_inventory_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.packs.h3_av.shared import generation

    production_digest = generation.CHARACTER_SWAP_SHA256
    assert production_digest == "4b2a3f420ae804c0aa3422761ff84dbd1bf52eef6900ffab6d2e66df63cb4e79"
    monkeypatch.delenv("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE", raising=False)
    assert generation.character_swap_asset_sha256() == production_digest

    checkout = Path(__file__).resolve().parents[3]
    fixture_relative = "tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py"
    source_relatives = (
        fixture_relative,
        "astrid/packs/vibecomfy/actions/run/run.py",
        "astrid/packs/vibecomfy/pack.yaml",
        "astrid/packs/h3_av/shared/generation.py",
        "astrid/packs/h3_av/shared/compile.py",
    )
    source_files = {
        relative: hashlib.sha256((checkout / relative).read_bytes()).hexdigest()
        for relative in source_relatives
    }
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps({
        "schema": "astrid-h3-candidate-source-manifest/v1",
        "synthetic_inference": True,
        "synthetic_inference_classification": "synthetic-inference-only",
        "source_files": source_files,
    }, sort_keys=True), encoding="utf-8")

    model_root = tmp_path / "models"
    model_path = model_root / "loras" / generation.CHARACTER_SWAP_FILENAME
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"source-bound synthetic Character Swap model")
    model_digest = "sha256:" + hashlib.sha256(model_path.read_bytes()).hexdigest()
    model_asset = {
        "name": generation.CHARACTER_SWAP_FILENAME,
        "subdir": "loras",
        "sha256": model_digest,
        "size": model_path.stat().st_size,
    }
    from astrid.core.generation.model_root import canonical_model_inventory_digest

    bound_model_root = {
        "schema_version": 1,
        "path": str(model_root.resolve()),
        "inventory": [model_asset],
        "inventory_digest": canonical_model_inventory_digest([model_asset]),
        "qualification_identity": None,
    }
    fixture_path = checkout / fixture_relative
    evidence_root = tmp_path / "t9-child-evidence"
    evidence_root.mkdir(mode=0o700)
    marker_base = evidence_root / "activation-marker.json"
    calls_path = evidence_root / "calls.jsonl"
    evidence_base = evidence_root / "output-evidence.json"
    profile_path = tmp_path / "t9-model-substitute.json"
    profile_path.write_text(json.dumps({
        "schema_version": "hc03-worker-readiness.v1",
        "status": "ready",
        "launch": {
            "model_root": bound_model_root,
        },
        "t9_model_substitute": {
            "approved": True,
            "mode": "deterministic_cpu_model_boundary_v1",
            "purpose": "h3_character_swap_public_runtime_cpu_acceptance_v1",
            "candidate_manifest_path": str(candidate_path.resolve()),
            "candidate_manifest_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            "source_path": str(fixture_path.resolve()),
            "source_sha256": "sha256:" + source_files[fixture_relative],
            "model_asset": model_asset,
            "activation_marker_path": str(marker_base),
            "calls_path": str(calls_path),
            "evidence_path": str(evidence_base),
        },
    }, sort_keys=True), encoding="utf-8")
    monkeypatch.setenv("ASTRID_H3_CANDIDATE_MANIFEST", str(candidate_path))
    monkeypatch.setenv("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE", str(profile_path))

    assert generation.character_swap_asset_sha256() == model_digest.removeprefix("sha256:")

    workflow = generation.build_generation_workflow(
        references=["replacement.png"],
        prompt="Keep the replacement identity stable.",
        frames=24,
        model="minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        steps=8,
        seed=1,
        adapter_strength=0.7,
        turbo_enabled=False,
        character_swap_sha256=model_digest.removeprefix("sha256:"),
    )
    adapter_rows = [
        row for row in workflow.metadata["model_assets"]
        if row.get("name") == generation.CHARACTER_SWAP_FILENAME
    ]
    assert len(adapter_rows) == 1
    assert adapter_rows[0]["sha256"] == model_digest.removeprefix("sha256:")
    assert adapter_rows[0]["hf_revision"] == generation.CHARACTER_SWAP_REVISION


def test_character_swap_digest_override_fails_closed_on_source_pin_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.packs.h3_av.shared.generation import character_swap_asset_sha256

    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps({
        "schema": "astrid-h3-candidate-source-manifest/v1",
        "synthetic_inference": True,
        "synthetic_inference_classification": "synthetic-inference-only",
        "source_files": {
            "tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py": "0" * 64,
        },
    }), encoding="utf-8")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "schema_version": "hc03-worker-readiness.v1",
        "status": "ready",
        "t9_model_substitute": {
            "approved": True,
            "mode": "deterministic_cpu_model_boundary_v1",
            "purpose": "h3_character_swap_public_runtime_cpu_acceptance_v1",
            "candidate_manifest_path": str(candidate_path.resolve()),
            "source_path": str(Path(__file__).resolve().parents[3] / "tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py"),
            "candidate_manifest_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        },
    }), encoding="utf-8")
    monkeypatch.setenv("ASTRID_H3_CANDIDATE_MANIFEST", str(candidate_path))
    monkeypatch.setenv("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE", str(profile_path))

    with pytest.raises(ValueError, match="fixture source does not match"):
        character_swap_asset_sha256()


def test_character_swap_digest_override_rejects_production_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.packs.h3_av.shared.generation import character_swap_asset_sha256

    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps({
        "schema": "astrid-h3-candidate-source-manifest/v1",
        "synthetic_inference": False,
        "synthetic_inference_classification": "production-inference",
        "source_files": {},
    }), encoding="utf-8")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "schema_version": "hc03-worker-readiness.v1",
        "status": "ready",
        "t9_model_substitute": {
            "approved": True,
            "mode": "deterministic_cpu_model_boundary_v1",
            "purpose": "h3_character_swap_public_runtime_cpu_acceptance_v1",
            "candidate_manifest_path": str(candidate_path.resolve()),
            "candidate_manifest_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        },
    }), encoding="utf-8")
    monkeypatch.setenv("ASTRID_H3_CANDIDATE_MANIFEST", str(candidate_path))
    monkeypatch.setenv("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE", str(profile_path))

    with pytest.raises(ValueError, match="synthetic-only"):
        character_swap_asset_sha256()




@pytest.mark.parametrize("profile", ["source", "installed"])
def test_h3_host_startup_policy_matches_real_installed_caller(monkeypatch, profile):
    from tests.helpers import h3_cpu_runtime as harness
    monkeypatch.setattr(harness, "_HOST_EXECUTION_PROFILE", profile)
    assert harness._pack_host_startup_options() == {}




@pytest.mark.parametrize("data", [
    {"object_id": "not-a-digest"}, {},
    {"object_id": "sha256:" + "b" * 64},
    {"object_id": "sha256:" + "a" * 64, "digest": "sha256:" + "b" * 64},
])
def test_managed_transform_file_import_rejects_noncanonical_or_wrong_bytes(tmp_path, data):
    from types import SimpleNamespace
    from tests.helpers.h3_cpu_runtime import _managed_transform_file_input
    path = tmp_path / "request.json"
    path.write_text("{}", encoding="utf-8")
    owner = SimpleNamespace(media=SimpleNamespace(import_file=lambda **kwargs: SimpleNamespace(ok=True, data=data)))
    with pytest.raises((ValueError, RuntimeError)):
        _managed_transform_file_input(owner, "fixture-project", path, key="operation", port="request")


def test_managed_transform_file_import_propagates_unsuccessful_import(tmp_path):
    from types import SimpleNamespace
    from tests.helpers.h3_cpu_runtime import _managed_transform_file_input
    path = tmp_path / "request.json"
    path.write_text("{}", encoding="utf-8")
    owner = SimpleNamespace(media=SimpleNamespace(import_file=lambda **kwargs: SimpleNamespace(ok=False, data=None)))
    with pytest.raises(RuntimeError, match="managed request import failed"):
        _managed_transform_file_input(owner, "fixture-project", path, key="operation", port="request")


@pytest.mark.parametrize("value", ["/tmp/caller-local.json", {"path": "/tmp/caller-local.json"}, {"object_id": "sha256:" + "a" * 64, "digest": "sha256:" + "b" * 64}])
def test_transform_file_policy_retains_strict_caller_path_and_digest_negatives(value):
    from astrid.core.execution.managed_inputs import managed_file_digest
    with pytest.raises(ValueError):
        managed_file_digest(value, "asset_map")


def test_transform_file_policy_accepts_canonical_managed_descriptor():
    from astrid.core.execution.managed_inputs import managed_file_digest
    digest = "sha256:" + "a" * 64
    assert managed_file_digest({"object_id": digest, "digest": digest, "filename": "request.json"}, "request") == digest















def _managed_import_real_sdk_media(transport):
    from astrid_runtime_client import WorkspaceClient as GeneratedWorkspaceClient
    from astrid.sdk.workspace_client import WorkspaceClient
    from astrid.sdk.remote import RemoteMedia
    workspace = WorkspaceClient("http://127.0.0.1:1", "fixture-only-no-network")
    workspace._generated = GeneratedWorkspaceClient(workspace.endpoint, transport=transport)
    return RemoteMedia(workspace)


def _managed_import_wire_response(headers, body):
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    data = {"object_id": digest, "digest": digest, "filename": headers["X-Original-Name"]}
    receipt = {"receipt_id": "fixture-import", "command_kind": "object.ingest",
               "idempotency_key": headers["Idempotency-Key"], "request_hash": "a" * 64,
               "project_id": "fixture-project", "project_seq": [1, 1], "event_ids": [],
               "result": data, "created_at": "2026-10-11T00:00:00Z"}
    return 201, {}, json.dumps({"data": data, "receipt": receipt}).encode()


@pytest.mark.integration
def test_managed_import_legacy_key_is_rejected_by_real_sdk(tmp_path):
    from runtime_protocol.service import validate_idempotency_key
    from runtime_protocol.errors import InvalidRequestError
    from astrid.sdk.contracts import DomainResult
    path = tmp_path / "request.json"
    path.write_text("{}")
    def transport(method, route, headers, body):
        assert method == "POST" and route == "/v1/projects/fixture-project/objects"
        try:
            validate_idempotency_key(headers["Idempotency-Key"])
        except InvalidRequestError as exc:
            return exc.status, {}, json.dumps({"code": exc.code, "message": "invalid fixture key", "details": {}}).encode()
        raise AssertionError("legacy key unexpectedly accepted")
    result = _managed_import_real_sdk_media(transport).import_file(
        project="fixture-project", path=path, realm="managed_local",
        idempotency_key="h3-transform-input:operation:request:sha256:" + "a" * 64)
    assert isinstance(result, DomainResult) and not result.ok
    assert result.error.code == "invalid_request" and result.data is None and result.receipt is None


@pytest.mark.integration
def test_managed_import_both_ports_use_valid_stable_distinct_keys_and_real_results(tmp_path):
    from types import SimpleNamespace
    from runtime_protocol.service import validate_idempotency_key
    from astrid.sdk.contracts import DomainResult
    from tests.helpers.h3_cpu_runtime import _managed_transform_file_input
    seen = []
    def transport(method, route, headers, body):
        assert method == "POST" and route == "/v1/projects/fixture-project/objects"
        assert headers["Content-Type"] == "application/json"
        validate_idempotency_key(headers["Idempotency-Key"])
        seen.append(headers["Idempotency-Key"])
        return _managed_import_wire_response(headers, body)
    media = _managed_import_real_sdk_media(transport)
    owner = SimpleNamespace(media=media)
    for port, name in (("request", "request.json"), ("asset_map", "asset-map.json")):
        path = tmp_path / name
        path.write_text("{}")
        for _ in range(2):
            descriptor = _managed_transform_file_input(owner, "fixture-project", path, key="operation:with/unsafe" + "x" * 300, port=port)
            assert descriptor == {"object_id": "sha256:" + hashlib.sha256(b"{}").hexdigest(),
                                  "digest": "sha256:" + hashlib.sha256(b"{}").hexdigest(), "filename": name}
    assert seen[0] == seen[1] and seen[2] == seen[3] and seen[0] != seen[2]
    assert all(len(key) <= 256 and ":" not in key for key in seen)
    path.write_text('{"changed":true}')
    _managed_transform_file_input(owner, "fixture-project", path, key="operation:with/unsafe" + "x" * 300, port="asset_map")
    assert seen[4] != seen[3]
    result = media.import_file(project="fixture-project", path=path, idempotency_key="valid-direct-result")
    assert isinstance(result, DomainResult) and result.ok and isinstance(result.data, dict)
    assert result.receipt is not None and result.error is None


@pytest.mark.parametrize("port", ["request", "asset_map"])
def test_managed_import_real_sdk_auth_failure_is_not_converted_to_success(tmp_path, port):
    from types import SimpleNamespace
    from astrid.sdk.contracts import DomainResult
    from tests.helpers.h3_cpu_runtime import _managed_transform_file_input
    path = tmp_path / "input.json"
    path.write_text("{}")
    def transport(method, route, headers, body):
        return 401, {}, json.dumps({"code": "unauthorized", "message": "controlled denial", "details": {}}).encode()
    media = _managed_import_real_sdk_media(transport)
    failed = media.import_file(project="fixture-project", path=path, idempotency_key="controlled-denial")
    assert isinstance(failed, DomainResult) and not failed.ok and failed.data is None
    assert failed.error.code == "unauthorized" and failed.receipt is None
    with pytest.raises(RuntimeError, match=f"managed {port} import failed"):
        _managed_transform_file_input(SimpleNamespace(media=media), "fixture-project", path, key="operation", port=port)


@pytest.mark.parametrize("port", ["request", "asset_map"])
def test_managed_import_rejects_genuine_nonmapping_success_result(tmp_path, port):
    from types import SimpleNamespace
    from astrid.sdk.contracts import DomainResult
    from tests.helpers.h3_cpu_runtime import _managed_transform_file_input
    path = tmp_path / "input.json"
    path.write_text("{}")
    class Media:
        def import_file(self, **kwargs):
            return DomainResult.success([])
    with pytest.raises(RuntimeError, match=f"managed {port} import failed"):
        _managed_transform_file_input(SimpleNamespace(media=Media()), "fixture-project", path, key="operation", port=port)


@pytest.mark.parametrize("fault", ["missing", "tampered", "disagreeing"])
def test_managed_import_real_sdk_success_keeps_strict_descriptor_negatives(tmp_path, fault):
    from types import SimpleNamespace
    from tests.helpers.h3_cpu_runtime import _managed_transform_file_input
    path = tmp_path / "request.json"
    path.write_text("{}")
    def transport(method, route, headers, body):
        status, response_headers, response = _managed_import_wire_response(headers, body)
        value = json.loads(response)
        if fault == "missing": value["data"] = {}
        elif fault == "tampered": value["data"]["object_id"] = value["data"]["digest"] = "sha256:" + "b" * 64
        else: value["data"]["digest"] = "sha256:" + "b" * 64
        return status, response_headers, json.dumps(value).encode()
    with pytest.raises((ValueError, RuntimeError)):
        _managed_transform_file_input(SimpleNamespace(media=_managed_import_real_sdk_media(transport)),
                                      "fixture-project", path, key="operation", port="request")




@pytest.mark.parametrize("failure", [None, "missing", "relative", "direct-alias", "ancestor-alias", "fixture-drift", "profile-drift", "candidate-drift", "production", "code-drift"])
def test_synthetic_fixture_uses_pinned_explicit_source_with_installed_code_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure,
) -> None:
    import importlib.util
    from tests.packs.test_vibecomfy_ir_bridge import _t9_child_case

    case = _t9_child_case(tmp_path)
    candidate, candidate_path, profile, profile_path, _, _, _ = case
    checkout = Path(__file__).resolve().parents[3]
    installed = tmp_path / "installed"
    relatives = ("astrid/packs/h3_av/shared/generation.py", "astrid/packs/h3_av/shared/compile.py")
    for relative in relatives:
        target = installed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((checkout / relative).read_bytes())
    assert not (installed / "tests").exists()
    fixture = tmp_path / "fixture-source" / "approved_character_swap_cpu_boundary.py"
    fixture.parent.mkdir()
    fixture.write_bytes((checkout / "tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py").read_bytes())
    boundary = profile["t9_model_substitute"]
    boundary["source_path"] = str(fixture)
    if failure == "missing": boundary["source_path"] = str(tmp_path / "absent.py")
    elif failure == "relative": boundary["source_path"] = "fixture-source/approved_character_swap_cpu_boundary.py"
    elif failure == "direct-alias":
        alias = tmp_path / "alias.py"; alias.symlink_to(fixture); boundary["source_path"] = str(alias)
    elif failure == "ancestor-alias":
        alias = tmp_path / "aliased-directory"; alias.symlink_to(fixture.parent, target_is_directory=True)
        boundary["source_path"] = str(alias / fixture.name)
    elif failure == "fixture-drift": fixture.write_bytes(fixture.read_bytes() + b"\n# drift\n")
    elif failure == "profile-drift": boundary["source_sha256"] = "sha256:" + "0" * 64
    elif failure == "candidate-drift": candidate["source_files"]["tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py"] = "0" * 64
    elif failure == "production": candidate["synthetic_inference"] = False
    elif failure == "code-drift":
        changed = installed / relatives[1]; changed.write_bytes(changed.read_bytes() + b"\n# drift\n")
    candidate_path.write_text(json.dumps(candidate, sort_keys=True))
    boundary["candidate_manifest_sha256"] = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    profile_path.write_text(json.dumps(profile, sort_keys=True))
    monkeypatch.setenv("ASTRID_H3_CANDIDATE_MANIFEST", str(candidate_path))
    monkeypatch.setenv("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE", str(profile_path))
    target = installed / relatives[0]
    spec = importlib.util.spec_from_file_location("astrid.packs.h3_av.shared._installed_fixture_generation", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__) == target
    if failure is None:
        assert module.character_swap_asset_sha256() == boundary["model_asset"]["sha256"].removeprefix("sha256:")
    else:
        with pytest.raises(ValueError):
            module.character_swap_asset_sha256()





@pytest.mark.parametrize("conflict", ["cpu", "profile", "profile_hash", "substitute"])
def test_render_only_rejects_required_h3_before_mutation(tmp_path, monkeypatch, conflict):
    from tests.helpers import h3_cpu_runtime as helper
    for key in helper._H3_ONLY_ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    kwargs = {"render_only": True}
    if conflict == "cpu":
        kwargs["cpu_model_boundary"] = helper.APPROVED_CPU_MODEL_BOUNDARY
    else:
        key = {"profile": "ASTRID_HOST_READINESS_PROFILE_PATH",
               "profile_hash": "ASTRID_HOST_READINESS_PROFILE_HASH",
               "substitute": "ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"}[conflict]
        monkeypatch.setenv(key, "explicit-required-fixture")
    before = dict(os.environ)
    root = tmp_path / "never-created"
    monkeypatch.setattr(helper, "candidate_manifest_path_from_environment", lambda: pytest.fail("conflict passed first guard"))
    with pytest.raises(ValueError, match="conflicts with required H3"):
        helper.H3CpuRuntime(root, **kwargs)
    assert not root.exists()
    assert dict(os.environ) == before




def test_render_only_default_preserves_full_h3_preconditions(tmp_path, monkeypatch):
    import inspect
    from tests.helpers import h3_cpu_runtime as helper
    assert inspect.signature(helper.H3CpuRuntime).parameters['render_only'].default is False
    candidate=tmp_path/'candidate.json';candidate.write_text('{}')
    monkeypatch.setattr(helper,'candidate_manifest_path_from_environment',lambda:candidate)
    monkeypatch.setattr(helper,'RUNTIME_ROOT',tmp_path)
    monkeypatch.setattr(helper,'T9_PROFILE',tmp_path/'required-profile-missing')
    root=tmp_path/'never-created'
    with pytest.raises(RuntimeError,match='approved T9 readiness profile is unavailable'):
        helper.H3CpuRuntime(root)
    assert not root.exists()
