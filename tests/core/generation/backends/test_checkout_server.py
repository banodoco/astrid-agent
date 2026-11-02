from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

import astrid.core.generation.backends.vibecomfy as backend_module
from astrid.core.generation.backends.vibecomfy import (
    COMFYUI_VERSION,
    VIBECOMFY_ENGINE_REVISION,
    VIBECOMFY_WARMTH_HINT_ENV,
    CheckoutServerAdapter,
    _validate_checkout_server_url,
    vibecomfy_warmth_hint,
)
from astrid.core.model_catalog.schema import BackendSpec, ModeSpec, ModelEntry
from astrid.core.generation.model_root import canonical_model_inventory_digest

RUNTIME_A = "c3d4e5f6-a7b8-49cd-8e01-23456789abcd"


def _approved_projection():
    from vibecomfy.testing.canonical import canonical_digest
    from vibecomfy.workflow_bundle import ApprovedProjectionRecord

    api = {"1": {"class_type": "SaveImage", "inputs": {}}}
    return ApprovedProjectionRecord("revision-1", None, {}, api, {}, canonical_digest(api))


class _Workflow:
    def __init__(self) -> None:
        self.metadata: dict[str, object] = {"unbound_inputs": {}}
        self.inputs: dict[str, object] = {}

    def set_input(self, name: str, value: object) -> None:
        self.inputs[name] = value


class _Response(io.BytesIO):
    status = 200

    def __init__(self, body: bytes, *, payload: object | None = None) -> None:
        super().__init__(body)
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def json(self) -> object:
        if self._payload is None:
            raise AssertionError("json() was not configured")
        return self._payload


def test_checkout_run_workflow_forwards_runtime_config_to_real_engine_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CheckoutServerAdapter("https://gpu.example.test")
    probe = Mock()
    monkeypatch.setattr(adapter, "_probe_system_stats", probe)
    engine = SimpleNamespace(
        prepared_runtime_instance_id=RUNTIME_A,
        runtime_instance_id=None,
        run=Mock(return_value="result"),
    )
    adapter._engine = engine
    config = object()
    workflow = object()

    assert adapter._run_workflow(workflow, config=config) == "result"

    probe.assert_called_once_with()
    engine.run.assert_called_once_with(
        workflow, runtime_instance_id=RUNTIME_A, config=config
    )


def _entry(template_hash: str, template: str = "image/z_image") -> ModelEntry:
    return ModelEntry(
        id="z-image",
        modality="image",
        modes={
            "t2i": ModeSpec(
                supports=("prompt", "seed", "size"),
                requires=("prompt",),
                backends={
                    "local": BackendSpec(
                        template=template,
                        template_hash=template_hash,
                    )
                },
            )
        },
    )


def _fake_vibe_modules(
    workflow: _Workflow,
    result: object,
    *,
    record: object,
    discovery: object,
) -> dict[str, types.ModuleType]:
    root = types.ModuleType("vibecomfy")
    ready = types.ModuleType("vibecomfy.registry.ready")
    runtime = types.ModuleType("vibecomfy.runtime.run")
    ready.repo_ready_template_discovery = Mock(return_value=discovery)  # type: ignore[attr-defined]
    ready.resolve_ready_template = Mock(return_value=record)  # type: ignore[attr-defined]
    ready.workflow_from_ready = Mock(return_value=workflow)  # type: ignore[attr-defined]
    runtime.run_sync = Mock(return_value=result)  # type: ignore[attr-defined]
    return {
        "vibecomfy": root,
        "vibecomfy.registry": types.ModuleType("vibecomfy.registry"),
        "vibecomfy.registry.ready": ready,
        "vibecomfy.runtime": types.ModuleType("vibecomfy.runtime"),
        "vibecomfy.runtime.run": runtime,
    }


def _template_record(tmp_path: Path, *, template_id: str = "image/z_image", source_scope: str = "repo") -> tuple[object, str]:
    template_path = tmp_path / "ready_templates" / "image" / "z_image.py"
    template_path.parent.mkdir(parents=True)
    template_path.write_bytes(b"def build():\n    return object()\n")
    digest = hashlib.sha256(template_path.read_bytes()).hexdigest()
    return (
        SimpleNamespace(
            template_id=template_id,
            path=template_path,
            root=template_path.parents[2],
            source_scope=source_scope,
        ),
        digest,
    )


