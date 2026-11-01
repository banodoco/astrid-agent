from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from tests.lanes import unavailable

from astrid.packs.h3_av.shared.input_bundle import build_input_bundle
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.vibecomfy.asset_manifest import read_archive
from astrid.sdk.pagination import paged_rows
from tests.helpers.runtime import locate_runtime


def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


@pytest.mark.parametrize("fault", ["success", "missing_revision", "missing_content",
                                  "revision_drift", "content_drift", "checkout_drift",
                                  "profile_hash_drift"])
def test_cpu_host_readiness_identity_reaches_real_installed_guard(tmp_path, fault):
    import sys
    from tests.helpers import h3_cpu_runtime as helper

    checkout = tmp_path / "controlled-vibe"
    package = checkout / "vibecomfy"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Controlled dependency, no backend."""\n')
    (package / "workflow_bundle.py").write_text("FIXTURE_ONLY = True\n")
    revision = helper.PINNED_VIBECOMFY_REVISION
    digest = "sha256:" + hashlib.sha256((package / "workflow_bundle.py").read_bytes()).hexdigest()
    profile = {
        "vibecomfy_candidate": {"kind": "local_snapshot", "checkout": str(checkout),
                               "revision": revision, "source_content_digest": digest},
        "vibecomfy_session": {"source_revision": revision, "source_content_digest": digest},
    }
    readiness = tmp_path / "readiness.json"
    readiness.write_text(json.dumps(profile))
    values = helper._vibecomfy_host_readiness_environment(
        checkout, readiness, "sha256:" + hashlib.sha256(readiness.read_bytes()).hexdigest(),
        revision, digest,
    )
    assert values["ASTRID_VIBECOMFY_ATTESTED_REVISION"] == profile["vibecomfy_session"]["source_revision"]
    assert values["ASTRID_VIBECOMFY_ATTESTED_CONTENT_DIGEST"] == profile["vibecomfy_session"]["source_content_digest"]
    if fault == "missing_revision": values.pop("ASTRID_VIBECOMFY_ATTESTED_REVISION")
    if fault == "missing_content": values.pop("ASTRID_VIBECOMFY_ATTESTED_CONTENT_DIGEST")
    if fault == "revision_drift": values["ASTRID_VIBECOMFY_ATTESTED_REVISION"] = "0" * 40
    if fault == "content_drift": values["ASTRID_VIBECOMFY_ATTESTED_CONTENT_DIGEST"] = "sha256:" + "0" * 64
    if fault == "checkout_drift": values["ASTRID_VIBECOMFY_CHECKOUT"] = str(tmp_path)
    if fault == "profile_hash_drift": values["ASTRID_HOST_READINESS_PROFILE_HASH"] = "sha256:" + "0" * 64
    identity_keys = tuple(helper._vibecomfy_host_readiness_environment(
        checkout, readiness, "unused", revision, digest,
    ))
    prior = {key: os.environ.get(key) for key in identity_keys}
    script = r'''
import json,os,sys
from pathlib import Path
import astrid
from astrid.core.execution.generic_host import _install_host_vibecomfy_import_root, _host_product_import_origins, HostError
from astrid.sdk.host_bootstrap import _host_render_environment
keys=json.loads(sys.argv[1]);checkout=Path(sys.argv[2]);before=_host_product_import_origins();oldpath=sys.path[:]
import astrid_runtime_client
# The installed product tree is what holds astrid and its client: site-packages in a wheel install, the checkout
# when the venv lives inside it, and their common ancestor when astrid is an editable install (CI).
product=Path(os.path.commonpath([Path(astrid.__file__).resolve().parent,Path(astrid_runtime_client.__file__).resolve().parent]))
environment=_host_render_environment(product)
assert {key:environment.get(key) for key in keys} == {key:os.environ.get(key) for key in keys}
os.environ.clear();os.environ.update(environment)
try:
    assert _install_host_vibecomfy_import_root()==checkout
    import vibecomfy.workflow_bundle as bundle
    assert Path(bundle.__file__).is_relative_to(checkout)
    assert _host_product_import_origins()==before
    print(json.dumps({'accepted':True,'product_origins_unchanged':True}))
except HostError:
    assert sys.path==oldpath
    print(json.dumps({'accepted':False,'path_unchanged':True}))
'''
    remove = (*identity_keys, "ASTRID_VIBECOMFY_CANDIDATE_KIND",
              "ASTRID_VIBECOMFY_CANDIDATE_REVISION", "ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST")
    with helper._temporary_environment(values, remove=remove):
        environment = dict(os.environ)
        environment.update(ASTRID_HOST_EXECUTION_PROFILE="installed", ASTRID_PACK_HOST_PYTHON=sys.executable)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-I", "-c", script, json.dumps(identity_keys), str(checkout)],
                                env=environment, text=True, capture_output=True, timeout=20)
    assert {key: os.environ.get(key) for key in identity_keys} == prior
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["accepted"] is (fault == "success")


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
    assert set(origins) == {"host", "child", "action", "client", "sdk"}
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
    from astrid.packs.rendering.rendering.renderers.remotion import run
    from tests.helpers import h3_cpu_runtime as helper

    assert Path(run.__file__).is_relative_to(helper._INSTALLED_EXECUTION_ROOT)
    assert run.gen_effect_registry is None
    project = Path(os.environ["ASTRID_REMOTION_PROJECT_DIR"])
    paths = run._registry_output_paths(project)
    assert len(paths) == 3
    assert {p.name for p in paths} == {"effects.generated.ts", "animations.generated.ts", "transitions.generated.ts"}
    assert all(p.is_file() and p.stat().st_size > 0 for p in paths)
    assert not (project / run._REGISTRY_STATE_PATH).exists()



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


def test_cpu_harness_asset_map_matches_public_transform_input_contract(tmp_path: Path) -> None:
    from tests.helpers.h3_cpu_runtime import _write_runtime_asset_map

    raw = {
        "version": 2,
        "prompt": "Describe the reference image.",
        "duration": 1,
        "media": [{"id": "look", "asset": "look.png", "role": "reference", "modality": "image"}],
        "settings": {},
    }
    image = tmp_path / "look.png"
    image.write_bytes(b"small deterministic image fixture")
    bundle = tmp_path / "input-bundle.zip"
    build_input_bundle(normalize_request(raw), {"look.png": str(image)}, bundle)

    asset_map_path = _write_runtime_asset_map(read_archive(bundle), tmp_path / "staged")
    asset_map = json.loads(asset_map_path.read_text(encoding="utf-8"))

    assert set(asset_map) == {"look.png"}
    materialized = Path(asset_map["look.png"])
    assert materialized.is_file()
    assert materialized.read_bytes() == image.read_bytes()


@pytest.mark.integration
def test_cpu_harness_invokes_transform_unbound_without_request_or_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    import sys

    import tests.helpers.h3_cpu_runtime as runtime_module

    request_path = tmp_path / "request.json"
    request_path.write_text("{}", encoding="utf-8")
    asset_map_path = tmp_path / "asset-map.json"
    asset_map_path.write_text("{}", encoding="utf-8")
    expected_path = tmp_path / "expected.json"
    archive = SimpleNamespace(manifest={"assets": [{
        "binding": "source.mp4", "member": "source.mp4", "sha256": "a" * 64, "size": 17,
    }]})
    monkeypatch.setattr(
        runtime_module,
        "normalize_request",
        lambda _value: SimpleNamespace(value={
            "version": 1, "source": {"asset": "source.mp4", "range": [0, 2]},
            "output": {"duration": 3},
        }),
    )
    monkeypatch.setattr(runtime_module, "read_archive", lambda _path: archive)

    def write_asset_map(_archive, out: Path) -> Path:
        out.mkdir(parents=True, exist_ok=True)
        return asset_map_path

    monkeypatch.setattr(runtime_module, "_write_runtime_asset_map", write_asset_map)

    captured = {}
    imports = []

    def import_file(**kwargs):
        imports.append(kwargs)
        digest = "sha256:" + hashlib.sha256(kwargs["path"].read_bytes()).hexdigest()
        return SimpleNamespace(ok=True, data={"object_id": digest})

    class Owner:
        media = SimpleNamespace(import_file=import_file)
        def invoke_result(self, capability_id, **kwargs):
            captured.update({
                "capability_id": capability_id,
                "support_root_at_invocation": os.environ.get("ASTRID_RUNTIME_DATA_ROOT"),
                "model_substitute_profile_at_invocation": os.environ.get(
                    "ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"
                ),
                **kwargs,
            })
            return SimpleNamespace(ok=True)

    runtime = runtime_module.H3CpuRuntime.__new__(runtime_module.H3CpuRuntime)
    runtime.schema_session = SimpleNamespace(expected_path=expected_path)
    runtime.data_root = tmp_path / "selected-support-root"
    runtime.owner = Owner()
    runtime.project_id = "fixture-project"
    runtime.cpu_model_boundary = "test-only"
    out = tmp_path / "out"
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", "/tmp/stale-runtime-support")
    monkeypatch.setenv("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE", "/tmp/stale-model-profile.json")

    result = runtime.run_public_transform(request_path, tmp_path / "bundle.zip", out=out, key="fixture")

    assert result.ok
    assert captured["capability_id"] == "h3_av.transform"
    assert captured["project"] == runtime.project_id
    assert captured["kind"] == "orchestrator"
    assert captured["python_exec"] == sys.executable
    assert "orchestrator_args" not in captured
    assert captured["inputs"]["project"] == runtime.project_id
    assert [row["path"] for row in imports] == [request_path.resolve(), asset_map_path.resolve()]
    assert all(row["project"] == runtime.project_id and row["realm"] == "managed_local" for row in imports)
    for port, path, imported in zip(("request", "asset_map"), (request_path, asset_map_path), imports):
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        assert captured["inputs"][port] == str(path.resolve())
        from runtime_protocol.service import validate_idempotency_key
        validate_idempotency_key(imported["idempotency_key"])
    assert imports[0]["idempotency_key"] != imports[1]["idempotency_key"]
    assert captured["support_root_at_invocation"] == str(runtime.data_root.resolve())
    assert captured["model_substitute_profile_at_invocation"] is None
    assert "execution_request" not in captured
    assert set(captured["inputs"]) == {"request", "asset_map", "project"}
    assert "execution_target" not in captured
    assert os.environ["ASTRID_RUNTIME_DATA_ROOT"] == "/tmp/stale-runtime-support"
    assert os.environ["ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"] == "/tmp/stale-model-profile.json"


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


def test_candidate_manifest_reaches_declared_orchestrator_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    from astrid.core.subprocess_env import build_child_subprocess_env

    checkout = Path(__file__).resolve().parents[3]
    orchestrator_path = checkout / "astrid/packs/h3_av/pack.yaml"
    compile_path = checkout / "astrid/packs/h3_av/pack.yaml"
    orchestrator = yaml.safe_load(orchestrator_path.read_text(encoding="utf-8"))["actions"]["transform"]
    compile_executor = yaml.safe_load(compile_path.read_text(encoding="utf-8"))["actions"]["compile"]
    allowed = orchestrator["isolation"]["env_passthrough"]
    compile_allowed = compile_executor["isolation"]["env_passthrough"]
    assert "ASTRID_H3_CANDIDATE_MANIFEST" in allowed
    assert "ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE" in allowed
    assert "ASTRID_H3_CANDIDATE_MANIFEST" in compile_allowed
    assert "ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE" in compile_allowed
    assert "ASTRID_RUNTIME_DATA_ROOT" in allowed
    assert "*" not in allowed
    assert "*" not in compile_allowed

    candidate = "/tmp/selected-h3-candidate.json"
    model_profile = "/tmp/selected-t9-model-substitute.json"
    support = "/tmp/selected-support-root"
    monkeypatch.setenv("ASTRID_H3_CANDIDATE_MANIFEST", candidate)
    monkeypatch.setenv("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE", model_profile)
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", support)
    monkeypatch.setenv("ASTRID_H3_UNDECLARED_TEST_VALUE", "must-not-cross")
    inherited_owner_pin = "__CF_USER_TEXT_ENCODING"
    parent = (
        {inherited_owner_pin: os.environ[inherited_owner_pin]}
        if inherited_owner_pin in os.environ
        else {}
    )
    child_env = build_child_subprocess_env(
        base=os.environ,
        parent=parent,
        passthrough=allowed,
        declared_passthrough=allowed,
    )
    compile_child_env = build_child_subprocess_env(
        base=os.environ,
        parent=parent,
        passthrough=compile_allowed,
        declared_passthrough=compile_allowed,
    )

    assert child_env["ASTRID_H3_CANDIDATE_MANIFEST"] == candidate
    assert child_env["ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"] == model_profile
    assert compile_child_env["ASTRID_H3_CANDIDATE_MANIFEST"] == candidate
    assert compile_child_env["ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"] == model_profile
    assert child_env["ASTRID_RUNTIME_DATA_ROOT"] == support
    assert "ASTRID_H3_UNDECLARED_TEST_VALUE" not in child_env


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
        "astrid/packs/h3_av/actions/compile/compile.py",
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


def test_cpu_transform_current_v3_preview_and_old_kind_rejection(tmp_path: Path) -> None:
    from astrid.sdk import invoke_result

    request = tmp_path / "request.json"
    asset_map = tmp_path / "asset-map.json"
    request.write_text("{}", encoding="utf-8")
    asset_map.write_text("{}", encoding="utf-8")
    inputs = {"request": str(request), "asset_map": str(asset_map), "project": "fixture-project"}
    preview = invoke_result(
        "h3_av.transform", kind="orchestrator", inputs=inputs,
        project="fixture-project", out=tmp_path / "out", dry_run=True,
    )
    assert preview.ok, preview.error
    assert preview.capability_type == "orchestrator"
    command = preview.raw_result["command"]
    assert "--project" in command
    assert command[command.index("--project") + 1] == "fixture-project"
    assert "--request" in command and "--asset-map" in command
    rejected = invoke_result(
        "h3_av.transform", kind="executor", inputs=inputs,
        project="fixture-project", out=tmp_path / "out", dry_run=True,
    )
    assert not rejected.ok
    assert rejected.error["type"] == "CapabilityNotFoundError"
    assert not (tmp_path / "out").exists()


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



def test_h3_cleanup_keeps_host_primary_when_original_runtime_also_fails(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests.helpers import h3_cpu_runtime as harness
    from astrid.sdk import host_bootstrap
    events = []
    host_error = RuntimeError("controlled original host graph failure")
    runtime_error = RuntimeError("controlled original Runtime failure")
    original = object()
    state = {"pid": 12345, "process_birth_id": "original-birth"}
    monkeypatch.setitem(host_bootstrap._RETAINED_HOSTS, (12345, "original-birth"), original)
    def failed_host(selected):
        assert selected is state
        events.append("host")
        raise host_error
    def failed_runtime():
        events.append("runtime")
        raise runtime_error
    monkeypatch.setattr(host_bootstrap, "_stop_registered_host", failed_host)
    runtime = harness.H3CpuRuntime.__new__(harness.H3CpuRuntime)
    runtime._closed = False; runtime.host_pid = 12345; runtime.original_host = original
    runtime.original_host_state = state; runtime.schema_session = SimpleNamespace(close=lambda: events.append("schema"))
    boundary = SimpleNamespace(stop=failed_runtime)
    runtime.public_workspace = boundary
    monkeypatch.setattr(runtime, "_restore_parent_state", lambda: events.append("restore"))
    with pytest.raises(RuntimeError, match="cleanup was incomplete") as error:
        runtime.close()
    assert error.value.__cause__ is host_error
    assert events == ["host", "schema", "runtime", "restore"]
    assert runtime.original_host is original and runtime.public_workspace is boundary and not runtime._closed


@pytest.mark.parametrize("failed", [None, "daemon_birth", "daemon_argv", "child_birth", "child_parent", "listener"])
def test_schema_ownership_failure_row_is_fixed_and_secret_free(tmp_path, monkeypatch, failed):
    import sys
    from types import SimpleNamespace
    from tests.helpers import h3_cpu_runtime as harness
    token = "controlled-secret-token-never-emitted"
    marker = {"pid": 123, "url": "http://127.0.0.1:8188", "launch_token": token,
              "process_start_identity": "daemon-birth", "comfy_pid": 124,
              "comfy_process_start_identity": "child-birth"}
    for name, value in {"pid": "123", "url": marker["url"], "comfy_pid": "124", "comfy_process_start_identity": "child-birth"}.items():
        (tmp_path / name).write_text(value)
    owner = SimpleNamespace(
        _read_launch_marker=lambda path: marker,
        _process_start_identity=lambda pid: ("changed" if failed == ("daemon_birth" if pid == 123 else "child_birth") else ("daemon-birth" if pid == 123 else "child-birth")),
        _process_commandline=lambda pid: ("python", "--launch-token", token),
        _launch_token_is_exactly_paired=lambda argv, selected: failed != "daemon_argv" and selected == token,
        _process_parent_pid=lambda pid: 999 if failed == "child_parent" else 123,
        _listener_owner_pid=lambda url: None if failed == "listener" else 124,
    )
    monkeypatch.setitem(sys.modules, "vibecomfy", SimpleNamespace(runtime=SimpleNamespace(session=owner)))
    monkeypatch.setitem(sys.modules, "vibecomfy.runtime", SimpleNamespace(session=owner))
    row = harness._schema_session_ownership_failure_row(tmp_path, 123)
    assert row["phase"] == "schema_ownership_rejected" and row["daemon_pid"] == 123 and row["child_pid"] == 124
    checks = {k: v for k, v in row.items() if k not in {"phase", "daemon_pid", "child_pid", "elapsed_ms"}}
    assert all(type(v) is bool for v in checks.values())
    assert {k for k, v in checks.items() if v is False} == ({failed} if failed else set())
    encoded = json.dumps(row)
    assert len(encoded.encode()) <= 512
    assert token not in encoded and "daemon-birth" not in encoded and "http://" not in encoded


def test_schema_ownership_failure_row_unknown_predicate_and_missing_marker(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from tests.helpers import h3_cpu_runtime as harness
    def unavailable(*args): raise OSError("controlled unavailable observation")
    owner = SimpleNamespace(_read_launch_marker=lambda path: None, _process_commandline=unavailable)
    monkeypatch.setitem(sys.modules, "vibecomfy", SimpleNamespace(runtime=SimpleNamespace(session=owner)))
    monkeypatch.setitem(sys.modules, "vibecomfy.runtime", SimpleNamespace(session=owner))
    with pytest.raises(OSError): harness._schema_session_ownership_failure_row(tmp_path, 123)


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_schema_ownership_row_emission_retains_fd_and_private_sink(tmp_path, monkeypatch, mode):
    import os
    from tests.helpers import h3_cpu_runtime as harness
    path = tmp_path / "private.log"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, mode)
    os.fchmod(fd, mode)
    before = os.fstat(fd)
    real_stat, real_write = os.fstat, os.write
    monkeypatch.setattr(harness.os, "fstat", lambda selected: real_stat(fd) if selected == 2 else real_stat(selected))
    writes = []
    def write(selected, data):
        assert selected == 2
        writes.append(data)
        return real_write(fd, data)
    monkeypatch.setattr(harness.os, "write", write)
    try:
        harness._emit_schema_session_ownership_failure({"phase": "schema_ownership_rejected", "daemon_pid": 123})
        assert len(writes) == (1 if mode == 0o600 else 0)
        assert all(len(data) <= 512 for data in writes)
        assert os.fstat(fd).st_ino == before.st_ino and (os.fstat(fd).st_mode & 0o777) == mode
    finally: os.close(fd)


@pytest.mark.parametrize("failure", ["observation", "output"])
def test_schema_ownership_diagnostic_preserves_primary_and_close(tmp_path, monkeypatch, failure):
    import inspect
    from tests.helpers import h3_cpu_runtime as harness
    # Execute the actual exception path from the constructor, so its ordering
    # and primary exception remain the production helper's own statements.
    source = inspect.getsource(harness.ManagedSchemaSession.__init__)
    start = source.index("            try:\n                _verify_owned_vibe_session")
    end = source.index("            # The CLI can observe", start)
    guarded = source[start:end]
    import textwrap
    primary = ValueError("controlled strict ownership rejection")
    events = []
    obj = type("Fixture", (), {})(); obj.session_dir = tmp_path; obj.pid = 123
    def reject(*args): events.append("verify"); raise primary
    def observe(*args):
        events.append("observe")
        if failure == "observation": raise OSError("controlled observation failure")
        return {"phase": "schema_ownership_rejected", "daemon_pid": 123}
    def emit(row):
        events.append("emit")
        if failure == "output": harness._emit_schema_session_ownership_failure(row)
    monkeypatch.setattr(harness.os, "fstat", lambda fd: (_ for _ in ()).throw(OSError("controlled output failure")))
    code = "try:\n" + textwrap.indent(textwrap.dedent(guarded), "    ") + "except BaseException:\n    try:\n        close()\n    finally:\n        emit(getattr(self, '_ownership_failure_row', None))\n    raise\n"
    with pytest.raises(ValueError) as caught:
        exec(code, {"self": obj, "_verify_owned_vibe_session": reject, "_schema_session_ownership_failure_row": observe,
                    "close": lambda: events.append("close"), "emit": emit})
    assert caught.value is primary and events == ["verify", "observe", "close", "emit"]


def test_cpu_harness_local_parent_retains_child_managed_input_materialization(tmp_path: Path) -> None:
    """The helper's actual descriptors survive SDK admission and localization.

    Only the object store and task transport are synthetic. The command child
    reads the admitted request and asset map after the original caller is gone;
    nested assets remain explicitly fixture-owned, not remotely portable.
    """
    import sys
    from types import SimpleNamespace
    import yaml
    from astrid.core.execution.generic_host import GenericPackHost
    from astrid.packs.h3_av.actions.transform import run as transform
    from astrid.sdk.invocation import _kernel_invoke
    from tests.helpers import h3_cpu_runtime as helper

    caller = tmp_path / "caller"
    caller.mkdir()
    source = caller / "baseline.mp4"
    source.write_bytes(b"CPU fixture source bytes; no generation")
    request = {"version": 1, "operation": "continue",
               "source": {"asset": "baseline.mp4", "range": [0, 1]},
               "output": {"duration": 2}, "content": {"prompt": "continue"},
               "changes": {"video": [{"during": [1, 2], "area": {"full_frame": True}, "action": "generate"}],
                           "audio": [{"during": [1, 2], "action": "generate"}]},
               "references": [], "overrides": {}}
    request_path = caller / "request.json"
    request_path.write_text(json.dumps(request))
    bundle = caller / "bundle.zip"
    build_input_bundle(normalize_request(request), {"baseline.mp4": str(source)}, bundle)
    source_bytes = source.read_bytes()
    objects = {}
    admitted = {}
    manifest = yaml.safe_load((Path(transform.__file__).parents[2] / "pack.yaml").read_text())["actions"]["transform"]
    ports = [SimpleNamespace(**row) for row in manifest["inputs"]]
    capability = SimpleNamespace(id="h3_av.transform", capability_type="executor", inputs=ports)

    class Media:
        def import_file(self, *, project, path, realm, idempotency_key):
            assert project == "cpu-admission" and realm == "managed_local"
            payload = Path(path).read_bytes()
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            objects[digest] = payload
            return SimpleNamespace(ok=True, data={"object_id": digest})

    class Tasks:
        def create(self, **kwargs):
            admitted.update(kwargs)
            return SimpleNamespace(ok=True, data={"task_id": "task-cpu", "run_id": "run-cpu"})

    class Owner:
        media = Media()
        tasks = Tasks()
        def invoke_result(self, capability_id, **kwargs):
            assert capability_id == "h3_av.transform" and kwargs["kind"] == "orchestrator"
            assert kwargs["inputs"]["project"] == kwargs["project"] == "cpu-admission"
            assert all(isinstance(kwargs["inputs"][name], str) for name in ("request", "asset_map"))
            # Managed descriptors are complementary child transport evidence,
            # never submissions of the now-local transform parent.
            child_inputs = {"project": kwargs["project"]}
            for name in ("request", "asset_map"):
                path = Path(kwargs["inputs"][name])
                digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                child_inputs[name] = {"object_id": digest, "digest": digest, "filename": path.name}
            child = SimpleNamespace(id="fixture.managed_child", capability_type="executor", inputs=ports)
            _kernel_invoke(child, kind="executor", project=kwargs["project"],
                           inputs=child_inputs, outputs={}, _client=self)
            assert set(admitted["spec"]["inputs"]) == {"request", "asset_map", "project"}
            assert len(admitted["input_manifest"]) == 2
            shutil.rmtree(caller)
            attempt = tmp_path / "attempt"
            attempt.mkdir()
            host = GenericPackHost(pack_roots=[], client=SimpleNamespace(
                get_object=lambda digest: objects["sha256:" + digest.removeprefix("sha256:")]))
            localized = host._materialize_inputs(
                {"spec": admitted["spec"], "input_object_ids": admitted["input_manifest"]}, attempt,
                file_input_names=frozenset(row.name for row in ports if row.type == "file"))
            assert localized["project"] == "cpu-admission"
            assert all(Path(localized[name]).is_relative_to(attempt) for name in ("request", "asset_map"))
            nested = json.loads(Path(localized["asset_map"]).read_text())
            assert Path(nested["baseline.mp4"]).is_absolute()
            assert Path(nested["baseline.mp4"]).is_relative_to(tmp_path / "out")
            assert Path(nested["baseline.mp4"]).read_bytes() == source_bytes
            bindings = {**localized, "python_exec": sys.executable, "out": str(attempt / "outputs")}
            command = manifest["invocation"]["command"]
            argv = [value.format_map(bindings) for value in command["argv"]]
            for arg in command.get("input_args", []):
                if arg["input"] in localized:
                    argv.extend([arg["flag"], str(localized[arg["input"]])])
            assert argv[:3] == [sys.executable, "-m", "astrid.packs.h3_av.actions.transform.run"]
            parsed = transform.build_parser().parse_args(argv[3:])
            assert parsed.project == "cpu-admission"
            # A real isolated child consumes the exact current action argv's
            # localized file fields; it does not launch the full transform.
            child = "import sys,json,pathlib; a=sys.argv[1:]; r=pathlib.Path(a[a.index('--request')+1]); m=pathlib.Path(a[a.index('--asset-map')+1]); v=json.loads(r.read_text()); n=json.loads(m.read_text()); assert v['source']['asset']=='baseline.mp4'; assert pathlib.Path(n['baseline.mp4']).read_bytes()==" + repr(source_bytes) + "; assert a[a.index('--project')+1]=='cpu-admission'"
            subprocess.run([sys.executable, "-I", "-c", child, *argv[3:]], check=True, timeout=10)
            return SimpleNamespace(ok=True)

    runtime = helper.H3CpuRuntime.__new__(helper.H3CpuRuntime)
    runtime.schema_session = SimpleNamespace(expected_path=tmp_path / "expected.json")
    runtime.data_root = tmp_path / "support"
    runtime.owner = Owner()
    runtime.project_id = "cpu-admission"
    runtime.cpu_model_boundary = "test-only"
    assert runtime.run_public_transform(request_path, bundle, out=tmp_path / "out", key="real-admission").ok
    assert not caller.exists()


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


def test_character_cpu_transport_matches_actual_compiler_raw_frame_timing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from astrid.sdk.contracts import DomainResult
    from astrid.packs.h3_av.shared.generation import generation_timing
    from tests.helpers import h3_cpu_runtime as helper
    raw = {"version": 2, "prompt": "Replace <Video 1> with <Picture 1>.", "duration": 15,
           "media": [
               {"id": "baseline", "asset": "baseline.mp4", "role": "timeline", "modality": "video",
                "at": {"frame": 0}, "range": [0, 15], "edit": [{"stream": "video", "during": [0, 15], "mask": {"full_frame": True}}]},
               {"id": "performance", "asset": "performance.mp4", "role": "reference", "modality": "video", "range": [0, 15], "audio": False},
               {"id": "replacement", "asset": "replacement.png", "role": "reference", "modality": "image"}],
           "settings": {"adapter": {"id": "character_swap_v1"}}}
    request = tmp_path / "request.json"
    request.write_text(json.dumps(raw))
    assets = [{"binding": name, "member": name, "sha256": "a" * 64, "size": 1}
              for name in ("baseline.mp4", "performance.mp4", "replacement.png")]
    monkeypatch.setattr(helper, "read_archive", lambda p: SimpleNamespace(manifest={"assets": assets}, members={name: b"x" for name in ("baseline.mp4", "performance.mp4", "replacement.png")}))
    def transport(method, route, headers, body):
        return _managed_import_wire_response(headers, body)
    owner = SimpleNamespace(media=_managed_import_real_sdk_media(transport), invoke_result=lambda *args, **kwargs: DomainResult.success({}))
    runtime = helper.H3CpuRuntime.__new__(helper.H3CpuRuntime)
    runtime.schema_session = SimpleNamespace(expected_path=tmp_path / "expected.json")
    runtime.owner = owner
    runtime.project_id = "fixture-project"
    runtime.data_root = tmp_path / "support"
    runtime.cpu_model_boundary = "fixture-only"
    assert runtime.run_public_transform(request, tmp_path / "bundle.zip", out=tmp_path / "out", key="timing").ok
    expected = json.loads(runtime.schema_session.expected_path.read_text())
    timing = generation_timing(15)
    assert expected["generated_seconds"] == timing["raw_frames"] / timing["fps"] == 362 / 24
    assert timing["requested_frames"] == 360 and timing["trim_tail_frames"] == 2
    assert normalize_request(raw).value["duration"] == 15


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
    relatives = ("astrid/packs/h3_av/shared/generation.py", "astrid/packs/h3_av/actions/compile/compile.py")
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

@pytest.mark.parametrize("fault", ["success", "return_failure", "raise"])
@pytest.mark.integration
def test_cpu_public_parent_carries_attested_vibe_imports_to_real_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from types import SimpleNamespace
    import sys

    import tests.helpers.h3_cpu_runtime as runtime_module

    request_path = tmp_path / "request.json"
    request_path.write_text("{}", encoding="utf-8")
    asset_map_path = tmp_path / "asset-map.json"
    asset_map_path.write_text("{}", encoding="utf-8")
    expected_path = tmp_path / "expected.json"
    archive = SimpleNamespace(manifest={"assets": [{
        "binding": "source.mp4", "member": "source.mp4", "sha256": "a" * 64, "size": 17,
    }]})
    monkeypatch.setattr(
        runtime_module,
        "normalize_request",
        lambda _value: SimpleNamespace(value={
            "version": 1, "source": {"asset": "source.mp4", "range": [0, 2]},
            "output": {"duration": 3},
        }),
    )
    monkeypatch.setattr(runtime_module, "read_archive", lambda _path: archive)

    def write_asset_map(_archive, out: Path) -> Path:
        out.mkdir(parents=True, exist_ok=True)
        return asset_map_path

    monkeypatch.setattr(runtime_module, "_write_runtime_asset_map", write_asset_map)

    captured = {}
    imports = []

    def import_file(**kwargs):
        imports.append(kwargs)
        digest = "sha256:" + hashlib.sha256(kwargs["path"].read_bytes()).hexdigest()
        return SimpleNamespace(ok=True, data={"object_id": digest})

    class Owner:
        media = SimpleNamespace(import_file=import_file)
        def invoke_result(self, capability_id, **kwargs):
            import subprocess, yaml
            from astrid.core.execution.executor.actions import action_orchestrator_definition
            from astrid.core.execution.orchestrator.runner import _command_subprocess_env, OrchestratorRunRequest
            pack_root = runtime_module.ASTRID_ROOT / "astrid/packs/h3_av"
            action = yaml.safe_load((pack_root / "pack.yaml").read_text())["actions"]["transform"]
            definition = action_orchestrator_definition(SimpleNamespace(id="h3_av", version="1", root=pack_root), "transform", action)
            child_env = _command_subprocess_env(definition, OrchestratorRunRequest("h3_av.transform", out=out), {})
            assert child_env["PYTHONPATH"] == runtime_module._vibecomfy_child_pythonpath(runtime.vibecomfy_root)
            assert child_env["VIBECOMFY_HEADLESS"] == "1"
            assert "/tmp/stale-parent-import" not in child_env["PYTHONPATH"]
            python = os.environ.get("ASTRID_PACK_HOST_PYTHON") or sys.executable
            completed = subprocess.run([python, "-B", "-c", "from vibecomfy import workflow_bundle; print(workflow_bundle.__file__)"], cwd=tmp_path, env=child_env, capture_output=True, text=True, check=True, timeout=10)
            assert Path(completed.stdout.strip()).resolve().is_relative_to(runtime.vibecomfy_root.resolve())
            captured.update({
                "capability_id": capability_id,
                "support_root_at_invocation": os.environ.get("ASTRID_RUNTIME_DATA_ROOT"),
                "model_substitute_profile_at_invocation": os.environ.get(
                    "ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"
                ),
                **kwargs,
            })
            if fault == "raise":
                raise RuntimeError("original public invocation failure")
            return SimpleNamespace(ok=fault == "success")

    runtime = runtime_module.H3CpuRuntime.__new__(runtime_module.H3CpuRuntime)
    runtime.schema_session = SimpleNamespace(expected_path=expected_path)
    runtime.data_root = tmp_path / "selected-support-root"
    runtime.owner = Owner()
    runtime.project_id = "fixture-project"
    runtime.cpu_model_boundary = "test-only"
    runtime.vibecomfy_root = runtime_module.VIBECOMFY_SOURCE_ROOT
    assert (runtime.vibecomfy_root / "vibecomfy/__init__.py").is_file()
    monkeypatch.setattr(runtime_module, "_HOST_EXECUTION_PROFILE", "installed")
    monkeypatch.setenv("PYTHONPATH", "/tmp/stale-parent-import")
    monkeypatch.setenv("VIBECOMFY_HEADLESS", "0")
    out = tmp_path / "out"
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", "/tmp/stale-runtime-support")
    monkeypatch.setenv("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE", "/tmp/stale-model-profile.json")

    if fault == "raise":
        with pytest.raises(RuntimeError, match="original public invocation failure"):
            runtime.run_public_transform(request_path, tmp_path / "bundle.zip", out=out, key="fixture")
    else:
        result = runtime.run_public_transform(request_path, tmp_path / "bundle.zip", out=out, key="fixture")
        assert result.ok is (fault == "success")
    assert os.environ["PYTHONPATH"] == "/tmp/stale-parent-import"
    assert os.environ["VIBECOMFY_HEADLESS"] == "0"
    assert captured["capability_id"] == "h3_av.transform"
    assert captured["project"] == runtime.project_id
    assert captured["kind"] == "orchestrator"
    assert captured["python_exec"] == sys.executable
    assert "orchestrator_args" not in captured
    assert captured["inputs"]["project"] == runtime.project_id
    assert [row["path"] for row in imports] == [request_path.resolve(), asset_map_path.resolve()]
    assert all(row["project"] == runtime.project_id and row["realm"] == "managed_local" for row in imports)
    for port, path, imported in zip(("request", "asset_map"), (request_path, asset_map_path), imports):
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        assert captured["inputs"][port] == str(path.resolve())
        from runtime_protocol.service import validate_idempotency_key
        validate_idempotency_key(imported["idempotency_key"])
    assert imports[0]["idempotency_key"] != imports[1]["idempotency_key"]
    assert captured["support_root_at_invocation"] == str(runtime.data_root.resolve())
    assert captured["model_substitute_profile_at_invocation"] is None
    assert "execution_request" not in captured
    assert set(captured["inputs"]) == {"request", "asset_map", "project"}
    assert "execution_target" not in captured
    assert os.environ["ASTRID_RUNTIME_DATA_ROOT"] == "/tmp/stale-runtime-support"
    assert os.environ["ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE"] == "/tmp/stale-model-profile.json"




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


@pytest.mark.parametrize("failure", [None, "sdk", "missing_capability", "cleanup"])
def test_render_only_factory_keeps_sdk_contract_and_original_cleanup(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    import sys
    from astrid.sdk import host_bootstrap
    from tests.helpers import h3_cpu_runtime as helper
    for key in helper._H3_ONLY_ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ASTRID_VIBECOMFY_CHECKOUT", "unused-inherited-fixture-selection")
    monkeypatch.setenv("ASTRID_VIBECOMFY_ATTESTED_REVISION", "unused-inherited-revision")
    before = dict(os.environ); before_path = tuple(sys.path)
    root = (tmp_path / "runtime").resolve(); support = root / "runtime-data/runtime"
    candidate = tmp_path / "candidate.json"; candidate.write_text('{}')
    monkeypatch.setattr(helper, "candidate_manifest_path_from_environment", lambda: candidate)
    monkeypatch.setattr(helper, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(helper, "T9_PROFILE", tmp_path / "missing-unused-profile")
    monkeypatch.setattr(helper, "VIBECOMFY_SOURCE_ROOT", tmp_path / "missing-unused-vibe")
    monkeypatch.setattr(helper, "_prepare_clean_vibecomfy_checkout", lambda *_: pytest.fail("render-only copied Vibe"))
    monkeypatch.setattr(helper, "ManagedSchemaSession", lambda *_args, **_kw: pytest.fail("render-only started schema"))
    monkeypatch.setattr(helper, "_load_public_workspace_fixture", lambda: object())
    monkeypatch.setattr(helper, "_public_workspace_environment", lambda: {})
    monkeypatch.setattr(helper, "active_source_inventory", lambda: SimpleNamespace(sources=(), identity=""))
    monkeypatch.setattr(helper, "source_checkout_digest", lambda _p: "sha256:" + "7" * 64)
    stops=[]; waits=[]; calls=[]
    class Original:
        def poll(self): return None
        def wait(self, timeout): waits.append(timeout); return 0
    original=Original()
    class Workspace:
        endpoint="http://127.0.0.1:1"; actor_id="fixture-owner"; realm_id="fixture-realm"; runtime_pid=123
        python=Path(sys.executable)
        discovery={"runtime_instance_id":"fixture-runtime"}
        def __init__(self):
            support.mkdir(parents=True, exist_ok=True)
            self.credential_file=root/'owner.json';self.credential_file.write_text(json.dumps({"scope":"astrid","actor_id":self.actor_id,"token":"synthetic-owned-token"}))
            creds=support/'credentials';creds.mkdir();worker=creds/'worker';worker.write_text('synthetic-owned-token');worker.chmod(0o600)
            self.up_result={"worker_credential_file":str(worker),"worker_actor":helper.PACK_HOST_ACTOR,"worker_scopes":helper.PACK_HOST_SCOPES}
        def stop(self):
            stops.append('runtime')
            if failure == "cleanup": raise RuntimeError("synthetic original Runtime cleanup failed")
            return {"owner_stopped":True,"original_wait_exit_code":0}
        def provenance(self): return {"original_runtime_handle_retained":True}
    monkeypatch.setattr(helper, "_start_retained_public_workspace", lambda **_kw: Workspace())
    owner=SimpleNamespace(projects=SimpleNamespace(create=lambda **_kw: SimpleNamespace(ok=True,data={"project_id":"fixture-project"})))
    monkeypatch.setattr(helper, "AstridClient", SimpleNamespace(open=lambda **_kw: owner))
    health={"status":"ok","runtime_instance_id":"fixture-runtime","runtime_epoch":1,"runtime_session_id":"fixture-session","schema_digest":helper.SCHEMA_DIGEST}
    monkeypatch.setattr(helper, "RuntimeProtocolClient", lambda *_: SimpleNamespace(health=lambda:health))
    def bootstrap(value, **kwargs):
        calls.append((dict(value), dict(kwargs)))
        assert not set(helper._H3_ONLY_ENVIRONMENT).intersection(os.environ)
        assert "readiness_profile_path" not in value and "readiness_profile_hash" not in value
        assert value['worker_actor']==helper.PACK_HOST_ACTOR and value['worker_scopes']==helper.PACK_HOST_SCOPES
        assert value['source_checkout']==str(helper._INSTALLED_EXECUTION_ROOT if helper._HOST_EXECUTION_PROFILE=='installed' else helper.ASTRID_ROOT)
        if failure in ("sdk", "cleanup"): raise RuntimeError("synthetic SDK qualification failed")
        ready=support/'generic-host.ready.json';ready.write_text(json.dumps({"identity_attestation":{"target":None,"target_digest":None}}))
        (support/'generic-host.json').write_text(json.dumps({"pid":321,"process_birth_id":"synthetic-birth"}))
        monkeypatch.setitem(host_bootstrap._RETAINED_HOSTS,(321,"synthetic-birth"),original)
        return {"host_status":"ready","host_pid":321,"host_ready_file":str(ready),
                "host_ready_capabilities":["rendering.render"] if failure=='missing_capability' else ["rendering.render","rendering.timeline_visualize"]}
    monkeypatch.setattr(helper,"ensure_pack_host",bootstrap)
    monkeypatch.setattr(host_bootstrap,"_stop_registered_host",lambda _state:stops.append('host'))
    if failure:
        with pytest.raises(RuntimeError) as raised: helper.H3CpuRuntime(root,render_only=True)
        if failure=='cleanup':
            assert str(raised.value)=='synthetic SDK qualification failed'
            assert 'original Runtime cleanup failed' in str(raised.value.__cause__.__cause__)
        assert stops==(['host','runtime'] if failure=='missing_capability' else ['runtime'])
    else:
        rt=helper.H3CpuRuntime(root,render_only=True)
        assert rt.schema_session is None and not (root/'managed-session').exists() and not (root/'runtime-data/models').exists()
        assert not hasattr(rt,'readiness_profile_path')
        rt.close();rt.close()
        assert stops==['host','runtime']
        provenance=json.loads(rt.public_workspace_provenance_path.read_text())
        assert provenance['render_only'] is True and provenance['synthetic_inference'] is False
    assert len(calls)==1
    assert dict(os.environ)==before and tuple(sys.path)==before_path


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