def _patch_remote_open(monkeypatch: pytest.MonkeyPatch, *, output: bytes = b"png") -> list[str]:
    calls: list[str] = []

    def open_remote(request: object, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        calls.append(url)
        if url.endswith("/system_stats"):
            return _Response(
                b'{"system":{"comfyui_version":"0.26.0"}}',
                payload={"system": {"comfyui_version": COMFYUI_VERSION}},
            )
        return _Response(output)

    monkeypatch.setattr(backend_module, "_open_checkout_http", open_remote)
    return calls


def test_repo_loader_requires_canonical_repo_record_and_verifies_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, digest = _template_record(tmp_path)
    workflow = _Workflow()
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "comfy_outputs": [
                    {"filename": "x.png", "subfolder": "renders", "type": "output"}
                ]
            }
        ),
        encoding="utf-8",
    )
    result = SimpleNamespace(metadata_path=metadata_path, outputs=["/must/not/read"])
    discovery = object()
    modules = _fake_vibe_modules(
        workflow, result, record=record, discovery=discovery
    )
    calls = _patch_remote_open(monkeypatch)
    with patch.dict(sys.modules, modules):
        adapter = CheckoutServerAdapter("HTTPS://GPU.EXAMPLE.TEST:8888/")
        generated = adapter.generate(
            entry=_entry(f"sha256:{digest}"),
            mode="t2i",
            params={"prompt": "a cat", "seed": 7, "size": "512x384"},
            out_dir=tmp_path / "out",
            model_bytes_digest="sha256:" + "a" * 64,
            runtime_instance_id=RUNTIME_A,
        )
    ready = modules["vibecomfy.registry.ready"]
    assert ready.resolve_ready_template.call_args.args == ("image/z_image", discovery)
    assert ready.workflow_from_ready.call_args.args == ("image/z_image",)
    assert ready.workflow_from_ready.call_args.kwargs["_discovery"] is discovery
    assert calls[0] == "https://gpu.example.test:8888/system_stats"
    assert calls[1] == "https://gpu.example.test:8888/system_stats"
    assert calls[2].endswith("/view?filename=x.png&subfolder=renders&type=output")


def test_repo_loader_rejects_wrong_hash_before_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, _digest = _template_record(tmp_path)
    modules = _fake_vibe_modules(
        _Workflow(),
        SimpleNamespace(metadata_path=tmp_path / "unused.json"),
        record=record,
        discovery=object(),
    )
    _patch_remote_open(monkeypatch)
    with patch.dict(sys.modules, modules):
        with pytest.raises(ValueError, match="sha256 pin"):
            CheckoutServerAdapter("https://gpu.example.test").generate(
                entry=_entry("sha256:" + "0" * 64),
                mode="t2i",
                params={"prompt": "a cat"},
                out_dir=tmp_path / "out",
                model_bytes_digest="sha256:" + "a" * 64,
                runtime_instance_id=RUNTIME_A,
            )
    assert not modules["vibecomfy.registry.ready"].workflow_from_ready.called



@pytest.mark.parametrize("record_id", ["z_image", "../z_image", "image/../z_image"])
def test_repo_loader_rejects_non_category_record(
    tmp_path: Path, record_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, digest = _template_record(tmp_path, template_id=record_id)
    modules = _fake_vibe_modules(
        _Workflow(),
        SimpleNamespace(metadata_path=tmp_path / "unused.json"),
        record=record,
        discovery=object(),
    )
    _patch_remote_open(monkeypatch)
    with patch.dict(sys.modules, modules):
        with pytest.raises(ValueError, match="canonical repo template"):
            CheckoutServerAdapter("https://gpu.example.test").generate(
                entry=_entry(f"sha256:{digest}"),
                mode="t2i",
                params={"prompt": "a cat"},
                out_dir=tmp_path / "out",
                model_bytes_digest="sha256:" + "a" * 64,
                runtime_instance_id=RUNTIME_A,
            )

def test_repo_loader_rejects_dynamic_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, digest = _template_record(tmp_path, source_scope="dynamic")
    modules = _fake_vibe_modules(
        _Workflow(),
        SimpleNamespace(metadata_path=tmp_path / "unused.json"),
        record=record,
        discovery=object(),
    )
    _patch_remote_open(monkeypatch)
    with patch.dict(sys.modules, modules):
        with pytest.raises(ValueError, match="canonical repo template"):
            CheckoutServerAdapter("https://gpu.example.test").generate(
                entry=_entry(f"sha256:{digest}"),
                mode="t2i",
                params={"prompt": "a cat"},
                out_dir=tmp_path / "out",
                model_bytes_digest="sha256:" + "a" * 64,
                runtime_instance_id=RUNTIME_A,
            )


def test_remote_output_custody_downloads_metadata_descriptors_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, digest = _template_record(tmp_path)
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "comfy_outputs": {"9": {"images": [
                    {"filename": "remote.png", "subfolder": "", "type": "output",
                     "metadata": {"kind": "image", "width": 512, "height": 512},
                     "future": "ignored for routing"}
                ]}}}
        ),
        encoding="utf-8",
    )
    result = SimpleNamespace(metadata_path=metadata_path, outputs=[str(tmp_path / "local.png")])
    modules = _fake_vibe_modules(
        _Workflow(), result, record=record, discovery=object()
    )
    calls = _patch_remote_open(monkeypatch)
    from unittest.mock import patch

    with patch.dict(sys.modules, modules):
        generated = CheckoutServerAdapter("https://GPU.EXAMPLE.TEST:8888/").generate(
            entry=_entry(f"sha256:{digest}"),
            mode="t2i",
            params={"prompt": "a cat"},
            out_dir=tmp_path / "out",
            model_bytes_digest="sha256:" + "a" * 64,
            runtime_instance_id=RUNTIME_A,
        )
    assert len(calls) == 3
    assert calls[0] == "https://gpu.example.test:8888/system_stats"
    assert calls[1] == "https://gpu.example.test:8888/system_stats"
    query = parse_qs(urlsplit(calls[2]).query, keep_blank_values=True)
    assert query == {"filename": ["remote.png"], "subfolder": [""], "type": ["output"]}
    assert generated.image_paths[0].read_bytes() == b"png"
    assert not list((tmp_path / "out").glob(".checkout-download-*"))
    assert modules["vibecomfy.runtime.run"].run_sync.call_args.kwargs == {
        "server_url": "https://gpu.example.test:8888"
    }


@pytest.mark.parametrize(
    "descriptors",
    [
        [],
        [{"filename": "../escape.png", "subfolder": "", "type": "output"}],
        [{"filename": "ok.png", "subfolder": "../escape", "type": "output"}],
        [{"filename": "ok.png", "subfolder": "", "type": "input"}],
        [{"filename": "ok.png", "subfolder": "", "type": "output", "extra": "x" * 65536}],
    ],
)
def test_remote_output_custody_rejects_empty_or_malformed_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptors: list[dict[str, str]],
) -> None:
    record, digest = _template_record(tmp_path)
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps({"comfy_outputs": descriptors}), encoding="utf-8")
    result = SimpleNamespace(metadata_path=metadata_path)
    modules = _fake_vibe_modules(_Workflow(), result, record=record, discovery=object())
    _patch_remote_open(monkeypatch)
    from unittest.mock import patch

    with patch.dict(sys.modules, modules):
        with pytest.raises(ValueError):
            CheckoutServerAdapter("https://gpu.example.test").generate(
                entry=_entry(f"sha256:{digest}"),
                mode="t2i",
                params={"prompt": "a cat"},
                out_dir=tmp_path / "out",
                model_bytes_digest="sha256:" + "a" * 64,
                runtime_instance_id=RUNTIME_A,
            )


@pytest.mark.parametrize(
    "value",
    [
        "https://user:pass@example.test",
        "https://example.test?token=secret",
        "https://example.test#fragment",
        "https://example.test/path",
        "https:// example.test",
        "https://example.test\\path",
        "https://example.test:notaport",
        "https://example.test:0",
        "https://example.test:65536",
        "https://example..test",
        "https://-example.test",
    ],
)
def test_checkout_server_url_rejects_unsafe_origins(value: str) -> None:
    with pytest.raises(ValueError):
        _validate_checkout_server_url(value)


def test_checkout_server_url_accepts_canonical_origin() -> None:
    assert _validate_checkout_server_url("HTTPS://GPU.EXAMPLE.TEST:8888/") == (
        "https://gpu.example.test:8888"
    )


def test_system_stats_probe_warns_for_workflow_drift_and_bounds_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = CheckoutServerAdapter("https://gpu.example.test", expected_comfyui_version="==" + COMFYUI_VERSION)
    monkeypatch.setattr(
        backend_module,
        "_open_checkout_http",
        lambda request, *, timeout: _Response(
            b"x" * (64 * 1024 + 1),
            payload={"system": {"comfyui_version": COMFYUI_VERSION}},
        ),
    )
    with pytest.raises(ValueError, match="too large"):
        adapter._probe_system_stats()
    assert not adapter._system_stats_verified
    assert not any(
        path.is_file() and b"x" * 64 in path.read_bytes()
        for path in tmp_path.rglob("*")
    )

    monkeypatch.setattr(
        backend_module,
        "_open_checkout_http",
        lambda request, *, timeout: _Response(
            b'{"system":{"comfyui_version":"0.25.0"}}',
            payload={"system": {"comfyui_version": "0.25.0"}},
        ),
    )
    adapter._probe_system_stats()
    assert adapter._system_stats_verified
    assert adapter._version_readiness["status"] == "ready_with_warnings"
    assert adapter._version_readiness["issues"][0]["code"] == "version_drift"
    adapter._host_profile = {"runtime": {"comfyui_version": "==" + COMFYUI_VERSION}}
    with pytest.raises(ValueError, match="0.26.0"):
        adapter._probe_system_stats()
    assert not adapter._system_stats_verified
    adapter._host_profile = None
    monkeypatch.setattr(
        backend_module,
        "_open_checkout_http",
        lambda request, *, timeout: _Response(
            b'{"system":{"comfyui_version":"0.26.0"}}',
            payload={"system": {"comfyui_version": COMFYUI_VERSION}},
        ),
    )
    adapter._probe_system_stats()
    assert adapter.runtime_instance_id is None


def test_generate_requires_digest_and_canonical_runtime_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "vibecomfy", types.ModuleType("vibecomfy"))
    adapter = CheckoutServerAdapter("https://gpu.example.test")
    entry = _entry("sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="model_bytes_digest"):
        adapter.generate(
            entry=entry,
            mode="t2i",
            params={"prompt": "a cat"},
            out_dir=tmp_path / "out",
            runtime_instance_id=RUNTIME_A,
        )
    with pytest.raises(ValueError, match="runtime_instance_id"):
        adapter.generate(
            entry=entry,
            mode="t2i",
            params={"prompt": "a cat"},
            out_dir=tmp_path / "out",
            model_bytes_digest="sha256:" + "a" * 64,
        )


def test_generate_rejects_unscoped_fingerprint_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "vibecomfy", types.ModuleType("vibecomfy"))
    with pytest.raises(ValueError, match="canonical session fingerprint"):
        CheckoutServerAdapter("https://gpu.example.test").generate(
            entry=_entry("sha256:" + "a" * 64),
            mode="t2i",
            params={"prompt": "a cat"},
            out_dir=tmp_path / "out",
            fingerprint="caller-override",
            model_bytes_digest="sha256:" + "a" * 64,
            runtime_instance_id=RUNTIME_A,
        )


def test_checkout_server_records_pinned_engine_contract() -> None:
    assert COMFYUI_VERSION == "0.26.0"

def test_generate_failure_discards_prepared_warmth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = CheckoutServerAdapter("https://gpu.example.test")
    adapter._probe_system_stats = Mock(return_value=None)  # type: ignore[method-assign]
    with patch.object(
        backend_module.VibeComfyBackend,
        "generate",
        side_effect=RuntimeError("template preflight failed"),
    ):
        with pytest.raises(RuntimeError, match="template preflight failed"):
            adapter.generate(
                entry=_entry("sha256:" + "a" * 64),
                mode="t2i",
                params={"prompt": "a cat"},
                out_dir=tmp_path / "out",
                model_bytes_digest="sha256:" + "a" * 64,
                runtime_instance_id=RUNTIME_A,
            )

    assert adapter._engine.warm is False
    assert adapter._engine.last_warm_reused is False
    second = adapter._engine.prepare_session(
        "same-fingerprint",
        "same-warmth",
        runtime_instance_id=RUNTIME_A,
        model_bytes_digest="sha256:" + "a" * 64,
    )
    assert second["lifecycle"] == "cold"
    assert second["warm_reused"] is False


def _hc03_profile(tmp_path: Path, *, model_digest: str = "sha256:" + "a" * 64) -> dict[str, object]:
    from vibecomfy.runtime.session import (
        current_source_content_digest,
        current_source_revision,
    )

    (tmp_path / "outputs").mkdir()
    models_root = tmp_path / "models"
    models_root.mkdir()
    model_fixture = models_root / "fixture.safetensors"
    model_fixture.write_bytes(b"nonempty-model-fixture")
    model_inventory = [{
        "name": model_fixture.name,
        "sha256": "sha256:" + hashlib.sha256(model_fixture.read_bytes()).hexdigest(),
        "size": model_fixture.stat().st_size,
        "subdir": ".",
    }]
    comfy_output = tmp_path / "comfy-output"
    comfy_output.mkdir()
    session_dir = tmp_path / "sessions" / "default"
    session_dir.mkdir(parents=True)
    pid = os.getpid()
    server_url = "http://gpu.example.test:8188"
    source_revision = current_source_revision() or "vibe-source"
    source_content_digest = current_source_content_digest() or "sha256:" + "b" * 64
    config_bytes = json.dumps(
        {
            "base_directory": "/managed/comfy",
            "disable_known_models": True,
            "output_directory": str(comfy_output),
        },
        sort_keys=True,
    ).encode("utf-8")
    (session_dir / "pid").write_text(str(pid), encoding="utf-8")
    (session_dir / "comfy_pid").write_text(str(pid), encoding="utf-8")
    (session_dir / "comfy_process_start_identity").write_text("process-birth", encoding="utf-8")
    (session_dir / "url").write_text(server_url, encoding="utf-8")
    (session_dir / "config.json").write_bytes(config_bytes)
    (session_dir / "source_revision").write_text(source_revision, encoding="utf-8")
    (session_dir / "source_content_digest").write_text(source_content_digest, encoding="utf-8")
    (session_dir / "daemon.log").write_text("owned\n", encoding="utf-8")
    marker = {
        "launch_token": "launch-token",
        "pid": pid,
        "process_start_identity": "process-birth",
        "comfy_pid": pid,
        "comfy_process_start_identity": "process-birth",
        "url": server_url,
    }
    (session_dir / "launch.json").write_text(json.dumps(marker), encoding="utf-8")
    exact = {
        "interpreter": "python",
        "runtime_lock": "sha256:" + "1" * 64,
        "engine_lock": "sha256:" + "2" * 64,
        "model_digest": model_digest,
        "custom_node_digest": "sha256:" + "3" * 64,
        "driver": "driver",
        "root": "sha256:" + "4" * 64,
        "port": 8180,
    }
    minimum = {"vram_bytes": 8, "scratch_bytes": 8}
    facts = {"exact": dict(sorted(exact.items())), "minimum": dict(sorted(minimum.items()))}
    return {
        "schema_version": "hc03-worker-readiness.v1",
        "status": "ready",
        "verified_facts": facts,
        "verified_facts_digest": backend_module._canonical_sha256(facts),
        "runtime": {"runtime_instance_id": RUNTIME_A},
        "launch": {
            "output_root": str(tmp_path / "outputs"),
            "model_root": {
                "schema_version": 1,
                "path": str(models_root),
                "inventory": model_inventory,
                "inventory_digest": canonical_model_inventory_digest(model_inventory),
            },
        },
        "vibecomfy_session": {
            "session_dir": str(session_dir),
            "server_url": server_url,
            "pid": pid,
            "comfy_pid": pid,
            "launch_token": "launch-token",
            "process_birth_id": "process-birth",
            "comfy_process_birth_id": "process-birth",
            "source_revision": source_revision,
            "source_content_digest": source_content_digest,
            "config_digest": "sha256:" + hashlib.sha256(config_bytes).hexdigest(),
        },
    }


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_from_host_session_requires_owned_registry_and_binds_hc03(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _hc03_profile(tmp_path)
    monkeypatch.setattr(backend_module, "_verify_owned_vibe_session", lambda *_: None)
    _patch_remote_open(monkeypatch)
    adapter = CheckoutServerAdapter.from_host_session(
        hc03_profile=profile,
        model_id="z-image",
        template_id="image/z_image",
        invocation_identity="task-1",
    )
    assert adapter.runtime_instance_id == RUNTIME_A
    assert adapter._bound_model_id == "z-image"
    assert adapter._bound_template_id == "image/z_image"
    assert adapter._invocation_identity == "task-1"
    assert adapter._bound_fingerprint


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_from_host_session_adopts_only_matching_host_warmth_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _hc03_profile(tmp_path)
    monkeypatch.setattr(backend_module, "_verify_owned_vibe_session", lambda *_: None)
    _patch_remote_open(monkeypatch)
    session = profile["vibecomfy_session"]
    exact = profile["verified_facts"]["exact"]
    hint = vibecomfy_warmth_hint(
        session_id=str(Path(session["session_dir"])),
        process_birth_id=session["process_birth_id"],
        comfy_process_birth_id=session["comfy_process_birth_id"],
        runtime_instance_id=RUNTIME_A,
        model_id="z-image",
        model_bytes_digest=exact["model_digest"],
        facts_digest=profile["verified_facts_digest"],
        source_revision=session["source_revision"],
        source_content_digest=session["source_content_digest"],
        config_digest=session["config_digest"],
    )
    monkeypatch.setenv(VIBECOMFY_WARMTH_HINT_ENV, hint)
    adapter = CheckoutServerAdapter.from_host_session(
        hc03_profile=profile,
        model_id="z-image",
        template_id="different-compatible-workflow",
        invocation_identity="task-2",
    )

    assert adapter._engine.warm is False
    assert adapter._engine.last_warm_reused is False
    assert adapter._engine._retention_hint is not None
    adapter._revalidate_host_session = Mock(return_value=None)  # type: ignore[method-assign]
    adapter._probe_system_stats = Mock(return_value=None)  # type: ignore[method-assign]
    prepared = adapter.warm_session(
        adapter._bound_fingerprint,
        adapter._bound_warmth_identity,
        runtime_instance_id=RUNTIME_A,
        model_bytes_digest=exact["model_digest"],
    )
    assert prepared["retention_permitted"] is True
    assert prepared["warm_reused"] is False


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_from_host_session_rejects_reachability_without_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _hc03_profile(tmp_path)
    profile.pop("vibecomfy_session")
    monkeypatch.setattr(backend_module, "_open_checkout_http", lambda *args, **kwargs: pytest.fail("must not probe"))
    with pytest.raises(ValueError, match="vibecomfy_session"):
        CheckoutServerAdapter.from_host_session(
            hc03_profile=profile,
            model_id="z-image",
            template_id="image/z_image",
            invocation_identity="task-1",
        )


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_from_host_session_rejects_model_root_inventory_digest_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _hc03_profile(tmp_path)
    launch = profile["launch"]
    assert isinstance(launch, dict)
    model_root = launch["model_root"]
    assert isinstance(model_root, dict)
    model_root["inventory_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="model-root binding"):
        CheckoutServerAdapter.from_host_session(
            hc03_profile=profile,
            model_id="z-image",
            template_id="image/z_image",
            invocation_identity="task-1",
        )


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_from_host_session_rejects_source_content_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _hc03_profile(tmp_path)
    profile["vibecomfy_session"]["source_content_digest"] = "sha256:" + "c" * 64
    monkeypatch.setattr(backend_module, "_verify_owned_vibe_session", lambda *_: None)
    _patch_remote_open(monkeypatch)
    with pytest.raises(ValueError, match="source content digest"):
        CheckoutServerAdapter.from_host_session(
            hc03_profile=profile,
            model_id="z-image",
            template_id="image/z_image",
            invocation_identity="task-1",
        )


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_run_compiled_workflow_uses_bundle_and_private_output_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vibecomfy
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    import vibecomfy.workflow_bundle as workflow_bundle
    profile = _hc03_profile(tmp_path)
    target_schema = SimpleNamespace(refresh=Mock(return_value={}))
    select_schema = Mock(return_value=target_schema)
    target_schema_module = types.ModuleType("vibecomfy.schema")
    target_schema_module.get_target_schema_provider = select_schema  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vibecomfy.schema", target_schema_module)

    output_root = tmp_path / "outputs"
    monkeypatch.setattr(backend_module, "_verify_owned_vibe_session", lambda *_: None)
    _patch_remote_open(monkeypatch, output=b"artifact")
    (tmp_path / "comfy-output" / "artifact.png").write_bytes(b"artifact")
    adapter = CheckoutServerAdapter.from_host_session(
        hc03_profile=profile,
        model_id="z-image",
        template_id="image/z_image",
        invocation_identity="task-1",
    )
    workflow = VibeWorkflow(
        id="image/z_image",
        source=WorkflowSource(id="image/z_image"),
        metadata={"ready_template": "image/z_image"},
    )
    metadata_path = tmp_path / "run-metadata.json"
    metadata_path.write_text(
        json.dumps({"comfy_outputs": [{"filename": "artifact.png", "subfolder": "", "type": "output"}]}),
        encoding="utf-8",
    )
    bundle = Mock()
    bundle.workflow = workflow
    bundle.workflow_identity = workflow.id
    bundle.require_canonical_authority.return_value = None
    compile_observations: list[tuple[dict[str, object], str | None]] = []

    def compile(**kwargs: object) -> object:
        compile_observations.append((dict(kwargs), os.environ.get("VIBECOMFY_MODELS_ROOT")))
        return _approved_projection()

    bundle.compile.side_effect = compile
    monkeypatch.setenv("VIBECOMFY_MODELS_ROOT", "prior-model-root")
    monkeypatch.setattr(workflow_bundle, "load_bundle", lambda _: bundle)
    adapter._run_workflow = Mock(return_value=SimpleNamespace(metadata_path=metadata_path))  # type: ignore[method-assign]
    generated = adapter.run_compiled_workflow(workflow, output_root / "task-1")
    bundle.require_canonical_authority.assert_called_once_with("checkout_server execution")
    select_schema.assert_called_once_with(server_url=adapter._origin)
    target_schema.refresh.assert_called_once_with()
    bundle.compile.assert_called_once_with(
        schema_provider=target_schema, models_root=str(tmp_path / "models")
    )
    assert compile_observations == [
        (
            {"schema_provider": target_schema, "models_root": str(tmp_path / "models")},
            str(tmp_path / "models"),
        )
    ]
    assert os.environ["VIBECOMFY_MODELS_ROOT"] == "prior-model-root"
    assert generated[0].read_bytes() == b"artifact"


@pytest.mark.integration  # needs vibecomfy from a second checkout
@pytest.mark.parametrize("failure_at", ["refresh", "compile"])
def test_run_rejects_target_schema_failure_before_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_at: str,
) -> None:
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    import vibecomfy.workflow_bundle as workflow_bundle

    profile = _hc03_profile(tmp_path)
    monkeypatch.setattr(backend_module, "_verify_owned_vibe_session", lambda *_: None)
    _patch_remote_open(monkeypatch)
    adapter = CheckoutServerAdapter.from_host_session(
        hc03_profile=profile, model_id="z-image", template_id="image/z_image",
        invocation_identity="task-1",
    )
    target_schema = SimpleNamespace(refresh=Mock(return_value={}))
    select_schema = Mock(return_value=target_schema)
    target_schema_module = types.ModuleType("vibecomfy.schema")
    target_schema_module.get_target_schema_provider = select_schema  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vibecomfy.schema", target_schema_module)
    workflow = VibeWorkflow("image/z_image", WorkflowSource("image/z_image"))
    bundle = Mock(workflow=workflow)
    monkeypatch.setattr(workflow_bundle, "load_bundle", lambda _: bundle)
    if failure_at == "refresh":
        target_schema.refresh.side_effect = ValueError("target schema unavailable")
    else:
        bundle.compile.side_effect = ValueError("invalid target schema for workflow")
    adapter._run_workflow = Mock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="target schema"):
        adapter.run_compiled_workflow(workflow, tmp_path / "outputs" / "task-1")
    select_schema.assert_called_once_with(server_url=adapter._origin)
    target_schema.refresh.assert_called_once_with()
    if failure_at == "refresh":
        bundle.compile.assert_not_called()
    else:
        bundle.compile.assert_called_once_with(
            schema_provider=target_schema, models_root=str(tmp_path / "models")
        )
    adapter._run_workflow.assert_not_called()


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_run_compiled_workflow_augments_workflow_config_with_host_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    import vibecomfy.workflow_bundle as workflow_bundle

    profile = _hc03_profile(tmp_path)
    class _TargetSchema:
        def refresh(self) -> dict[str, object]:
            return {}

    target_schema_module = types.ModuleType("vibecomfy.schema")
    target_schema_module.get_target_schema_provider = lambda **_: _TargetSchema()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vibecomfy.schema", target_schema_module)
    monkeypatch.setattr(backend_module, "_verify_owned_vibe_session", lambda *_: None)
    _patch_remote_open(monkeypatch, output=b"artifact")
    (tmp_path / "comfy-output" / "artifact.png").write_bytes(b"artifact")
    adapter = CheckoutServerAdapter.from_host_session(
        hc03_profile=profile,
        model_id="z-image",
        template_id="image/z_image",
        invocation_identity="task-1",
    )
    output_root = tmp_path / "outputs"
    workflow = VibeWorkflow(
        id="image/z_image",
        source=WorkflowSource(id="image/z_image"),
        metadata={
            "ready_template": "image/z_image",
            "comfy_configuration": {"cache_policy": "none"},
        },
    )
    bundle = Mock()
    bundle.workflow = workflow
    bundle.workflow_identity = workflow.id
    bundle.require_canonical_authority.return_value = None
    bundle.compile.return_value = _approved_projection()
    monkeypatch.setattr(workflow_bundle, "load_bundle", lambda _: bundle)
    metadata_path = tmp_path / "run-metadata.json"
    metadata_path.write_text(
        json.dumps({"comfy_outputs": [{"filename": "artifact.png", "subfolder": "", "type": "output"}]}),
        encoding="utf-8",
    )
    adapter._run_workflow = Mock(return_value=SimpleNamespace(metadata_path=metadata_path))  # type: ignore[method-assign]
    adapter.run_compiled_workflow(
        workflow,
        output_root / "task-1",
        task_identity="host-task",
        attempt_identity="host-attempt",
    )

    config = adapter._run_workflow.call_args.kwargs["config"]
    assert config.extra["task_id"] == "host-task"
    assert config.extra["attempt_id"] == "host-attempt"
    assert config.cache_policy == "none"
    assert config.extra["output_directory"] == str(
        Path(profile["vibecomfy_session"]["session_dir"]).parents[1] / "comfy-output"
    )

    adapter._run_workflow.reset_mock()
    adapter.run_compiled_workflow(workflow, output_root / "task-2")
    config_without_ids = adapter._run_workflow.call_args.kwargs["config"]
    assert config_without_ids.extra["output_directory"] == config.extra["output_directory"]


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_run_compiled_workflow_rejects_conflicting_environment_output_before_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    import vibecomfy.workflow_bundle as workflow_bundle

    profile = _hc03_profile(tmp_path)

    class _TargetSchema:
        def refresh(self) -> dict[str, object]:
            return {}

    target_schema_module = types.ModuleType("vibecomfy.schema")
    target_schema_module.get_target_schema_provider = lambda **_: _TargetSchema()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vibecomfy.schema", target_schema_module)
    monkeypatch.setattr(backend_module, "_verify_owned_vibe_session", lambda *_: None)
    _patch_remote_open(monkeypatch, output=b"artifact")
    adapter = CheckoutServerAdapter.from_host_session(
        hc03_profile=profile,
        model_id="z-image",
        template_id="image/z_image",
        invocation_identity="task-1",
    )
    workflow = VibeWorkflow(
        id="image/z_image",
        source=WorkflowSource(id="image/z_image"),
        metadata={"ready_template": "image/z_image"},
    )
    bundle = Mock()
    bundle.workflow = workflow
    bundle.workflow_identity = workflow.id
    bundle.require_canonical_authority.return_value = None
    bundle.compile.return_value = _approved_projection()
    monkeypatch.setattr(workflow_bundle, "load_bundle", lambda _: bundle)
    adapter.warm_session = Mock()  # type: ignore[method-assign]
    monkeypatch.setenv(
        "VIBECOMFY_COMFY_CONFIGURATION",
        json.dumps({"output_directory": str(tmp_path / "wrong-output")}),
    )

    with pytest.raises(ValueError, match="environment output_directory"):
        adapter.run_compiled_workflow(workflow, tmp_path / "outputs" / "task-1")
    adapter.warm_session.assert_not_called()


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_run_compiled_workflow_accepts_canonical_bundle_loader_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import WorkflowBundle
    profile = _hc03_profile(tmp_path)
    class _TargetSchema:
        def refresh(self) -> dict[str, object]:
            return {}

    target_schema_module = types.ModuleType("vibecomfy.schema")
    target_schema_module.get_target_schema_provider = lambda **_: _TargetSchema()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vibecomfy.schema", target_schema_module)

    output_root = tmp_path / "outputs"
    monkeypatch.setattr(backend_module, "_verify_owned_vibe_session", lambda *_: None)
    _patch_remote_open(monkeypatch, output=b"artifact")
    (tmp_path / "comfy-output" / "artifact.png").write_bytes(b"artifact")
    adapter = CheckoutServerAdapter.from_host_session(
        hc03_profile=profile,
        model_id="z-image",
        template_id="canonical-workflow",
        invocation_identity="task-1",
    )
    workflow = VibeWorkflow(
        id="canonical-workflow",
        source=WorkflowSource(id="canonical-workflow"),
        metadata={},
    )
    bundle = Mock(spec=WorkflowBundle)
    bundle.workflow = workflow
    bundle.workflow_identity = workflow.id
    bundle.require_canonical_authority.return_value = None
    bundle.compile.return_value = _approved_projection()
    metadata_path = tmp_path / "run-metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "comfy_outputs": [
                    {"filename": "artifact.png", "subfolder": "", "type": "output"}
                ]
            }
        ),
        encoding="utf-8",
    )
    adapter._run_workflow = Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(metadata_path=metadata_path)
    )

    generated = adapter.run_compiled_workflow(bundle, output_root / "task-1")

    bundle.require_canonical_authority.assert_called_once_with(
        "checkout_server execution"
    )
    bundle.compile.assert_called_once_with(
        schema_provider=ANY, models_root=str(tmp_path / "models")
    )
    assert generated[0].read_bytes() == b"artifact"


@pytest.mark.integration  # needs vibecomfy from a second checkout
def test_run_compiled_workflow_rejects_registry_mutation_after_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vibecomfy
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource

    profile = _hc03_profile(tmp_path)
    monkeypatch.setattr(backend_module, "_verify_owned_vibe_session", lambda *_: None)
    _patch_remote_open(monkeypatch)
    adapter = CheckoutServerAdapter.from_host_session(
        hc03_profile=profile,
        model_id="z-image",
        template_id="image/z_image",
        invocation_identity="task-1",
    )
    session_dir = Path(profile["vibecomfy_session"]["session_dir"])
    (session_dir / "config.json").write_text('{"changed":true}\n', encoding="utf-8")
    workflow = VibeWorkflow(
        id="image/z_image",
        source=WorkflowSource(id="image/z_image"),
        metadata={"ready_template": "image/z_image"},
    )
    adapter._run_workflow = Mock()  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="registry identity changed"):
        adapter.run_compiled_workflow(workflow, tmp_path / "outputs" / "task-1")
    adapter._run_workflow.assert_not_called()
