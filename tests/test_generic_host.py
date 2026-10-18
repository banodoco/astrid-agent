from __future__ import annotations

import hashlib
from dataclasses import replace
from importlib.machinery import ModuleSpec
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host, process_group
from astrid.core.execution.custody_broker import CustodyError
from astrid.core.execution.generic_host import (
    AdapterRegistry,
    GenericPackHost,
    HostCancelled,
    CleanupUncertainError,
    HostError,
    HostRegistrationError,
    RuntimeProtocolClient,
    _assert_live_storage_envelope,
    _attempt_tree_bytes,
    _completed_process_evidence,
    _storage_tree_bytes,
    _task_storage_envelope,
    _terminate_process_group,
)
from astrid.core.execution.guards import ExecutionGuardPolicy


class FakeRuntime:
    schema_digest = "sha256:" + "1" * 64

    def __init__(self):
        self.registrations = []
        self.settlements = []
        self.uploaded_objects = {}
        self.failures = []
        self.heartbeats = []
        self.heartbeat_progress = []
        self.capability_registrations = []
        self.tasks = {}

    def health(self):
        return {
            "status": "ok",
            "protocol": "workspace.v1",
            "schema_digest": self.schema_digest,
            "runtime_epoch": 1,
            "instance_id": "fixture-runtime-instance",
            "runtime_instance_id": "fixture-runtime-instance",
            "coordinator_epoch": "fixture-runtime-instance",
        }

    def register_executor(self, executor_id, **payload):
        self.registrations.append((executor_id, payload))
        return {"id": executor_id, "state": "registered"}

    def register_capability(self, capability_id, **payload):
        self.capability_registrations.append((capability_id, payload))

    def heartbeat(self, task_id, lease_token, *, attempt_id, fence, progress=None):
        self.heartbeats.append((task_id, lease_token, attempt_id, fence))
        if progress is not None:
            self.heartbeat_progress.append((task_id, progress))

    def task(self, task_id):
        return self.tasks[task_id]

    def settle(self, task_id, lease_token, **payload):
        self.settlements.append((task_id, lease_token, payload))
        return {"task": {"id": task_id, "status": "completed"}}

    def fail(self, task_id, lease_token, error, **kwargs):
        self.failures.append((task_id, lease_token, error, kwargs))

    def claim_next(self, **payload):
        self.claim_payload = payload
        return None

    def upload_object(self, path, *, project_id, media_type, filename=None):
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.uploaded_objects[digest] = {
            "data": data,
            "filename": filename,
            "project_id": project_id,
            "media_type": media_type,
        }
        return SimpleNamespace(
            object_id=f"object-{digest[:12]}",
            digest=digest,
            size=len(data),
            media_type=media_type,
            filename=filename,
            project_id=project_id,
        )

    def withdraw_capability(self, capability_id, *, digest, reason):
        self.register_capability(
            capability_id,
            digest=digest,
            status="unavailable",
            unavailable_reason=reason,
        )


def test_runtime_failure_preserves_structured_guard_diagnostic() -> None:
    class Generated:
        def health(self):
            return {"runtime_epoch": 7}

        def fail_attempt(self, attempt_id, **kwargs):
            self.failure = (attempt_id, kwargs)
            return {"status": "failed"}

    generated = Generated()
    client = object.__new__(RuntimeProtocolClient)
    client.generated = generated
    client._runtime_epoch = None
    client._attempt_runtime_epochs = {"attempt-1": 7}

    client.fail(
        "task-1",
        "lease-1",
        "generated evidence cap exceeded",
        attempt_id="attempt-1",
        fence=3,
        failure_diagnostic={
            "guard": "generated_evidence",
            "category": "run_budget_exceeded",
            "capability_id": "rendering.render",
            "source_digest": "source-digest",
            "observed_bytes": 12,
            "configured_cap_bytes": 10,
        },
    )

    attempt_id, kwargs = generated.failure
    assert attempt_id == "attempt-1"
    assert kwargs["error"]["diagnostic"]["category"] == "run_budget_exceeded"
    assert kwargs["error"]["diagnostic"]["source_digest"] == "source-digest"


def _write_manifest(
    root: Path,
    *,
    version: str = "1.0",
    capability_id: str = "test.echo",
    cwd: str | None = None,
) -> Path:
    root.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "id": capability_id,
        "name": "Echo",
        "kind": "external",
        "version": version,
        "command": {
            "argv": [
                "{python_exec}",
                "-c",
                "from pathlib import Path; Path('{out}/answer.txt').write_text('ok')",
            ]
        },
        "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt", "artifact_type": "text/plain"}],
        "metadata": {"resource_keys": ["cpu"], "estimated_scratch_bytes": 1},
    }
    if cwd is not None:
        manifest["command"]["cwd"] = cwd
    path = root / "executor.yaml"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_admitted_definition_and_source_are_fenced_before_child_dispatch(tmp_path):
    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path])
    definition, admission = host.admit("executor", "test.echo")
    # Mutating the source after admission must fail closed; the child may not
    # reopen the mutable default registry and silently run a different command.
    (tmp_path / "echo" / "runtime.py").write_text("changed", encoding="utf-8")
    with pytest.raises(HostError, match="source digest changed"):
        host.invoke_capability(
            capability_kind="executor",
            capability_id="test.echo",
            request={"out": str(tmp_path / "attempt"), "inputs": {}},
            attempt=tmp_path / "attempt",
            definition=definition,
            admission=admission,
        )


def test_external_pack_import_root_is_fenced_before_direct_command(tmp_path):
    """A mutation in an imported external-pack module blocks command launch."""
    pack_root = tmp_path / "fixture_provider"
    executor_root = pack_root / "executors" / "echo"
    executor_root.mkdir(parents=True)
    (pack_root / "pack.yaml").write_text(
        "schema_version: 1\nid: fixture_provider\nname: Fixture\nversion: 1.0\n"
        "capabilities: [echo]\ncontent:\n  executors: executors\n",
        encoding="utf-8",
    )
    (pack_root / "helper.py").write_text("VALUE = 'before'\n", encoding="utf-8")
    (executor_root / "executor.yaml").write_text(
        json.dumps({
            "schema_version": 1,
            "id": "fixture_provider.echo",
            "name": "Echo",
            "kind": "external",
            "version": "1.0",
            "command": {"argv": [sys.executable, "-c", "from fixture_provider import helper; from pathlib import Path; Path('{out}/answer').write_text(helper.VALUE)"]},
            "outputs": [],
        }),
        encoding="utf-8",
    )
    host = GenericPackHost(pack_roots=[pack_root])
    host.discover()
    record = host.capabilities["fixture_provider.echo"]
    (pack_root / "helper.py").write_text("VALUE = 'after'\n", encoding="utf-8")
    with pytest.raises(HostError, match="source digest changed"):
        host._run_command_definition(
            record,
            {},
            tmp_path / "attempt" / "outputs",
            tmp_path / "attempt",
            admission={
                "source_digest": record.source_digest,
                "source_root": str(record.source_root),
                "source_roots": [str(root) for root in (record.source_root, pack_root)],
            },
        )


def test_child_environment_carries_explicit_runtime_connection(tmp_path):
    """Runtime-backed children use the host connection, never discovery."""
    _write_manifest(tmp_path / "echo")
    runtime = RuntimeProtocolClient("http://127.0.0.1:8765", "worker-token")
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    record = host.capabilities["test.echo"]

    child_env, secrets = host._child_environment(record, tmp_path / "attempt")
    try:
        assert child_env["BANODOCO_RUNTIME_ENDPOINT"] == "http://127.0.0.1:8765"
        assert child_env["BANODOCO_RUNTIME_CREDENTIAL"] == "worker-token"
    finally:
        child_env.clear()
        secrets.clear()


def _write_credential_manifest(root: Path) -> None:
    (root / "pack.yaml").write_text(
        "schema_version: 1\nid: credential_test\nname: Credential Test\n"
        "version: 1.0\ncontent:\n  executors: executors\n",
        encoding="utf-8",
    )
    executor_root = root / "executors" / "credential_echo"
    executor_root.mkdir(parents=True)
    (executor_root / "executor.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "credential_test.echo",
                "name": "Credential Echo",
                "kind": "external",
                "version": "1.0",
                "command": {"argv": ["{python_exec}", "-c", "pass"]},
                "outputs": [],
                "isolation": {
                    "mode": "subprocess",
                    "network": False,
                    "secrets_required": ["FAL_KEY"],
                },
            }
        ),
        encoding="utf-8",
    )


def test_manifest_credentials_use_shared_precedence_at_host_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness and injection agree on shared-file precedence and scope."""
    _write_credential_manifest(tmp_path)
    shared = tmp_path / "astrid.env"
    shared.write_text("FAL_KEY=from-shared\nUNRELATED_SECRET=must-not-cross\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(shared))
    monkeypatch.setenv("FAL_KEY", "stale-process")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-process")

    host = GenericPackHost(pack_roots=[tmp_path], credential_source={})
    record = host.discover()[0]
    host.preflight(record.id)
    record = host.capabilities[record.id]
    assert record.preflight["credentials"] == {"ok": True, "missing": []}

    child_env, secrets = host._child_environment(record, tmp_path / "attempt")
    try:
        assert child_env["FAL_KEY"] == "from-shared"
        assert child_env.get("OPENAI_API_KEY") is None
        assert child_env.get("UNRELATED_SECRET") is None
    finally:
        child_env.clear()
        secrets.clear()

    explicit_host = GenericPackHost(
        pack_roots=[tmp_path], credential_source={"FAL_KEY": "from-explicit"}
    )
    explicit_record = explicit_host.discover()[0]
    explicit_host.preflight(explicit_record.id)
    explicit_record = explicit_host.capabilities[explicit_record.id]
    child_env, secrets = explicit_host._child_environment(
        explicit_record, tmp_path / "explicit-attempt"
    )
    try:
        assert child_env["FAL_KEY"] == "from-explicit"
    finally:
        child_env.clear()
        secrets.clear()


def test_manifest_credential_missing_from_all_sources_blocks_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_credential_manifest(tmp_path)
    monkeypatch.setenv("ASTRID_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("FAL_KEY", raising=False)
    host = GenericPackHost(pack_roots=[tmp_path], credential_source={})
    record = host.discover()[0]
    host.preflight(record.id)
    record = host.capabilities[record.id]
    assert record.preflight["credentials"] == {"ok": False, "missing": ["FAL_KEY"]}


def test_input_materialization_rejects_traversal_names(tmp_path):
    _write_manifest(tmp_path / "echo")

    class Objects(FakeRuntime):
        def get_object(self, digest):
            return b"input"

    host = GenericPackHost(pack_roots=[tmp_path], client=Objects())
    with pytest.raises(HostError, match="input name escapes"):
        host._materialize_inputs(
            {
                "input_object_ids": [],
                "spec": {
                    "inputs": {},
                    "input_digests": [
                        {"name": "../escape", "digest": "a" * 64}
                    ],
                },
            },
            tmp_path / "attempt",
        )


def test_hc04_params_bind_only_definition_declared_ports(tmp_path):
    host = GenericPackHost(pack_roots=[tmp_path])
    bound = host._materialize_inputs(
        {
            "input_object_ids": [],
            "spec": {
                "family": "generation.generate_image",
                "params": {"prompt": "a lighthouse", "model": "z-image"},
                "output_policy": {},
            },
        },
        tmp_path / "attempt",
        task_param_ports=("prompt", "model"),
    )
    assert bound["prompt"] == "a lighthouse"
    assert bound["model"] == "z-image"

    with pytest.raises(HostError, match="undeclared parameter"):
        host._materialize_inputs(
            {
                "input_object_ids": [],
                "spec": {
                    "family": "generation.generate_image",
                    "params": {"prompt": "a lighthouse", "unknown": True},
                    "output_policy": {},
                },
            },
            tmp_path / "attempt-unknown",
            task_param_ports=("prompt",),
        )


def test_input_materialization_preserves_exact_length_ordinary_path(tmp_path):
    """A 64-character non-hex path is an executor value, not a CAS digest."""
    marker = Path("/tmp") / ("control-marker-" + "x" * 44)
    assert len(str(marker)) == 64

    class Objects(FakeRuntime):
        def get_object(self, object_digest):
            raise AssertionError(f"ordinary path was fetched as {object_digest!r}")

    host = GenericPackHost(pack_roots=[tmp_path], client=Objects())
    values = host._materialize_inputs(
        {
            "input_object_ids": [],
            "spec": {"inputs": {"control_marker": str(marker)}},
        },
        tmp_path / "attempt-exact-marker",
    )

    assert values["control_marker"] == str(marker)


def test_hc04_cas_param_materializes_authorized_image_reference(tmp_path):
    payload = b"source-image"
    digest = hashlib.sha256(payload).hexdigest()

    class Objects(FakeRuntime):
        def get_object(self, object_digest):
            assert object_digest == digest
            return payload

    host = GenericPackHost(pack_roots=[tmp_path], client=Objects())
    values = host._materialize_inputs(
        {
            "input_object_ids": [f"sha256:{digest}"],
            "spec": {
                "family": "generation.generate_image",
                "params": {
                    "mode": "i2i",
                    "image_ref": {
                        "digest": f"sha256:{digest}",
                        "filename": "source.jpg",
                    },
                },
                "output_policy": {},
            },
        },
        tmp_path / "attempt",
        task_param_ports=("mode", "image_ref"),
        cas_param_ports=("image_ref",),
    )
    staged = Path(values["image_ref"])
    assert staged == tmp_path / "attempt" / "inputs" / "source.jpg"
    assert staged.read_bytes() == payload


def test_hc04_multi_cas_materialization_preserves_role_order_and_names(tmp_path):
    image = b"character-image"
    video = b"driving-video"
    image_digest = hashlib.sha256(image).hexdigest()
    video_digest = hashlib.sha256(video).hexdigest()

    class Objects(FakeRuntime):
        def get_object(self, object_digest):
            return {image_digest: image, video_digest: video}[object_digest]

    host = GenericPackHost(pack_roots=[tmp_path], client=Objects())
    values = host._materialize_inputs(
        {
            "input_object_ids": [image_digest, video_digest],
            "spec": {
                "family": "vibecomfy.character_animation",
                "params": {
                    "reference_image_ref": {"digest": image_digest, "filename": "source.bin"},
                    "driving_video_ref": {"digest": video_digest, "filename": "source.bin"},
                },
                "output_policy": {},
            },
        },
        tmp_path / "attempt-ordered",
        task_param_ports=("reference_image_ref", "driving_video_ref"),
        cas_param_ports=("reference_image_ref", "driving_video_ref"),
    )
    image_path = Path(values["reference_image_ref"])
    video_path = Path(values["driving_video_ref"])
    assert image_path.name == "reference_image_ref--source.bin"
    assert video_path.name == "driving_video_ref--source.bin"
    assert image_path.read_bytes() == image
    assert video_path.read_bytes() == video

    with pytest.raises(HostError, match="ordered input_object_ids\\[0\\]"):
        host._materialize_inputs(
            {
                "input_object_ids": [video_digest, image_digest],
                "spec": {
                    "family": "vibecomfy.character_animation",
                    "params": {
                        "reference_image_ref": {"digest": image_digest, "filename": "source.png"},
                        "driving_video_ref": {"digest": video_digest, "filename": "source.mp4"},
                    },
                    "output_policy": {},
                },
            },
            tmp_path / "attempt-swapped",
            task_param_ports=("reference_image_ref", "driving_video_ref"),
            cas_param_ports=("reference_image_ref", "driving_video_ref"),
        )


def test_hc04_optional_video_end_frame_preserves_ordered_flf_roles(tmp_path):
    start = b"start-image"
    end = b"end-image"
    start_digest = hashlib.sha256(start).hexdigest()
    end_digest = hashlib.sha256(end).hexdigest()

    class Objects(FakeRuntime):
        def get_object(self, object_digest):
            return {start_digest: start, end_digest: end}[object_digest]

    host = GenericPackHost(pack_roots=[tmp_path], client=Objects())
    values = host._materialize_inputs(
        {
            "input_object_ids": [start_digest, end_digest],
            "spec": {
                "family": "generation.generate_video",
                "params": {
                    "mode": "flf",
                    "image_ref": {"digest": start_digest, "filename": "start.png"},
                    "image_end_ref": {"digest": end_digest, "filename": "end.png"},
                },
                "output_policy": {},
            },
        },
        tmp_path / "attempt-flf",
        task_param_ports=("mode", "image_ref", "image_end_ref"),
        cas_param_ports=("image_ref", "image_end_ref"),
        optional_cas_param_ports=("image_end_ref",),
    )
    assert Path(values["image_ref"]).read_bytes() == start
    assert Path(values["image_end_ref"]).read_bytes() == end

    i2v_values = host._materialize_inputs(
        {
            "input_object_ids": [start_digest],
            "spec": {
                "family": "generation.generate_video",
                "params": {
                    "mode": "i2v",
                    "image_ref": {"digest": start_digest, "filename": "start.png"},
                },
                "output_policy": {},
            },
        },
        tmp_path / "attempt-i2v",
        task_param_ports=("mode", "image_ref", "image_end_ref"),
        cas_param_ports=("image_ref", "image_end_ref"),
        optional_cas_param_ports=("image_end_ref",),
    )
    assert Path(i2v_values["image_ref"]).read_bytes() == start
    assert "image_end_ref" not in i2v_values


def test_hc04_cas_materialization_enforces_declared_size_before_write(tmp_path):
    payload = b"source-image"
    digest = hashlib.sha256(payload).hexdigest()

    class Objects(FakeRuntime):
        def get_object(self, object_digest):
            assert object_digest == digest
            return payload

    host = GenericPackHost(pack_roots=[tmp_path], client=Objects())
    with pytest.raises(HostError, match="exceeding bounded materialization limit"):
        host._materialize_inputs(
            {
                "input_object_ids": [digest],
                "spec": {
                    "inputs": {},
                    "params": {
                        "image_ref": {
                            "digest": digest,
                            "filename": "source.png",
                            "media_type": "image/png",
                        }
                    },
                },
            },
            tmp_path / "attempt-limit",
            authorized_input_object_ids=[digest],
            task_param_ports=("image_ref",),
            cas_param_ports=("image_ref",),
            storage_estimate={"scratch_bytes": 1024, "output_bytes": 0},
            input_size_limits={"image_ref": 3},
        )
    assert not (tmp_path / "attempt-limit" / "inputs" / "source.png").exists()

    with pytest.raises(HostError, match="CAS parameter 'image_ref'"):
        host._materialize_inputs(
            {
                "input_object_ids": [f"sha256:{digest}"],
                "spec": {
                    "family": "generation.generate_image",
                    "params": {"image_ref": "https://example.test/source.png"},
                    "output_policy": {},
                },
            },
            tmp_path / "attempt-url",
            task_param_ports=("image_ref",),
            cas_param_ports=("image_ref",),
        )


def test_bounded_qwen_admission_rejects_legacy_media_authority(tmp_path):
    digest = "a" * 64
    host = GenericPackHost(pack_roots=[tmp_path])
    params = {
        "model": "qwen-image-edit-2511",
        "mode": "edit",
        "execution": "cloud",
        "prompt": "a" * 64,
        "count": 1,
        "size": "1024x1024",
        "seed": 19,
        "image_ref": {
            "digest": digest,
            "filename": "source.png",
            "media_type": "image/png",
        },
    }
    base = {
        "input_object_ids": [digest],
        "spec": {
            "family": "generation.generate_image_edit",
            "params": params,
            "output_policy": {},
        },
    }
    with pytest.raises(HostError, match="legacy spec.inputs authority"):
        host._materialize_inputs(
            {
                **base,
                "spec": {
                    **base["spec"],
                    "inputs": {"image_ref": {"digest": digest}},
                },
            },
            tmp_path / "attempt-qwen-legacy-input",
            authorized_input_object_ids=[digest],
            task_param_ports=("model", "mode", "execution", "prompt", "image_ref", "count", "size", "seed"),
            cas_param_ports=("image_ref",),
            storage_policy_version="astrid.cloud-edit.qwen-source.v1",
        )
    with pytest.raises(HostError, match="legacy input_digests authority"):
        host._materialize_inputs(
            {
                **base,
                "spec": {
                    **base["spec"],
                    "input_digests": [{"name": "image_ref", "digest": digest}],
                },
            },
            tmp_path / "attempt-qwen-legacy-digests",
            authorized_input_object_ids=[digest],
            task_param_ports=("model", "mode", "execution", "prompt", "image_ref", "count", "size", "seed"),
            cas_param_ports=("image_ref",),
            storage_policy_version="astrid.cloud-edit.qwen-source.v1",
        )


def test_unified_edit_materialization_allows_optional_mask_for_source_profile(tmp_path):
    payload = b"source-image"
    digest = hashlib.sha256(payload).hexdigest()

    class Objects(FakeRuntime):
        def get_object(self, object_digest):
            assert object_digest == digest
            return payload

    host = GenericPackHost(pack_roots=[tmp_path], client=Objects())
    values = host._materialize_inputs(
        {
            "input_object_ids": [digest],
            "spec": {
                "family": "generation.generate_image_edit",
                "params": {
                    "model": "qwen-image-edit-2511",
                    "mode": "edit",
                    "execution": "cloud",
                    "prompt": "source edit",
                    "count": 1,
                    "size": "1024x1024",
                    "image_ref": {
                        "digest": digest,
                        "filename": "source.png",
                        "media_type": "image/png",
                    },
                },
                "output_policy": {},
            },
        },
        tmp_path / "attempt-unified-source",
        authorized_input_object_ids=[digest],
        task_param_ports=(
            "model",
            "mode",
            "execution",
            "prompt",
            "image_ref",
            "mask_ref",
            "count",
            "size",
        ),
        cas_param_ports=("image_ref", "mask_ref"),
        storage_policy_version="astrid.cloud-edit.unified.v1",
    )
    assert Path(values["image_ref"]).read_bytes() == payload
    assert "mask_ref" not in values


def test_input_materialization_rejects_foreign_nested_digest(tmp_path):
    authorized = hashlib.sha256(b"authorized").hexdigest()
    foreign = hashlib.sha256(b"foreign").hexdigest()
    registry = tmp_path / "assets.json"
    registry.write_text(
        json.dumps({"assets": {"foreign": {"object_id": "foreign-object", "digest": foreign}}}),
        encoding="utf-8",
    )

    class Objects(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.fetches = []

        def get_object(self, digest):
            self.fetches.append(digest)
            return b"foreign"

    runtime = Objects()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    with pytest.raises(HostError, match="not authorized"):
        host._materialize_inputs(
            {
                "input_object_ids": [authorized],
                "spec": {"inputs": {"assets_registry": str(registry)}},
            },
            tmp_path / "attempt",
        )
    assert runtime.fetches == []


def test_command_cwd_must_stay_in_attempt_or_source_scope(tmp_path):
    _write_manifest(tmp_path / "echo", cwd=str(tmp_path / "outside"))
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    record = host.capabilities["test.echo"]
    with pytest.raises(HostError, match="cwd escapes"):
        host._run_command_definition(record, {}, tmp_path / "attempt" / "outputs", tmp_path / "attempt")


def test_cancellation_terminates_descendant_process_group(tmp_path):
    root = tmp_path / "group"
    _write_manifest(root)
    manifest = json.loads((root / "executor.yaml").read_text(encoding="utf-8"))
    manifest["command"]["argv"] = [
        "{python_exec}",
        "-c",
        "import os,time; from pathlib import Path; p=os.fork(); Path('{out}/child.pid').write_text(str(p if p else os.getpid())); time.sleep(30)",
    ]
    (root / "executor.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    started = time.monotonic()

    def cancelled():
        return time.monotonic() - started > 0.75

    with pytest.raises(HostCancelled):
        host._run_command_definition(
            host.capabilities["test.echo"], {}, output_root, attempt, cancelled=cancelled
        )
    child_pid = int((output_root / "child.pid").read_text(encoding="utf-8"))
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("descendant survived cancellation")


@pytest.mark.parametrize("failure", [
    "unavailable", "observation-error", "interrupted-census", "escaped-child",
    "pid-reuse", "parent-missing", "signal-error",
])
def test_tree_uncertainty_retains_attempt_and_blocks_claim_after_group_cleanup(
    tmp_path, monkeypatch, failure,
):
    """Root-only cessation never certifies an uncertain graph at the host boundary.

    Acquisition/census are deterministic injected boundaries here. Census
    failure producers have separate deterministic production-function tests.
    Neither layer proves the current-native negative integration; that gap
    remains explicit. Do not create an unowned writer to model uncertainty.
    """
    root = tmp_path / "writer-pack"
    _write_manifest(root)
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[root], client=runtime, attempt_base=tmp_path / "attempts")
    host.discover()
    task = {"task": {
        "id": "census-task", "capability": "test.echo", "project_id": "demo",
        "attempt_id": "census-attempt", "fence": 1, "spec": {"spec": {"inputs": {}}},
    }}
    runtime.tasks["census-task"] = task
    attempt = tmp_path / "attempts" / "census-task-census-attempt"
    waits = []
    signals = []
    root_only_stops = []

    class OriginalProcess:
        pid = 100
        returncode = None
        _astrid_process_birth = "original-root"
        _astrid_tree_members = {100: "original-root", 200: "detached-writer"}

        def poll(self):
            return self.returncode

        def wait(self, *, timeout):
            assert self.returncode is not None
            waits.append((self.pid, timeout, self.returncode))
            return self.returncode

    process = OriginalProcess()
    process._astrid_custody = SimpleNamespace(signal=lambda *args: signals.append(args))

    def acquire(_argv, **_kwargs):
        assert (attempt / "outputs").is_dir()
        (attempt / "outputs" / "retained.txt").write_text("attempt evidence")
        (attempt / "outputs" / "writer.txt").write_text("unresolved writer evidence")
        return process

    def fail_census(original):
        assert original is process
        # Inject the current custody boundary's sticky failure, rather than
        # assuming an owned launch has not yet captured its initial tree.
        original._astrid_tree_uncertain = failure
        raise CleanupUncertainError(f"injected retained census failure: {failure}")

    def root_only_cessation(original, **_kwargs):
        assert original is process
        original._astrid_custody.signal(original.pid, original._astrid_process_birth, signal.SIGTERM)
        original.returncode = -signal.SIGTERM
        original.wait(timeout=0)
        root_only_stops.append(original.pid)
        # No graph proof, descendant signal, or uncertainty reset is supplied.

    monkeypatch.setattr(generic_host, "popen_owned_group", acquire)
    monkeypatch.setattr(generic_host, "observe_tree", fail_census)
    monkeypatch.setattr(generic_host, "_terminate_process_group", root_only_cessation)
    with pytest.raises(HostError, match="owned cleanup incomplete"):
        host.run_task(task, lease_token="lease-census")
    assert root_only_stops == [100]
    assert waits == [(100, 0, -signal.SIGTERM)]
    assert signals == [(100, "original-root", signal.SIGTERM)]
    assert process._astrid_tree_uncertain == failure
    with pytest.raises(CleanupUncertainError, match=failure):
        process_group.verify_tree_absent(process)
    assert host._cleanup_uncertain
    assert host.last_cleanup_receipt["status"] == "uncertain"
    assert attempt.is_dir()
    assert (attempt / "outputs" / "retained.txt").read_text() == "attempt evidence"
    assert (attempt / "outputs" / "writer.txt").read_text() == "unresolved writer evidence"
    with pytest.raises(HostError, match="cleanup uncertainty"):
        host.claim_once()
    with pytest.raises(HostError, match="cleanup uncertainty"):
        host.run_task(task, lease_token="later-lease")
    assert not hasattr(runtime, "claim_payload")
    assert runtime.uploaded_objects == {} and runtime.settlements == []


def test_cancellation_verifies_detached_writer_tree_before_cleanup_and_reuses_host(
    tmp_path, monkeypatch,
):
    root = tmp_path / "tree-pack"
    manifest_path = _write_manifest(root, capability_id="test.tree")
    manifest = json.loads(manifest_path.read_text())
    registry = tmp_path / "tree-identities"
    fixture = Path(__file__).parent / "core/rendering/fixtures/owned_tree_backend.py"
    manifest["command"]["argv"] = ["{python_exec}", str(fixture), "pack", "{out}", str(registry)]
    manifest_path.write_text(json.dumps(manifest))
    _write_manifest(tmp_path / "echo")
    observed = []
    original_observe = generic_host.observe_tree
    original_cleanup = generic_host._cleanup_ephemeral_attempt
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])

    def observe(process):
        live = original_observe(process)
        if not observed:
            observed.append(process)
        return live

    class CancelWhenOwned(FakeRuntime):
        def task(self, task_id):
            result = self.tasks[task_id]
            if task_id == "tree-task" and (registry / "worker.pid").exists() and observed:
                identities = {int(path.read_text()) for path in registry.glob("*.pid")}
                if len(identities) == 5 and identities <= observed[0]._astrid_tree_members.keys():
                    pids = {path.stem: int(path.read_text()) for path in registry.glob("*.pid")}
                    census = process_group._process_snapshot()
                    for parent, child in (("pack", "backend"), ("backend", "node"), ("node", "browser"), ("browser", "worker")):
                        assert census[pids[child]].ppid == pids[parent]
                    assert census[pids["backend"]].pgid == pids["backend"] != pids["pack"]
                    assert census[pids["browser"]].pgid == pids["browser"] != pids["backend"]
                    result["task"]["status"] = "cancelled"
            return result

    runtime = CancelWhenOwned()
    host = GenericPackHost(pack_roots=[root, tmp_path / "echo"], client=runtime, attempt_base=tmp_path / "attempts")
    host.discover()
    task = {"task": {
        "id": "tree-task", "capability": "test.tree", "project_id": "demo",
        "attempt_id": "tree-attempt", "fence": 1, "spec": {"spec": {"inputs": {}}},
    }}
    runtime.tasks["tree-task"] = task
    deleted = []

    def verify_before_delete(attempt):
        if attempt.name == "tree-task-tree-attempt":
            assert len(observed[0]._astrid_tree_members) == 5
            snapshot = process_group._process_snapshot()
            assert snapshot
            assert not any(pid in snapshot for pid in observed[0]._astrid_tree_members)
            assert sibling.poll() is None and os.getpid() in snapshot
        original_cleanup(attempt)
        deleted.append(attempt)

    monkeypatch.setattr(generic_host, "observe_tree", observe)
    monkeypatch.setattr(generic_host, "_cleanup_ephemeral_attempt", verify_before_delete)
    try:
        result = host.run_task(task, lease_token="lease-tree")
        assert result["status"] == "cancelled"
        assert runtime.tasks["tree-task"]["task"]["status"] == "cancelled"
        assert runtime.uploaded_objects == {} and runtime.settlements == [] and runtime.failures == []
        assert host.last_cleanup_receipt["status"] == "deleted"
        assert not host._cleanup_uncertain and not host._active_processes
        stopped_write = (registry / "last-write.txt").read_text()
        time.sleep(0.1)
        assert (registry / "last-write.txt").read_text() == stopped_write
        assert not (tmp_path / "attempts" / "tree-task-tree-attempt").exists()
        later = {"task": {
            **task["task"], "id": "later-task", "capability": "test.echo",
            "attempt_id": "later-attempt", "status": "running",
        }}
        runtime.tasks["later-task"] = later
        assert host.run_task(later, lease_token="lease-later")["task"]["status"] == "completed"
        assert len(runtime.settlements) == 1 and not host._cleanup_uncertain
        assert len(deleted) == 2 and sibling.poll() is None
    finally:
        # All fixture PIDs are descendants captured while ancestry was alive.
        for process in observed:
            snapshot = process_group._process_snapshot()
            for pid, birth in reversed(tuple(getattr(process, "_astrid_tree_members", {}).items())):
                info = snapshot.get(pid)
                if info is not None and info.birth == birth:
                    os.kill(pid, signal.SIGKILL)
            process.wait(timeout=3)
        sibling.terminate()
        sibling.wait(timeout=3)


def test_cancellation_reaps_sigterm_resistant_descendant_after_leader_exit(tmp_path):
    """A leader that exits on TERM must not let its stubborn child escape."""
    root = tmp_path / "leader-exits"
    _write_manifest(root)
    manifest = json.loads((root / "executor.yaml").read_text(encoding="utf-8"))
    child_code = (
        "import os,signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('{out}/child.pid').write_text(str(os.getpid())); "
        "os.write(int(sys.argv[1]), b'1'); os.close(int(sys.argv[1])); "
        "time.sleep(30)"
    )
    leader_code = (
        "import os,signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        f"ready_r,ready_w=os.pipe(); subprocess.Popen([sys.executable,'-c',{child_code!r},str(ready_w)], pass_fds=(ready_w,)); os.close(ready_w); os.read(ready_r,1); os.close(ready_r); "
        "time.sleep(30)"
    )
    # Keeping the child in the inherited process group is intentional: the
    # host owns that whole group.
    manifest["command"]["argv"] = ["{python_exec}", "-c", leader_code]
    (root / "executor.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    started = time.monotonic()

    def cancelled():
        return time.monotonic() - started > 0.75

    with pytest.raises(HostCancelled):
        host._run_command_definition(
            host.capabilities["test.echo"], {}, output_root, attempt, cancelled=cancelled
        )
    child_pid = int((output_root / "child.pid").read_text(encoding="utf-8"))
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("SIGTERM-resistant descendant survived leader-exit cancellation")


def test_cancellation_reaps_descendant_spawned_by_sigterm_handler(tmp_path):
    """A TERM handler may create a child after the first group census."""
    root = tmp_path / "late-child"
    _write_manifest(root)
    manifest = json.loads((root / "executor.yaml").read_text(encoding="utf-8"))
    child_code = (
        "import os,signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text(str(os.getpid())); "
        "os.write(int(sys.argv[2]), b'1'); os.close(int(sys.argv[2])); time.sleep(30)"
    )
    leader_code = (
        "import os,signal,subprocess,sys,time\n"
        f"def late(*_):\n    ready_r,ready_w=os.pipe(); subprocess.Popen([sys.executable,'-c',{child_code!r},'{str(root / 'child.pid')}',str(ready_w)], pass_fds=(ready_w,)); os.close(ready_w); os.read(ready_r,1); os.close(ready_r)\n"
        "signal.signal(signal.SIGTERM, late)\n"
        "time.sleep(30)\n"
    )
    manifest["command"]["argv"] = ["{python_exec}", "-c", leader_code]
    (root / "executor.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    started = time.monotonic()

    def cancelled():
        return time.monotonic() - started > 0.75

    with pytest.raises(HostCancelled):
        host._run_command_definition(
            host.capabilities["test.echo"], {}, output_root, attempt, cancelled=cancelled
        )
    child_pid = int((root / "child.pid").read_text(encoding="utf-8"))
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("SIGTERM-handler child survived cancellation")


def test_cleanup_does_not_signal_a_reused_group_after_leader_exit(monkeypatch):
    """An exited leader PID must never be reused as a killpg target."""
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait()

    def unexpected_killpg(*_args):
        raise AssertionError("cleanup signaled a group after its leader exited")

    monkeypatch.setattr(os, "killpg", unexpected_killpg)
    monkeypatch.setattr(os, "kill", unexpected_killpg)
    # An unobserved raw Popen cannot certify its descendants after root exit.
    # Refusal is required; leader exit is not a cleanup-custody substitute.
    with pytest.raises(CleanupUncertainError, match="ancestry capture"):
        _terminate_process_group(process)


def test_signal_revalidates_group_identity_immediately_before_killpg(monkeypatch):
    """A changed incarnation in the final signal window fails before custody signal."""
    signal_calls = []
    process = SimpleNamespace(
        pid=100, _astrid_process_birth="root",
        _astrid_custody=SimpleNamespace(
            observe=lambda _members: None,
            signal=lambda *args: signal_calls.append(args),
        ),
    )
    initial = {100: process_group._ProcessInfo(100, 1, 100, "root")}
    reused = {100: process_group._ProcessInfo(100, 1, 100, "different-root")}
    snapshots = iter((initial, reused))
    monkeypatch.setattr(process_group, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(os, "killpg", lambda *_args: pytest.fail("reused group was signalled"))
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("numeric member was signalled"))
    with pytest.raises(CleanupUncertainError, match="owned signal identity was reused"):
        process_group.signal_group(process, signal.SIGTERM)
    assert signal_calls == []


def test_tree_cleanup_rejects_reused_child_before_adopting_descendants(monkeypatch):
    """A reused child PID cannot pull an unrelated descendant into cleanup."""
    process = SimpleNamespace(pid=100, _astrid_process_birth="root")
    known: dict[int, str] = {}
    initial = {
        100: process_group._ProcessInfo(100, 1, 100, "root"),
        200: process_group._ProcessInfo(200, 100, 100, "child-old"),
    }
    assert process_group._tree_members(process, known, initial) == {
        100: "root",
        200: "child-old",
    }

    # PID 200 is now a different process.  Its child 300 is unrelated and
    # must not be adopted merely because the numeric parent PID matches.
    reused = {
        100: process_group._ProcessInfo(100, 1, 100, "root"),
        200: process_group._ProcessInfo(200, 100, 100, "child-new"),
        300: process_group._ProcessInfo(300, 200, 100, "unrelated"),
    }
    assert process_group._tree_members(process, known, reused) == {
        100: "root",
    }
    signalled = []
    process._astrid_custody = SimpleNamespace(signal=lambda *args: signalled.append(args))
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("numeric signal is not cleanup authority"))
    monkeypatch.setattr(os, "killpg", lambda *_args: pytest.fail("numeric group is not cleanup authority"))
    process_group._signal_valid_tree_members(process, known, signal.SIGKILL, reused)
    assert signalled == [(100, "root", signal.SIGKILL)]
    # The same valid numeric/root birth observation without a retained handle
    # must still fail, rather than silently falling back to os.kill.
    del process._astrid_custody
    with pytest.raises(CleanupUncertainError, match="lacks retained cleanup custody"):
        process_group._signal_valid_tree_members(process, known, signal.SIGKILL, reused)
    assert signalled == [(100, "root", signal.SIGKILL)]


def _package_preflight_host(tmp_path, *, independent_id="test.independent"):
    _write_manifest(tmp_path / "dependent", capability_id="test.dependent")
    _write_manifest(tmp_path / "independent", capability_id=independent_id)
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps({
        "schema_version": 1,
        "capabilities": [
            {
                "id": capability_id,
                "disposition": "optional",
                "evidence_reason": "focused package preflight fixture",
                "required_packages": packages,
            }
            for capability_id, packages in (
                ("test.dependent", ["astrid_preflight_missing", "astrid_preflight_present"]),
                (independent_id, []),
            )
        ],
    }), encoding="utf-8")
    host = GenericPackHost(pack_roots=[tmp_path], capability_matrix=matrix, credential_source={})
    host.discover()
    return host


def test_package_preflight_strict_finder_refusal_is_capability_local(tmp_path, monkeypatch):
    host = _package_preflight_host(tmp_path)
    looked_up = []
    ambient_lookups = []
    packages = {"astrid_preflight_missing", "astrid_preflight_present"}

    class StrictFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname not in packages:
                return None
            looked_up.append(fullname)
            if fullname == "astrid_preflight_missing":
                raise ModuleNotFoundError("outside reviewed import mapping", name=fullname)
            return ModuleSpec(fullname, loader=None)

    class AmbientFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname in packages:
                ambient_lookups.append(fullname)
                pytest.fail("package preflight must not retry ambient resolution")

    monkeypatch.setattr(sys, "meta_path", [StrictFinder(), AmbientFinder(), *sys.meta_path])
    records = {record.id: record for record in host.preflight()}

    assert looked_up == ["astrid_preflight_missing", "astrid_preflight_present"]
    assert ambient_lookups == []
    assert records["test.dependent"].preflight["packages"] == {
        "ok": False, "missing": ["astrid_preflight_missing"],
    }
    assert records["test.dependent"].ready is False
    assert records["test.independent"].preflight["packages"] == {"ok": True, "missing": []}
    assert records["test.independent"].ready is True
    assert packages.isdisjoint(sys.modules)  # Discovery must not import package code.


def test_package_preflight_none_is_capability_local(tmp_path, monkeypatch):
    host = _package_preflight_host(tmp_path)
    looked_up = []

    def find_spec(name):
        looked_up.append(name)
        assert name in {"astrid_preflight_missing", "astrid_preflight_present"}
        return None if name == "astrid_preflight_missing" else object()

    monkeypatch.setattr(generic_host.importlib.util, "find_spec", find_spec)
    records = {record.id: record for record in host.preflight()}

    assert looked_up == ["astrid_preflight_missing", "astrid_preflight_present"]
    assert records["test.dependent"].preflight["packages"] == {
        "ok": False, "missing": ["astrid_preflight_missing"],
    }
    assert records["test.dependent"].ready is False
    assert records["test.independent"].preflight["packages"] == {"ok": True, "missing": []}
    assert records["test.independent"].ready is True


@pytest.mark.parametrize("error_type", [ImportError, RuntimeError, ValueError, PermissionError, CustodyError])
def test_package_preflight_unrelated_finder_fault_propagates(tmp_path, monkeypatch, error_type):
    host = _package_preflight_host(tmp_path)
    fault = error_type("finder failed for a reason other than a missing module")

    def find_spec(name):
        assert name == "astrid_preflight_missing"
        raise fault

    monkeypatch.setattr(generic_host.importlib.util, "find_spec", find_spec)
    with pytest.raises(error_type) as caught:
        host.preflight()
    assert caught.value is fault


@pytest.mark.parametrize("disposition", ["optional", "unsupported", "retired"])
def test_package_preflight_atomic_registration_preserves_status(tmp_path, monkeypatch, disposition):
    host = _package_preflight_host(tmp_path)
    host.matrix["test.dependent"]["disposition"] = disposition
    runtime = FakeRuntime()
    host.client = runtime

    def find_spec(name):
        if name == "astrid_preflight_missing":
            raise ModuleNotFoundError("outside reviewed import mapping")
        assert name == "astrid_preflight_present"
        return ModuleSpec(name, loader=None)

    monkeypatch.setattr(generic_host.importlib.util, "find_spec", find_spec)
    host.register()

    assert len(runtime.registrations) == 1
    assert runtime.capability_registrations == []
    records = {row["capability_id"]: row for row in runtime.registrations[0][1]["capabilities"]}
    assert records["test.independent"]["status"] == "ready"
    assert records["test.dependent"]["status"] == (
        "unavailable" if disposition == "optional" else disposition
    )
    assert records["test.dependent"]["unavailable_reason"] == (
        "packages:missing=astrid_preflight_missing" if disposition == "optional"
        else "focused package preflight fixture"
    )


def test_package_preflight_present_packages_remain_ready(tmp_path, monkeypatch):
    host = _package_preflight_host(tmp_path)
    looked_up = []

    def find_spec(name):
        looked_up.append(name)
        return ModuleSpec(name, loader=None)

    monkeypatch.setattr(generic_host.importlib.util, "find_spec", find_spec)
    records = host.preflight("test.dependent")
    assert looked_up == ["astrid_preflight_missing", "astrid_preflight_present"]
    assert records[0].preflight["packages"] == {"ok": True, "missing": []}
    assert records[0].ready is True


def test_package_preflight_does_not_override_missing_speech_binary(tmp_path, monkeypatch):
    host = _package_preflight_host(tmp_path, independent_id="generation.generate_speech")
    host.matrix["generation.generate_speech"]["required_binaries"] = ["edge-tts"]

    def require(binaries):
        assert binaries == ("edge-tts",)
        raise generic_host.ExecutableAdmissionError("declared speech binary is missing")

    monkeypatch.setattr(host._executable_admission, "require", require)
    record = host.preflight("generation.generate_speech")[0]

    assert record.preflight["packages"] == {"ok": True, "missing": []}
    assert record.preflight["binaries"]["ok"] is False
    assert record.preflight["binaries"]["missing"] == ["edge-tts"]
    assert record.ready is False


def test_discovery_digest_and_truthful_preflight(tmp_path):
    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path])
    records = host.discover()
    assert [record.id for record in records] == ["test.echo"]
    assert records[0].source_digest
    assert host.preflight("test.echo")[0].ready
    original = records[0].capability_digest
    host.register()
    _write_manifest(tmp_path / "changed")
    changed = host.refresh()
    assert changed == ()  # a distinct capability does not invalidate the old one
    (tmp_path / "echo" / "executor.yaml").write_text((tmp_path / "changed" / "executor.yaml").read_text().replace('1.0', '2.0'), encoding="utf-8")
    assert host.refresh()[0].capability_digest != original
    with pytest.raises(Exception, match="deliberate re-registration"):
        host.register()
    host.register(deliberate=True)


def test_claim_only_admits_capabilities_that_are_currently_ready(tmp_path, monkeypatch):
    """The host candidate set must equal its current readiness predicate."""
    monkeypatch.delenv("ASTRID_PROVIDER_KEY", raising=False)
    root = tmp_path / "required_env"
    root.mkdir()
    (root / "executor.yaml").write_text(
        json.dumps({
            "schema_version": 1,
            "id": "fixture.required_env",
            "name": "Fixture Required Environment",
            "kind": "external",
            "version": "1.0",
            "command": {"argv": ["{python_exec}", "-c", "pass"]},
            "outputs": [],
            "isolation": {"mode": "subprocess", "network": False},
            "metadata": {
                "adapter_family": "cpu",
                "required_env": ["ASTRID_PROVIDER_KEY"],
            },
        }),
        encoding="utf-8",
    )
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()

    assert host.capabilities["fixture.required_env"].ready is False
    assert host.claim_once() is None
    assert not hasattr(runtime, "claim_payload")

    # Rotation is observed on the next claim cycle, without rebuilding the
    # host, and the now-ready capability becomes the only candidate.
    monkeypatch.setenv("ASTRID_PROVIDER_KEY", "fixture-secret")
    assert host.claim_once() is None
    assert runtime.claim_payload["capability_ids"] == ["fixture.required_env"]


def test_hivemind_contributor_key_file_satisfies_provider_readiness(tmp_path, monkeypatch):
    """Astrid login's standard key file is the host credential source."""
    monkeypatch.delenv("HIVEMIND_CONTRIBUTOR_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    key_path = tmp_path / ".hivemind" / "key"
    key_path.parent.mkdir()
    key_path.write_text("hm_" + "a" * 64, encoding="utf-8")

    root = tmp_path / "hivemind"
    root.mkdir()
    (root / "executor.yaml").write_text(
        json.dumps({
            "schema_version": 1,
            "id": "hivemind.contribute",
            "name": "Hivemind Contribute",
            "kind": "external",
            "version": "1.0",
            "command": {"argv": ["{python_exec}", "-c", "pass"]},
            "outputs": [],
            "isolation": {"mode": "subprocess", "network": True},
            "metadata": {
                "adapter_family": "provider",
                "required_env": ["HIVEMIND_CONTRIBUTOR_KEY"],
            },
        }),
        encoding="utf-8",
    )

    host = GenericPackHost(pack_roots=[tmp_path], capability_matrix=None)
    host.discover()

    record = host.preflight("hivemind.contribute")[0]
    assert record.preflight["credentials"] == {"ok": True, "missing": []}

def test_source_and_dependency_digests_invalidate_registration(tmp_path):
    _write_manifest(tmp_path / "base")
    child = tmp_path / "child"
    child.mkdir()
    (child / "executor.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "test.child",
                "name": "Child",
                "kind": "external",
                "version": "1.0",
                "graph": {"depends_on": ["test.echo"]},
                "command": {"argv": ["{python_exec}", "-c", "pass"]},
                "outputs": [],
            }
        ),
        encoding="utf-8",
    )
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    host.register()

    # A runtime source file can change without changing the manifest digest.
    (tmp_path / "base" / "runtime.py").write_text("changed", encoding="utf-8")
    changed = host.refresh()
    assert [record.id for record in changed] == ["test.echo"]
    with pytest.raises(HostError, match="source digest changed: test.echo"):
        host.register()
    host.register(deliberate=True)

    # A dependency manifest change invalidates its consumer's dependency
    # digest as well as the dependency's own source/capability state.
    (tmp_path / "base" / "executor.yaml").write_text(
        (tmp_path / "base" / "executor.yaml").read_text(encoding="utf-8").replace('"1.0"', '"2.0"'),
        encoding="utf-8",
    )
    changed = host.refresh()
    assert {record.id for record in changed} == {"test.echo", "test.child"}
    with pytest.raises(HostError, match="dependency digest changed: test.child"):
        host.register()


def test_removed_capability_invalidates_and_is_withdrawn_on_deliberate_reregistration(tmp_path):
    _write_manifest(tmp_path / "base", capability_id="test.base")
    _write_manifest(tmp_path / "removed")

    class WithdrawalRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.withdrawals = []

        def withdraw_capability(self, capability_id, *, digest, reason):
            self.withdrawals.append((capability_id, digest, reason))

    runtime = WithdrawalRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    host.register()
    removed_digest = host.capabilities["test.echo"].capability_digest

    (tmp_path / "removed" / "executor.yaml").unlink()
    assert [record.id for record in host.refresh()] == ["test.echo"]
    with pytest.raises(HostError, match="capability removed: test.echo"):
        host.register()

    result = host.register(deliberate=True)
    assert result["withdrawn_capabilities"] == ["test.echo"]
    assert runtime.withdrawals == [
        ("test.echo", removed_digest, "capability removed from source checkout")
    ]
    assert [
        item["capability_id"]
        for item in runtime.registrations[-1][1]["capabilities"]
    ] == ["test.base"]


def test_registration_carries_epochs_and_runtime_epoch_change_is_deterministic(tmp_path):
    _write_manifest(tmp_path / "echo")

    class EpochRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.epoch = 7

        def health(self):
            return {
                "status": "ok",
                "protocol": "workspace.v1",
                "schema_digest": self.schema_digest,
                **super().health(),
                "runtime_epoch": self.epoch,
            }

    runtime = EpochRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    host.register()
    payload = runtime.registrations[0][1]
    assert payload["protocol_version"] == "workspace.v1"
    assert payload["runtime_epoch"] == 7
    assert payload["source_epoch"] == host.source_epoch
    assert payload["dependency_digest"]
    capability = payload["capabilities"][0]
    assert capability["capability_id"] == "test.echo"
    assert capability["definition_digest"] == host.capabilities["test.echo"].capability_digest
    assert capability["status"] == "ready"

    runtime.epoch = 8
    with pytest.raises(HostError, match="runtime epoch changed; deliberate re-registration required"):
        host.register()


def test_runtime_protocol_mismatch_blocks_registration_before_publish(tmp_path):
    _write_manifest(tmp_path / "echo")

    class IncompatibleRuntime(FakeRuntime):
        def health(self):
            return {
                "status": "ok",
                "protocol": "workspace.v0",
                "schema_digest": self.schema_digest,
                "runtime_epoch": 1,
            }

    runtime = IncompatibleRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    with pytest.raises(HostError, match="runtime compatibility blocked: protocol expected=workspace.v1 actual=workspace.v0"):
        host.register()
    assert runtime.registrations == []


def test_runtime_unhealthy_status_blocks_registration_before_publish(tmp_path):
    _write_manifest(tmp_path / "echo")

    class UnhealthyRuntime(FakeRuntime):
        def health(self):
            return {
                "status": "unhealthy",
                "protocol": "workspace.v1",
                "schema_digest": self.schema_digest,
                "runtime_epoch": 1,
            }

    runtime = UnhealthyRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)

    with pytest.raises(HostError, match="status expected=ok actual=unhealthy"):
        host.register()

    assert runtime.capability_registrations == []
    assert runtime.registrations == []


def test_registration_failure_is_typed_and_has_no_capability_prepublication(tmp_path):
    from banodoco_workspace_client import ApiError

    _write_manifest(tmp_path / "echo")

    class FailingRuntime(FakeRuntime):
        def register_executor(self, executor_id, **payload):
            raise ApiError(
                503,
                "registration_unavailable",
                "executor registration unavailable",
                request_id="request-registration-1",
                details={"retryable": False},
            )

    runtime = FailingRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)

    with pytest.raises(HostRegistrationError) as failure:
        host.register()

    assert failure.value.code == "registration_unavailable"
    assert failure.value.request_id == "request-registration-1"
    assert failure.value.status == 503
    assert failure.value.details == {"retryable": False}
    assert runtime.capability_registrations == []
    assert runtime.registrations == []


def test_failed_replacement_registration_does_not_withdraw_removed_capability(tmp_path):
    from banodoco_workspace_client import ApiError

    _write_manifest(tmp_path / "kept", capability_id="test.kept")
    removed = _write_manifest(tmp_path / "removed")

    class FailingReplacementRuntime(FakeRuntime):
        fail_registration = False

        def __init__(self):
            super().__init__()
            self.withdrawals = []

        def register_executor(self, executor_id, **payload):
            if self.fail_registration:
                raise ApiError(
                    503,
                    "registration_unavailable",
                    "executor registration unavailable",
                    request_id="request-replacement-1",
                )
            return super().register_executor(executor_id, **payload)

        def withdraw_capability(self, capability_id, *, digest, reason):
            self.withdrawals.append((capability_id, digest, reason))

    runtime = FailingReplacementRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.register()
    removed.unlink()
    host.refresh()
    runtime.fail_registration = True

    with pytest.raises(HostRegistrationError):
        host.register(deliberate=True)

    assert runtime.withdrawals == []
    assert runtime.capability_registrations == []


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://runtime.example",
        "http://192.0.2.1:8000",
        "http://127.0.0.1:not-a-port",
    ),
)
def test_runtime_protocol_client_rejects_non_loopback_or_malformed_endpoint(endpoint):
    with pytest.raises(HostError, match="loopback|malformed"):
        RuntimeProtocolClient(endpoint, "worker-token")


def test_runtime_protocol_client_uses_worker_token_contract_without_user_handshake(
    monkeypatch,
):
    class WorkerGenerated:
        handshake_called = False

        def __init__(self, endpoint, token, *, timeout=30.0):
            self.endpoint = endpoint
            self.token = token
            self.timeout = timeout
            self.registration_payloads = []

        def handshake(self, *_args, **_kwargs):
            self.handshake_called = True
            raise AssertionError("worker adapter must not fabricate a user handshake")

        def register_executor(self, executor, *, idempotency_key):
            self.registration_payloads.append(executor)
            return {"executor_id": executor["executor_id"], "idempotency_key": idempotency_key}

    monkeypatch.setattr("banodoco_workspace_client.WorkspaceClient", WorkerGenerated)
    client = RuntimeProtocolClient(
        "http://127.0.0.1:8765", "worker-token", timeout=4.25
    )
    response = client.register_executor(
        "worker-1",
        capabilities=[],
        max_concurrency=1,
        resource_keys=[],
        source_digest="sha256:" + "a" * 64,
        dependency_digest="sha256:" + "b" * 64,
        source_epoch="source-epoch-1",
    )

    assert client.WORKER_SCOPES == (
        "handshake",
        "worker:register",
        "worker:execute",
        "projects:read",
        "tasks:read",
        "objects:read",
        "objects:write",
    )
    assert response["executor_id"] == "worker-1"
    assert client.generated.handshake_called is False
    assert client.generated.timeout == 4.25
    wire = client.generated.registration_payloads[0]
    assert wire["source_digest"] == "sha256:" + "a" * 64
    assert wire["dependency_digest"] == "sha256:" + "b" * 64
    assert wire["source_epoch"] == "source-epoch-1"
    assert "schema_digest" not in wire


def test_runtime_protocol_client_registration_retries_are_session_idempotent(
    monkeypatch,
):
    class WorkerGenerated:
        def __init__(self, *_args, **_kwargs):
            self.keys = []

        def register_executor(self, executor, *, idempotency_key):
            self.keys.append(idempotency_key)
            return {"executor_id": executor["executor_id"], "idempotency_key": idempotency_key}

    monkeypatch.setattr("banodoco_workspace_client.WorkspaceClient", WorkerGenerated)
    kwargs = {
        "capabilities": [],
        "max_concurrency": 1,
        "resource_keys": [],
        "source_digest": "sha256:" + "a" * 64,
        "dependency_digest": "sha256:" + "b" * 64,
        "source_epoch": "source-epoch-1",
    }
    first = RuntimeProtocolClient("http://127.0.0.1:8765", "worker-token")
    first.register_executor("worker-1", **kwargs)
    first.register_executor("worker-1", **kwargs)
    first.renew_registration_session()
    first.register_executor("worker-1", **kwargs)
    second = RuntimeProtocolClient("http://127.0.0.1:8765", "worker-token")
    second.register_executor("worker-1", **kwargs)

    assert first.generated.keys[0] == first.generated.keys[1]
    assert first.generated.keys[0] != first.generated.keys[2]
    assert first.generated.keys[2] != second.generated.keys[0]


def test_claim_loop_renews_executor_registration_after_liveness_interval(
    tmp_path, monkeypatch
):
    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path], client=FakeRuntime())
    host.discover()
    renewed = []
    monkeypatch.setattr(host, "_renew_executor_registration", lambda: renewed.append(True))
    host._registration_refresh_deadline = 0.0
    monkeypatch.setattr(host, "claim_once", lambda: None)
    host.run(once=True)
    assert renewed == [True]


def test_vendored_runtime_client_timeout_is_bounded_and_request_correlated(monkeypatch):
    from banodoco_workspace_client import ApiError, WorkspaceClient

    captured = {}

    def timed_out(request, *, timeout):
        captured["timeout"] = timeout
        captured["request_id"] = request.get_header("X-request-id")
        raise TimeoutError("fixture timeout")

    monkeypatch.setattr("urllib.request.urlopen", timed_out)

    with pytest.raises(ApiError) as failure:
        WorkspaceClient("http://127.0.0.1:8765", timeout=0.125).health()

    assert captured["timeout"] == 0.125
    assert captured["request_id"].startswith("request-")
    assert failure.value.code == "transport_timeout"
    assert failure.value.request_id == captured["request_id"]
    assert failure.value.details == {}


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_vendored_runtime_client_rejects_unbounded_timeout(timeout):
    from banodoco_workspace_client import WorkspaceClient

    with pytest.raises(ValueError, match="finite and positive"):
        WorkspaceClient("http://127.0.0.1:8765", timeout=timeout)


def test_vendored_runtime_client_preserves_server_failure_diagnostic():
    from banodoco_workspace_client import ApiError, WorkspaceClient

    def transport(_method, _path, _headers, _body):
        return 503, {}, json.dumps(
            {
                "code": "registration_unavailable",
                "message": "executor registration unavailable",
                "request_id": "request-server-1",
                "details": {"retryable": False},
            }
        ).encode()

    with pytest.raises(ApiError) as failure:
        WorkspaceClient("http://127.0.0.1:8765", transport=transport).health()

    assert failure.value.code == "registration_unavailable"
    assert failure.value.request_id == "request-server-1"
    assert failure.value.details == {"retryable": False}


def test_cli_writes_terminal_correlated_registration_failure_marker(
    tmp_path, monkeypatch, capsys
):
    from banodoco_workspace_client import ApiError
    from banodoco_workspace_client.contract_metadata import SCHEMA_DIGEST
    from astrid.core.execution import generic_host
    from astrid.core.gateway.dispatch import compose_profile_handoff

    _write_manifest(tmp_path / "pack")
    support = tmp_path / "support"
    support.mkdir()
    ready = support / "generic-host.ready.json"
    credential = support / "worker.token"
    credential.write_text("worker-token", encoding="utf-8")
    credential.chmod(0o600)
    boot_manifest = support / "astrid-host" / "boot-manifest.json"
    compose_profile_handoff(boot_manifest, support_root=support)

    class FailingClient:
        schema_digest = SCHEMA_DIGEST

        def __init__(self, _endpoint, _credential):
            pass

        def health(self):
            return {
                "status": "ok",
                "protocol": "workspace.v1",
                "schema_digest": self.schema_digest,
                "runtime_epoch": 1,
                "instance_id": "fixture-runtime-instance",
                "runtime_instance_id": "fixture-runtime-instance",
                "coordinator_epoch": "fixture-runtime-instance",
            }

        def register_capability(self, _capability_id, **_payload):
            raise ApiError(
                503,
                "registration_unavailable",
                "executor registration unavailable for worker-token",
                request_id="request-cli-1",
                details={"retryable": False},
            )

        def register_executor(self, _executor_id, **_payload):
            raise ApiError(
                503,
                "registration_unavailable",
                "executor registration unavailable for worker-token",
                request_id="request-cli-1",
                details={"retryable": False},
            )

    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", FailingClient)
    monkeypatch.setattr(generic_host, "process_birth_identity", lambda: "birth-cli-1")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "astrid-generic-host",
            "--pack-root",
            str(tmp_path / "pack"),
            "--runtime-endpoint",
            "http://127.0.0.1:8765",
            "--credential-file",
            str(credential),
            "--register",
            "--ready-file",
            str(ready),
            "--support-root",
            str(support),
            "--boot-manifest-path",
            str(boot_manifest),
        ],
    )

    assert generic_host._cli() == 1
    marker = json.loads(ready.read_text(encoding="utf-8"))
    assert marker == {
        "status": "failed",
        "terminal": True,
        "pid": os.getpid(),
        "process_birth_id": "birth-cli-1",
        "error": {
            "code": "registration_unavailable",
            "request_id": "request-cli-1",
            "message": "executor registration unavailable for [redacted]",
        },
    }
    assert "worker-token" not in ready.read_text(encoding="utf-8")
    assert '"terminal": true' in capsys.readouterr().err


def test_runtime_protocol_client_settlement_preserves_structured_result(monkeypatch):
    class WorkerGenerated:
        def __init__(self, endpoint, token, *, timeout=30.0):
            self.settlements = []

        def health(self):
            return {"runtime_epoch": 7}

        def settle_attempt(self, attempt_id, settlement, *, idempotency_key):
            self.settlements.append((attempt_id, settlement, idempotency_key))
            return settlement

    monkeypatch.setattr("banodoco_workspace_client.WorkspaceClient", WorkerGenerated)
    client = RuntimeProtocolClient("http://127.0.0.1:8765", "worker-token")
    client._attempt_runtime_epochs["attempt-1"] = 7
    result = {
        "adapter_family": "render",
        "capability_digest": "sha256:" + "a" * 64,
        "process_evidence": {"child_boundary": "subprocess"},
    }

    client.settle(
        "task-1",
        "lease-1",
        result=result,
        outputs=[],
        effect=None,
        attempt_id="attempt-1",
        fence=3,
    )

    _, wire, _ = client.generated.settlements[0]
    assert wire["result"] == result
    assert wire["runtime_epoch"] == 7
    assert "schema_digest" not in wire


def test_register_without_readiness_profile_publishes_empty_verified_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTRID_HOST_READINESS_PROFILE_PATH", raising=False)
    _write_manifest(tmp_path / "echo")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)

    host.register()

    assert runtime.registrations[0][1]["verified_facts"] == {}


def test_register_with_profile_missing_verified_facts_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(tmp_path / "echo")
    profile = tmp_path / "readiness.json"
    profile.write_text(json.dumps({"status": "ready"}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_HOST_READINESS_PROFILE_PATH", str(profile))
    monkeypatch.setenv(
        "ASTRID_HOST_READINESS_PROFILE_HASH",
        "sha256:" + hashlib.sha256(profile.read_bytes()).hexdigest(),
    )
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)

    with pytest.raises(HostError, match="missing verified_facts"):
        host.register()

    assert runtime.capability_registrations == []
    assert runtime.registrations == []


def test_register_with_profile_publishes_valid_verified_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(tmp_path / "echo")
    facts = {
        "exact": {"driver": "cuda-12.4/driver-550", "port": 8188},
        "minimum": {"vram_bytes": 16 * 1024**3, "scratch_bytes": 8 * 1024**3},
    }
    profile = tmp_path / "readiness.json"
    profile.write_text(json.dumps({"verified_facts": facts}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_HOST_READINESS_PROFILE_PATH", str(profile))
    monkeypatch.setenv(
        "ASTRID_HOST_READINESS_PROFILE_HASH",
        "sha256:" + hashlib.sha256(profile.read_bytes()).hexdigest(),
    )
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)

    host.register()

    assert runtime.registrations[0][1]["verified_facts"] == facts


@pytest.mark.parametrize(
    "profile_value",
    [
        {"verified_facts": []},
        {"verified_facts": {"exact": {"driver": ""}, "minimum": {}}},
        {"verified_facts": {"exact": {}, "minimum": {"vram_bytes": True}}},
    ],
)
def test_register_rejects_malformed_verified_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_value: object,
) -> None:
    _write_manifest(tmp_path / "echo")
    profile = tmp_path / "readiness.json"
    profile.write_text(json.dumps(profile_value), encoding="utf-8")
    monkeypatch.setenv("ASTRID_HOST_READINESS_PROFILE_PATH", str(profile))
    monkeypatch.setenv(
        "ASTRID_HOST_READINESS_PROFILE_HASH",
        "sha256:" + hashlib.sha256(profile.read_bytes()).hexdigest(),
    )
    runtime = FakeRuntime()

    with pytest.raises(HostError, match="verified_facts"):
        GenericPackHost(pack_roots=[tmp_path], client=runtime).register()

    assert runtime.capability_registrations == []
    assert runtime.registrations == []


def test_runtime_protocol_client_uses_a_fresh_idempotency_key_for_each_heartbeat(
    monkeypatch,
):
    class WorkerGenerated:
        def __init__(self, endpoint, token, *, timeout=30.0):
            self.heartbeats = []

        def health(self):
            return {"runtime_epoch": 7}

        def heartbeat_attempt(self, attempt_id, **payload):
            self.heartbeats.append((attempt_id, payload))
            return payload

    monkeypatch.setattr("banodoco_workspace_client.WorkspaceClient", WorkerGenerated)
    client = RuntimeProtocolClient("http://127.0.0.1:8765", "worker-token")
    client._attempt_runtime_epochs["attempt-1"] = 7

    client.heartbeat("task-1", "lease-1", attempt_id="attempt-1", fence=3)
    client.heartbeat("task-1", "lease-1", attempt_id="attempt-1", fence=3)

    first = client.generated.heartbeats[0][1]
    second = client.generated.heartbeats[1][1]
    assert first["runtime_epoch"] == second["runtime_epoch"] == 7
    assert first["idempotency_key"] != second["idempotency_key"]
    assert first["idempotency_key"].startswith("heartbeat-attempt-1-3-7-")


def test_register_and_run_uses_attempt_local_typed_output_and_cleanup(tmp_path):
    _write_manifest(tmp_path / "echo")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    result = host.register()
    assert runtime.registrations[0][1]["resource_keys"] == ["cpu"]
    assert runtime.capability_registrations == []
    assert runtime.registrations[0][1]["capabilities"][0]["capability_id"] == "test.echo"
    task = {
        "task": {
            "id": "task-1",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-1",
            "fence": 1,
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-1"] = task
    settled = host.run_task(task, lease_token="lease-1")
    assert settled["task"]["status"] == "completed"
    assert runtime.heartbeats == [("task-1", "lease-1", "attempt-1", 1)]
    outputs = runtime.settlements[0][2]["outputs"]
    evidence = runtime.settlements[0][2]["result"]["process_evidence"]
    assert evidence["capability_id"] == "test.echo"
    assert evidence["attempt_id"] == "attempt-1"
    assert evidence["fence"] == 1
    assert evidence["child_boundary"] == "subprocess"
    assert evidence["returncode"] == 0
    assert isinstance(evidence["process_id"], int) and evidence["process_id"] > 0
    assert outputs[0]["name"] == "answer"
    assert set(outputs[0]) <= {
        "name", "filename", "kind", "digest", "media_type", "size", "data_base64",
        "role", "is_primary", "ordinal",
    }
    assert outputs[0]["ordinal"] == 0
    assert "path" not in outputs[0]
    assert "artifact_type" not in outputs[0]
    assert outputs[0]["digest"]
    assert "content_base64" not in outputs[0]
    result = runtime.settlements[0][2]["result"]
    assert result["adapter_family"] == "cpu"
    assert result["capability_digest"] == host.capabilities["test.echo"].capability_digest
    assert result["source_digest"] == host.capabilities["test.echo"].source_digest
    assert result["dependency_digest"] == host.capabilities["test.echo"].dependency_digest
    assert result["process_evidence"]["child_boundary"] == "subprocess"
    assert result["process_evidence"]["returncode"] == 0
    assert isinstance(result["process_evidence"]["process_id"], int)
    assert not list(tmp_path.glob("astrid-attempt-*"))


def test_terminal_sidecar_progress_is_heartbeated_before_settlement(tmp_path):
    manifest_path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["command"]["argv"] = [
        "{python_exec}",
        "-c",
        "import os; from pathlib import Path; Path('{out}/answer.txt').write_text('ok'); Path(os.environ['ASTRID_PROGRESS_PATH']).write_text(__import__('json').dumps(dict(phase='complete', percent=100)))",
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    host.register()
    task = {
        "task": {
            "id": "task-terminal-progress",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-terminal-progress",
            "fence": 1,
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-terminal-progress"] = task

    settled = host.run_task(task, lease_token="lease-terminal-progress")

    assert settled["task"]["status"] == "completed"
    assert runtime.heartbeat_progress == [
        ("task-terminal-progress", {"phase": "complete", "percent": 100})
    ]


def test_run_does_not_require_a_filesystem_free_space_floor(tmp_path, monkeypatch):
    _write_manifest(tmp_path / "echo")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    host.register()
    task = {
        "task": {
            "id": "task-low-space",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-low-space",
            "fence": 1,
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-low-space"] = task

    monkeypatch.setattr(
        "astrid.core.execution.generic_host.shutil.disk_usage",
        lambda _path: pytest.fail("the removed scratch-floor guard was invoked"),
    )
    settled = host.run_task(task, lease_token="lease-low-space")

    assert settled["task"]["status"] == "completed"
    guard_receipt = runtime.settlements[0][2]["result"]["execution_guards"]
    assert "scratch" not in guard_receipt
    assert guard_receipt["evidence"] is not None
    assert guard_receipt["deadline_seconds"] == 3600.0


def test_structure_pack_members_survive_harvest_cleanup_and_reopen(tmp_path):
    root = tmp_path / "timeline"
    root.mkdir()
    (root / "executor.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "test.timeline",
                "name": "Timeline",
                "kind": "external",
                "version": "1.0",
                "command": {
                    "argv": [
                        "{python_exec}",
                        "-c",
                        (
                            "from pathlib import Path; import json, hashlib; "
                            "from astrid.core._shared.result_manifest import build_manifest, write_manifest; "
                            "out=Path('{out}'); pack=out/'agent-view'; pack.mkdir(parents=True, exist_ok=True); "
                            "md=pack/'structure.md'; md.write_text('# structure\\n', encoding='utf-8'); "
                            "idx=pack/'transcript-index.json'; idx.write_text(json.dumps({'speech': [{'text': 'We stay with the ending.'}]}), encoding='utf-8'); "
                            "png=pack/'PG001.png'; png.write_bytes(b'PNG fixture bytes'); "
                            "inner=build_manifest(kind='timeline_visualization', inputs={}, created='t', outputs=["
                            "{'name':'structure','path':'structure.md'},"
                            "{'name':'transcript_index','path':'transcript-index.json'},"
                            "{'name':'page','path':'PG001.png'}]); "
                            "write_manifest(pack/'manifest.json', inner); "
                            "write_manifest(out/'manifest.json', build_manifest(kind='timeline_visualization_result', inputs={}, created='t', outputs=["
                            "{'name':'pack_root','path':'agent-view','role':'auxiliary'},"
                            "{'name':'manifest_path','path':'agent-view/manifest.json','role':'result','is_primary':True}]))"
                        ),
                    ]
                },
                "outputs": [
                    {
                        "name": "pack_root",
                        "type": "directory",
                        "path_template": "{out}/agent-view",
                        "artifact_type": "evidence/timeline-visualization",
                    },
                    {
                        "name": "manifest_path",
                        "type": "file",
                        "path_template": "{out}/agent-view/manifest.json",
                        "artifact_type": "metadata/result-manifest",
                    },
                ],
                "metadata": {"output_result_manifest": True},
            }
        ),
        encoding="utf-8",
    )
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    task = {
        "task": {
            "id": "task-timeline",
            "capability": "test.timeline",
            "project_id": "demo",
            "attempt_id": "attempt-timeline",
            "fence": 1,
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-timeline"] = task

    host.run_task(task, lease_token="lease-1")

    settled = runtime.settlements[0][2]
    outputs = settled["outputs"]
    filenames = [
        item["filename"]
        for item in runtime.uploaded_objects.values()
        if item["filename"] is not None
    ]
    assert "structure.md" in filenames
    assert "transcript-index.json" in filenames
    assert "PG001.png" in filenames
    assert all(not filename.startswith("agent-view/") for filename in filenames)
    assert any(
        item["name"] == "manifest_path"
        and item["filename"] == "manifest.json"
        for item in outputs
    )
    assert len({item["digest"] for item in outputs}) == len(outputs)
    assert not list(tmp_path.glob("astrid-attempt-*"))

    # Reopen the returned managed product from its runtime-owned bytes. This
    # is the post-cleanup path the frozen viewer uses; no attempt-local file is
    # consulted.
    reopened = tmp_path / "reopened"
    for item in runtime.uploaded_objects.values():
        filename = item["filename"]
        assert isinstance(filename, str)
        relative = Path(filename.removeprefix("agent-view/"))
        assert not relative.is_absolute() and ".." not in relative.parts
        destination = reopened / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item["data"])
    assert (reopened / "structure.md").read_text(encoding="utf-8") == "# structure\n"
    assert json.loads((reopened / "transcript-index.json").read_text(encoding="utf-8"))["speech"][0]["text"] == "We stay with the ending."
    assert (reopened / "PG001.png").read_bytes() == b"PNG fixture bytes"


def test_mid_render_evidence_abort_keeps_measurement_on_runtime_failure(tmp_path):
    manifest_path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["command"]["argv"] = [
        "{python_exec}",
        "-c",
        "from pathlib import Path; import time; Path('{out}/answer.txt').write_text('ok'); time.sleep(2)",
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(
        pack_roots=[tmp_path],
        client=runtime,
        execution_policy=ExecutionGuardPolicy(
            evidence_cap_bytes=1,
            deadline_seconds=5,
        ),
    )
    host.discover()
    task = {
        "task": {
            "id": "task-evidence-abort",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-evidence-abort",
            "fence": 1,
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-evidence-abort"] = task

    with pytest.raises(HostError, match="generated evidence cap exceeded"):
        host.run_task(task, lease_token="lease-evidence-abort")

    assert runtime.settlements == []
    assert len(runtime.failures) == 1
    diagnostic = runtime.failures[0][3]["failure_diagnostic"]
    assert diagnostic["category"] == "attempt_cap_exceeded"
    assert diagnostic["attempt_observed_bytes"] == 2
    assert diagnostic["cap_bytes"] == 1
    assert diagnostic["observed_bytes"] == 2
    assert diagnostic["configured_cap_bytes"] == 1
    assert diagnostic["largest_paths"] == [
        {"path": "outputs/answer.txt", "bytes": 2, "classification": "generated"}
    ]
    assert diagnostic["source_digest"] == host.capabilities["test.echo"].source_digest
    assert not list(tmp_path.glob("astrid-attempt-*"))


def _confine_storage_fixture(host, monkeypatch):
    """Use the real source/bootstrap fence in these synthetic command tests."""
    original = host._allocate_attempt_root
    factory = generic_host._AttemptPolicyFactory(host._executable_admission)
    def allocate(task_id, attempt_id):
        root = original(task_id, attempt_id)
        host._confinement_policy = factory.build(
            generic_host._AuthenticatedAttempt((("project_id", "demo"),)),
            attempt=root, record=host.capabilities["test.echo"])
        return root
    monkeypatch.setattr(host, "_allocate_attempt_root", allocate)


def test_explicit_task_storage_envelope_rejects_output_overrun_and_cleans_up(tmp_path, monkeypatch):
    _write_manifest(tmp_path / "echo")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    _confine_storage_fixture(host, monkeypatch)
    task = {
        "task": {
            "id": "task-storage-overrun",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-storage-overrun",
            "fence": 1,
            "storage_estimate": {"scratch_bytes": 1024, "output_bytes": 1},
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-storage-overrun"] = task
    with pytest.raises(HostError, match="output bytes 2 exceed task output limit 1"):
        host.run_task(task, lease_token="lease-storage-overrun")
    assert runtime.settlements == []
    assert runtime.failures and "output bytes 2" in runtime.failures[0][2]
    assert not list(tmp_path.glob("astrid-attempt-*"))
    assert host.last_cleanup_receipt["observed_absent"] is True


def test_live_storage_overrun_preserves_bounded_scratch_diagnostic_before_cleanup(tmp_path, monkeypatch):
    manifest_path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["command"]["argv"] = [
        "{python_exec}",
        "-c",
        (
            "from pathlib import Path; import time; "
            "root=Path('{out}'); "
            "(root/'.remotion-runtime-fixture').mkdir(); "
            "(root/'.remotion-runtime-fixture'/'frame.bin.tmp').write_bytes(b'x'*80); "
            "time.sleep(2)"
        ),
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    _confine_storage_fixture(host, monkeypatch)
    original_materialize = host._materialize_inputs
    def materialize(spec, root, **kwargs):
        values = original_materialize(spec, root, **kwargs)
        # Managed inputs are host-staged read-only data, never child writes.
        managed = root / "managed-objects"
        managed.mkdir(exist_ok=True)
        (managed / "source.bin").write_bytes(b"x" * 7)
        return values
    monkeypatch.setattr(host, "_materialize_inputs", materialize)
    task = {
        "task": {
            "id": "task-storage-scratch-diagnostic",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-storage-scratch-diagnostic",
            "fence": 1,
            "storage_estimate": {"scratch_bytes": 10, "output_bytes": 1},
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-storage-scratch-diagnostic"] = task

    with pytest.raises(HostError, match="scratch bytes"):
        host.run_task(task, lease_token="lease-storage-scratch-diagnostic")

    assert runtime.settlements == []
    assert runtime.failures and "scratch bytes" in runtime.failures[0][2]
    diagnostic = runtime.failures[0][3]["failure_diagnostic"]
    assert diagnostic["guard"] == "storage_envelope"
    assert diagnostic["category"] == "scratch_overrun"
    assert diagnostic["configured_scratch_bytes"] == 10
    assert diagnostic["observed_scratch_bytes"] >= 87
    assert diagnostic["path_classes"]["managed_inputs"] == {"files": 1, "bytes": 7}
    assert diagnostic["path_classes"]["renderer_workspace"] == {"files": 1, "bytes": 80}
    assert diagnostic["largest_scratch_paths"][0] == {
        "path": "outputs/.remotion-runtime-fixture/frame.bin.tmp",
        "bytes": 80,
        "classification": "renderer_workspace",
    }
    assert not list(tmp_path.glob("astrid-attempt-*"))
    assert host.last_cleanup_receipt["observed_absent"] is True


def test_live_storage_envelope_charges_atomic_temps_to_scratch(tmp_path):
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    (output / "image.png").write_bytes(b"12345")
    (output / ".image.png.temporary.png.tmp").write_bytes(b"x" * 80)

    _assert_live_storage_envelope(
        {"scratch_bytes": 80, "output_bytes": 5},
        attempt,
        output,
    )


def test_live_storage_envelope_charges_render_workspace_to_scratch(tmp_path):
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    render_workspace = attempt / ".video.mp4.render-service-fixture"
    output.mkdir(parents=True)
    (render_workspace / "outputs").mkdir(parents=True)
    (output / "video.mp4").write_bytes(b"12345")
    (render_workspace / "outputs" / "element-0001.jpeg").write_bytes(b"x" * 80)

    _assert_live_storage_envelope(
        {"scratch_bytes": 80, "output_bytes": 5},
        attempt,
        output,
    )


@pytest.mark.parametrize("counter", (_attempt_tree_bytes, _storage_tree_bytes))
def test_live_storage_counters_tolerate_a_file_vanishing_during_scan(
    tmp_path, monkeypatch, counter
):
    root = tmp_path / "attempt"
    root.mkdir()
    vanished = root / "element-2744.jpeg"
    retained = root / "element-2745.jpeg"
    vanished.write_bytes(b"gone")
    retained.write_bytes(b"kept")

    original_stat = Path.stat

    def stat_without_vanished(path, *args, **kwargs):
        if path == vanished:
            vanished.unlink(missing_ok=True)
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_without_vanished)
    assert counter(root) == len(b"kept")


@pytest.mark.parametrize("counter", (_attempt_tree_bytes, _storage_tree_bytes))
def test_live_storage_counters_tolerate_a_directory_vanishing_during_scan(
    tmp_path, monkeypatch, counter
):
    root = tmp_path / "attempt"
    root.mkdir()
    retained = root / "retained.bin"
    retained.write_bytes(b"kept")
    vanished_directory = root / ".render-service-fixture"
    vanished_directory.mkdir()

    original_rglob = Path.rglob

    def rglob_without_vanished_directory(path, pattern):
        if path != root:
            return original_rglob(path, pattern)

        def entries():
            yield retained
            raise FileNotFoundError(vanished_directory)

        return entries()

    monkeypatch.setattr(Path, "rglob", rglob_without_vanished_directory)
    assert counter(root) == len(b"kept")


def test_final_storage_envelope_counts_published_temps_and_rejects_escape(tmp_path):
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    published_temp = output / "published.png.tmp"
    published_temp.write_bytes(b"x" * 10)
    with pytest.raises(HostError, match="output bytes 10 exceed task output limit 0"):
        _task_storage_envelope(
            {"storage_estimate": {"scratch_bytes": 10, "output_bytes": 0}},
            attempt,
            [{"path": str(published_temp), "name": "generated_images"}],
        )

    outside = attempt / "outside.png"
    outside.write_bytes(b"x")
    with pytest.raises(HostError, match="escapes the output directory"):
        _task_storage_envelope(
            {"storage_estimate": {"scratch_bytes": 10, "output_bytes": 10}},
            attempt,
            [{"path": str(outside), "name": "generated_images"}],
        )


def test_final_storage_envelope_counts_stable_and_published_paths_as_a_union(tmp_path):
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    stable = output / "image.png"
    published_temp = output / "image.png.tmp"
    stable.write_bytes(b"x" * 6)
    published_temp.write_bytes(b"y" * 6)

    with pytest.raises(HostError, match="output bytes 12 exceed task output limit 10"):
        _task_storage_envelope(
            {"storage_estimate": {"scratch_bytes": 10, "output_bytes": 10}},
            attempt,
            [{"path": str(published_temp), "name": "generated_images"}],
        )


def test_required_storage_admission_failure_is_terminal_and_not_dispatched(tmp_path):
    manifest_path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata"].update({
        "storage_estimate_required": True,
        "storage_estimate_exact": True,
        "estimated_scratch_bytes": 7,
        "estimated_output_bytes": 11,
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    task = {
        "task": {
            "id": "task-invalid-storage-admission",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-invalid-storage-admission",
            "fence": 1,
            "storage_estimate": {"scratch_bytes": 7, "output_bytes": 12},
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-invalid-storage-admission"] = task
    with pytest.raises(HostError, match="requires storage_estimate"):
        host.run_task(task, lease_token="lease-invalid-storage-admission")
    assert runtime.settlements == []
    assert runtime.failures
    assert runtime.failures[0][3]["retryable"] is False
    assert not list(tmp_path.glob("astrid-attempt-*"))


def test_fixed_request_scope_rejects_conflicting_task_parameters(tmp_path):
    manifest_path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"] = [
        {"name": "model", "type": "string", "required": True},
        {"name": "mode", "type": "string", "required": True},
        {"name": "execution", "type": "string", "required": True},
    ]
    manifest["metadata"]["fixed_inputs"] = {"model": "z-image", "mode": "i2i", "execution": "cloud"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    task = {
        "task": {
            "id": "task-fixed-scope",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-fixed-scope",
            "fence": 1,
            "spec": {
                "spec": {
                    "params": {"model": "other-model", "mode": "i2i", "execution": "cloud"},
                    "inputs": {},
                }
            },
        }
    }
    runtime.tasks["task-fixed-scope"] = task
    with pytest.raises(HostError, match="request escapes fixed scope"):
        host.run_task(task, lease_token="lease-fixed-scope")
    assert runtime.failures and runtime.failures[0][3]["retryable"] is False


def test_fixed_request_scope_rejects_unsupported_legacy_inputs(tmp_path):
    manifest_path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"] = [
        {"name": "model", "type": "string", "required": True},
        {"name": "mode", "type": "string", "required": True},
        {"name": "execution", "type": "string", "required": True},
    ]
    manifest["metadata"]["fixed_inputs"] = {
        "model": "qwen-image-edit-2511",
        "mode": "edit",
        "execution": "cloud",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    task = {
        "task": {
            "id": "task-fixed-legacy-input",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-fixed-legacy-input",
            "fence": 1,
            "spec": {
                "spec": {
                    "params": {
                        "model": "qwen-image-edit-2511",
                        "mode": "edit",
                        "execution": "cloud",
                    },
                    "inputs": {"mask_ref": "/tmp/mask.png"},
                }
            },
        }
    }
    runtime.tasks["task-fixed-legacy-input"] = task
    with pytest.raises(HostError, match="unsupported legacy input"):
        host.run_task(task, lease_token="lease-fixed-legacy-input")
    assert runtime.failures and runtime.failures[0][3]["retryable"] is False


def test_completed_process_evidence_reads_settlement_payload_when_result_omits_identity():
    evidence = _completed_process_evidence(
        capability_id="wan2gp.generate_video",
        attempt_id="attempt-1",
        fence=1,
        result=object(),
        payload={"process_id": 257745, "returncode": 0},
    )
    assert evidence["process_id"] == 257745
    assert evidence["returncode"] == 0
    assert evidence["child_boundary"] == "subprocess"


def test_completed_process_evidence_fails_closed_without_returncode():
    with pytest.raises(HostError, match="missing process evidence returncode"):
        _completed_process_evidence(
            capability_id="wan2gp.generate_video",
            attempt_id="attempt-1",
            fence=1,
            result=object(),
            payload={"process_id": 257745},
        )


def test_unready_capability_is_not_dispatched(tmp_path, monkeypatch):
    _write_manifest(tmp_path / "echo")
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "empty-source-state.json"))
    monkeypatch.setenv("PATH", "")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    host.preflight()
    # python_exec is resolved by the runner; with PATH empty the source still
    # remains a valid manifest and readiness is determined by its declaration.
    assert host.capabilities["test.echo"].ready


def test_optional_capability_still_requires_storage_admission(tmp_path):
    manifest_path = _write_manifest(
        tmp_path / "media",
        capability_id="vibecomfy.video_enhance",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata"].update(
        {
            "storage_estimate_required": True,
            "storage_estimate_exact": True,
            "estimated_scratch_bytes": 7,
            "estimated_output_bytes": 11,
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    matrix = tmp_path / "matrix.json"
    matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capabilities": [
                    {
                        "id": "vibecomfy.video_enhance",
                        "disposition": "optional",
                        "evidence_reason": "bounded test capability",
                        "adapter_family": "local_generation",
                        "resource_keys": ["gpu"],
                        "required_packages": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = FakeRuntime()
    host = GenericPackHost(
        pack_roots=[tmp_path],
        capability_matrix=matrix,
        client=runtime,
    )
    host.discover()

    task = {
        "id": "task-optional-media",
        "capability": "vibecomfy.video_enhance",
        "attempt_id": "attempt-optional-media",
        "fence": 1,
    }
    with pytest.raises(HostError, match="requires a whole-task storage_estimate"):
        host.run_task(task, lease_token="lease-optional-media")

    assert runtime.settlements == []
    assert runtime.failures
    assert runtime.failures[0][3]["retryable"] is False


def test_claim_loop_fails_explicitly_without_canonical_claim_operation(tmp_path):
    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path], client=object())
    host.discover()
    with pytest.raises(HostError, match="canonical claim-next operation"):
        host.run(once=True)


def test_long_running_claim_loop_survives_and_backs_off_after_runtime_failure(
    tmp_path, monkeypatch, capsys
):
    """One failed queue poll must not terminate the registered pack host."""

    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path], client=FakeRuntime())
    host.discover()
    calls = 0
    waits: list[float] = []

    def claim_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("coordinator temporarily unavailable")
        host._shutdown.set()
        return None

    def wait(delay: float) -> bool:
        waits.append(delay)
        return False

    monkeypatch.setattr(host, "claim_once", claim_once)
    monkeypatch.setattr(host._shutdown, "wait", wait)

    assert host.run(poll_seconds=0.25) == []
    assert calls == 2
    assert waits == [0.25]
    assert "generic host claim failed (1 consecutive)" in capsys.readouterr().err


def test_adapter_registry_classifies_provider_local_generation_and_render():
    packs = Path(generic_host.__file__).resolve().parents[2] / "packs"
    provider = GenericPackHost(pack_roots=[packs / "generation"])
    provider.discover()
    assert AdapterRegistry.resolve(provider.capabilities["generation.generate_image_openai"].definition).family == "provider"
    local = GenericPackHost(pack_roots=[packs / "vibecomfy"])
    local.discover()
    assert AdapterRegistry.resolve(local.capabilities["vibecomfy.run"].definition).family == "local_generation"
    local.preflight("vibecomfy.run")
    assert local.capabilities["vibecomfy.run"].resource_keys == ("gpu",)
    render = GenericPackHost(pack_roots=[packs / "rendering"])
    render.discover()
    assert AdapterRegistry.resolve(render.capabilities["rendering.render"].definition).family == "render"
    render.preflight("rendering.render")
    report = render.capabilities["rendering.render"].preflight
    if not report["binaries"]["ok"]:
        assert "ffmpeg" in report["binaries"]["missing"]
    assert "remotion" in report
    assert render.capabilities["rendering.render"].estimated_scratch_bytes is None
    assert render.capabilities["rendering.render"].estimated_output_bytes is None


def test_render_preflight_requires_the_explicit_execution_runtime(monkeypatch):
    monkeypatch.delenv("ASTRID_REMOTION_PROJECT_DIR", raising=False)
    monkeypatch.delenv("ASTRID_NODE_EXECUTABLE", raising=False)
    monkeypatch.delenv("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", raising=False)
    # V3 exports render from the pack declaration, not an executor folder.
    pack_root = Path(generic_host.__file__).resolve().parents[2] / "packs" / "rendering"
    host = GenericPackHost(pack_roots=[pack_root])
    host.discover()
    assert host.capabilities["rendering.render"].manifest_path == pack_root / "pack.yaml"
    assert host.capabilities["rendering.render"].adapter.requires_remotion

    host.preflight("rendering.render")

    record = host.capabilities["rendering.render"]
    assert not record.ready
    assert not record.preflight["remotion"]["ok"]
    assert "ASTRID_REMOTION_PROJECT_DIR" in record.preflight["remotion"]["reason"]


@pytest.fixture
def builtin_v3_render_command():
    pack_root = Path(generic_host.__file__).resolve().parents[2] / "packs" / "rendering"
    host = GenericPackHost(pack_roots=[pack_root])
    host.discover()
    record = host.capabilities["rendering.render"]
    definition, admission = host.admit("executor", "rendering.render")
    return host, record, definition, dict(admission)


def test_v3_render_command_selects_host_command_bridge_from_declared_descriptor(
    builtin_v3_render_command, tmp_path, monkeypatch
):
    from io import BytesIO

    from astrid.core.execution import render_command_pipe

    host, record, definition, admission = builtin_v3_render_command
    assert definition["command"]["argv"][1:3] == [
        "-m",
        "astrid.packs.rendering.actions.render.run",
    ]
    assert record.definition.metadata["runtime_module"] == definition["command"]["argv"][2]

    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    timeline = tmp_path / "timeline.json"
    timeline.write_text('{"tracks": [], "clips": []}', encoding="utf-8")
    policy = object()
    monkeypatch.setattr(host, "_required_confinement_policy", lambda _attempt: policy)
    monkeypatch.setattr(
        host,
        "_child_environment",
        lambda *args, **kwargs: ({"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}, {}),
    )
    monkeypatch.setattr(generic_host, "_assert_live_storage_envelope", lambda *args: None)
    monkeypatch.setattr(generic_host, "harvest_staged_outputs", lambda *args, **kwargs: [])
    monkeypatch.setattr(generic_host, "observe_tree", lambda _process: None)
    monkeypatch.setattr(generic_host, "_terminate_process_group", lambda _process: None)

    class Process:
        pid = 48120
        returncode = 0

        def __init__(self):
            self.stdin = BytesIO()
            self.stdout = BytesIO()
            self.stderr = BytesIO()

        def poll(self):
            return self.returncode

    process = Process()
    launched = {}

    def plan(argv, *, attempt, cwd, environment, **kwargs):
        launched["argv"] = list(argv)
        return SimpleNamespace(
            argv=tuple(argv),
            cwd=cwd,
            environment=tuple(sorted(environment.items())),
            subprocess_options=lambda: {},
        )

    def popen(argv, **kwargs):
        launched["popen_argv"] = list(argv)
        return process

    class ReachedRenderCommandBridge(Exception):
        pass

    def select_existing_bridge(selected_process, **kwargs):
        # Stop at the real bridge boundary; this selector fixture returns no
        # protocol response and never substitutes for a renderer owner.
        launched["bridge_process"] = selected_process
        launched["bridge_kwargs"] = kwargs
        raise ReachedRenderCommandBridge

    monkeypatch.setattr(host, "_workload_launch_plan", plan)
    monkeypatch.setattr(generic_host, "popen_owned_group", popen)
    monkeypatch.setattr(render_command_pipe, "HostRenderCommandBridge", select_existing_bridge)

    with pytest.raises(ReachedRenderCommandBridge):
        host._run_command_definition(
            record,
            {"timeline": str(timeline)},
            output_root,
            attempt,
            admission=admission,
        )

    launch_argv = launched["popen_argv"]
    assert launch_argv[:5] == [sys.executable, "-I", "-S", "-B", "-c"]
    assert "install_render_command_client" in launch_argv[5]
    assert "sys.path.extend(json.loads(sys.argv[1]))" in launch_argv[5]
    assert launch_argv[7:9] == [definition["command"]["argv"][2], "--timeline"]
    assert launch_argv[9] == str(timeline)
    assert "--renderer-command-stdio" not in launch_argv
    bridge_kwargs = launched["bridge_kwargs"]
    assert launched["bridge_process"] is process
    assert bridge_kwargs["policy"] is policy
    assert bridge_kwargs["backend_root"] == record.source_root
    assert bridge_kwargs["admission"] == {**admission, "capability_id": "rendering.render"}


def test_v3_render_command_refuses_a_mismatched_admitted_source_before_launch(
    builtin_v3_render_command, tmp_path, monkeypatch
):
    host, record, _definition, admission = builtin_v3_render_command
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    timeline = tmp_path / "timeline.json"
    timeline.write_text('{"tracks": [], "clips": []}', encoding="utf-8")
    mismatched_admission = {**admission, "source_root": str(tmp_path / "other-pack")}
    monkeypatch.setattr(
        generic_host,
        "popen_owned_group",
        lambda *args, **kwargs: pytest.fail("mismatched source reached process launch"),
    )

    with pytest.raises(HostError, match="exact admitted source and capability"):
        host._run_command_definition(
            record,
            {"timeline": str(timeline)},
            output_root,
            attempt,
            admission=mismatched_admission,
        )


def test_adapter_registry_preserves_explicit_empty_matrix_lists(tmp_path):
    _write_manifest(tmp_path / "echo")
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    adapter = AdapterRegistry.from_matrix(
        record.definition,
        {
            "adapter_family": "render",
            "resource_keys": [],
            "required_binaries": [],
            "required_packages": [],
        },
    )
    assert adapter.resource_keys == ()
    assert adapter.required_binaries == ()
    assert adapter.required_packages == ()


def test_register_preserves_declared_dispositions_and_block_reasons(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRID_TEST_PROVIDER_KEY", raising=False)
    records = [
        ("required.provider", "required", "Provider credential is required"),
        ("optional.provider", "optional", "Optional provider credential"),
        ("unsupported.provider", "unsupported", "Provider is not shipped"),
        ("retired.provider", "retired", "Provider was retired"),
    ]
    capabilities = []
    for capability_id, disposition, evidence_reason in records:
        root = tmp_path / capability_id.replace(".", "-")
        root.mkdir()
        (root / "executor.yaml").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": capability_id,
                    "name": capability_id,
                    "kind": "external",
                    "version": "1.0",
                    "command": {"argv": ["{python_exec}", "-c", "pass"]},
                    "outputs": [],
                    "isolation": {"mode": "subprocess", "network": True},
                    "metadata": {"adapter_family": "provider"},
                }
            ),
            encoding="utf-8",
        )
        capabilities.append(
            {
                "id": capability_id,
                "disposition": disposition,
                "evidence_reason": evidence_reason,
                "adapter_family": "provider",
                "required_env": (["ASTRID_TEST_PROVIDER_KEY"] if disposition in {"required", "optional"} else []),
                "required_binaries": [],
                "required_packages": [],
            }
        )
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps({"schema_version": 1, "capabilities": capabilities}), encoding="utf-8")

    class CaptureRuntime(RuntimeProtocolClient):
        schema_digest = FakeRuntime.schema_digest

        def __init__(self):
            self.registration_payload = None

        def health(self):
            return {
                "status": "ok",
                "protocol": "workspace.v1",
                "schema_digest": self.schema_digest,
                "runtime_epoch": 1,
                "instance_id": "fixture-runtime-instance",
                "runtime_instance_id": "fixture-runtime-instance",
                "coordinator_epoch": "fixture-runtime-instance",
            }

        def register_executor(self, executor_id, **payload):
            self.registration_payload = payload
            return {"executor_id": executor_id, **payload}

    runtime = CaptureRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], capability_matrix=matrix, client=runtime)
    host.discover()
    host.register()
    assert runtime.registration_payload is not None
    registered = {
        item["capability_id"]: item
        for item in runtime.registration_payload["capabilities"]
    }
    assert registered["required.provider"]["status"] == "unavailable"
    unavailable_reason = registered["required.provider"]["unavailable_reason"]
    assert unavailable_reason
    reason_by_check = {
        component.split(":", 1)[0]: component
        for component in unavailable_reason.split(";")
    }
    assert reason_by_check["credentials"] == "credentials:missing=ASTRID_TEST_PROVIDER_KEY"
    assert reason_by_check["network"] == "network:reason=provider network_policy is missing"
    assert registered["optional.provider"]["status"] == "unavailable"
    assert registered["unsupported.provider"]["status"] == "unsupported"
    assert registered["unsupported.provider"]["unavailable_reason"] == "Provider is not shipped"
    assert registered["retired.provider"]["status"] == "retired"
    assert registered["retired.provider"]["unavailable_reason"] == "Provider was retired"


def test_command_host_harvests_result_manifest_media(tmp_path: Path) -> None:
    root = tmp_path / "wanlike"
    root.mkdir()
    (root / "executor.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "test.generate",
                "name": "Generate",
                "kind": "external",
                "version": "1.0",
                "command": {
                    "argv": [
                        "{python_exec}",
                        "-c",
                        (
                            "from pathlib import Path; import json, hashlib; "
                            "out=Path('{out}'); out.mkdir(parents=True, exist_ok=True); "
                            "a=out/'a.mp4'; a.write_bytes(b'a'); "
                            "b=out/'b.mp4'; b.write_bytes(b'bb'); "
                            "(out/'manifest.json').write_text(json.dumps({"
                            "'schema_version':1,'kind':'video','inputs':{},"
                            "'outputs':["
                            "{'path':'a.mp4','name':'generated_videos','ordinal':0,'role':'result',"
                            "'is_primary':True,'content_hash':'sha256:'+hashlib.sha256(a.read_bytes()).hexdigest(),'bytes':1},"
                            "{'path':'b.mp4','name':'generated_videos','ordinal':1,'role':'result',"
                            "'content_hash':'sha256:'+hashlib.sha256(b.read_bytes()).hexdigest(),'bytes':2}],"
                            "'created':'t','warnings':[]}))"
                        ),
                    ]
                },
                "outputs": [
                    {"name": "generated_videos", "type": "file", "artifact_type": "video/clip"},
                    {"name": "video_manifest", "type": "file", "path_template": "{out}/manifest.json"},
                ],
                "metadata": {"output_result_manifest": True},
            }
        ),
        encoding="utf-8",
    )
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    result = host._run_command_definition(
        host.capabilities["test.generate"], {}, output_root, attempt
    )
    assert [item["name"] for item in result.outputs] == [
        "generated_videos",
        "generated_videos",
    ]
    assert [item["ordinal"] for item in result.outputs] == [0, 1]
    assert [Path(item["path"]).name for item in result.outputs] == ["a.mp4", "b.mp4"]

    runtime = FakeRuntime()
    publishing_host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    publishing_host.discover()
    task = {
        "task": {
            "id": "task-collection",
            "capability": "test.generate",
            "project_id": "demo",
            "attempt_id": "attempt-collection",
            "fence": 1,
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-collection"] = task
    publishing_host.run_task(task, lease_token="lease-1")
    settled = runtime.settlements[0][2]["outputs"]
    assert [item["name"] for item in settled] == [
        "generated_videos",
        "generated_videos",
    ]
    assert all(
        set(item)
        <= {
            "name", "kind", "filename", "digest", "media_type", "size", "data_base64",
            "role", "is_primary", "ordinal",
        }
        for item in settled
    )
    # Declared ordinals are each row's identity on the port (generated_videos#0, #1);
    # without them the Runtime files both rows under #0.
    assert [item["ordinal"] for item in settled] == [0, 1]
    assert [item["filename"] for item in settled] == ["a.mp4", "b.mp4"]
    assert all("path" not in item and "artifact_type" not in item for item in settled)


def test_command_host_fail_closes_success_without_media(tmp_path: Path) -> None:
    root = tmp_path / "emptygen"
    root.mkdir()
    (root / "executor.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "test.emptygen",
                "name": "Empty",
                "kind": "external",
                "version": "1.0",
                "command": {
                    "argv": [
                        "{python_exec}",
                        "-c",
                        (
                            "from pathlib import Path; import json; "
                            "out=Path('{out}'); out.mkdir(parents=True, exist_ok=True); "
                            "(out/'manifest.json').write_text(json.dumps({"
                            "'schema_version':1,'kind':'video','inputs':{},'outputs':[],"
                            "'created':'t','warnings':[]}))"
                        ),
                    ]
                },
                "outputs": [
                    {"name": "generated_videos", "type": "file", "artifact_type": "video/clip"},
                ],
                "metadata": {"output_result_manifest": True},
            }
        ),
        encoding="utf-8",
    )
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    task = {
        "task": {
            "id": "task-empty",
            "capability": "test.emptygen",
            "project_id": "demo",
            "attempt_id": "attempt-empty",
            "fence": 1,
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-empty"] = task
    with pytest.raises(HostError, match="no concrete outputs|no result files"):
        host.run_task(task, lease_token="lease-1")
    assert runtime.settlements == []


def test_command_host_rejects_media_receipt_with_wrong_port_name(tmp_path: Path) -> None:
    root = tmp_path / "wrong-port"
    root.mkdir()
    (root / "executor.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "test.wrong-port",
                "name": "Wrong port",
                "kind": "external",
                "version": "1.0",
                "command": {
                    "argv": [
                        "{python_exec}",
                        "-c",
                        (
                            "from pathlib import Path; import json, hashlib; "
                            "out=Path('{out}'); out.mkdir(parents=True, exist_ok=True); "
                            "clip=out/'clip.mp4'; clip.write_bytes(b'mp4'); "
                            "(out/'manifest.json').write_text(json.dumps({"
                            "'schema_version':1,'kind':'video','inputs':{},"
                            "'outputs':[{'path':'clip.mp4','name':'wrong_port','ordinal':0,"
                            "'role':'result','content_hash':'sha256:'+hashlib.sha256(clip.read_bytes()).hexdigest(),"
                            "'bytes':3}],'created':'t','warnings':[]}))"
                        ),
                    ]
                },
                "outputs": [
                    {
                        "name": "generated_videos",
                        "type": "file",
                        "artifact_type": "video/clip",
                    }
                ],
                "metadata": {"output_result_manifest": True},
            }
        ),
        encoding="utf-8",
    )
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    task = {
        "task": {
            "id": "task-wrong-port",
            "capability": "test.wrong-port",
            "project_id": "demo",
            "attempt_id": "attempt-wrong-port",
            "fence": 1,
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-wrong-port"] = task
    with pytest.raises(HostError, match="undeclared port|declared output port"):
        host.run_task(task, lease_token="lease-1")
    assert runtime.settlements == []


def _local_custody_request(tmp_path):
    import hashlib
    root = tmp_path / "out" / "sessions" / "selected"
    config = {"cwd": str(tmp_path), "runtime_root": str(tmp_path), "server_log_path": str(root / "comfy.log"),
              "port": 8188, "locality": "managed_local_server", "warm_policy": "auto", "ready_timeout_sec": 10}
    digest = "sha256:" + "a" * 64
    profile = {"profile_id": "selected", "workspace_uuid": "workspace", "realm_root": str(tmp_path),
               "support_root": str(tmp_path), "machine_id": "machine", "worker_executable": "/selected/relay",
               "host_executable": "/selected/python", "engine_executable": "/selected/python",
               "engine_listener_executable": "/selected/listener", "engine_endpoint": "http://127.0.0.1:8188",
               "worker_artifact_digest": digest, "host_artifact_digest": digest, "engine_artifact_digest": digest,
               "engine_listener_artifact_digest": digest, "session_config_digest": "sha256:" + hashlib.sha256(json.dumps(config, indent=2, sort_keys=True).encode()).hexdigest(),
               "profile_revision": "revision", "profile_digest": digest, "release_digest": digest,
               "engine_launch": {"module": "vibecomfy.commands.session", "session_root": str(root), "config": config,
                                 "source_revision": "selected-vibe", "source_content_digest": digest,
                                 "listener_argv": ["/selected/listener", "serve"],
                                 "adapter_pins": {key: digest for key in ("session_source_sha256", "spawn_sha256", "cleanup_sha256", "stop_sha256", "adapter_source_sha256")}}}
    return {"version": "runtime.local-execution-host/v1", "command": "prepare_local_execution",
            "operation_id": "runtime-op", "channel_id": "private-channel", "owner_epoch": "runtime-A",
            "runtime_owner": {"pid": 101, "uid": 501, "birth_id": "runtime-birth", "runtime_instance_id": "runtime-A", "coordinator_epoch": "runtime-A"},
            "profile": profile, "custody_scope": str(tmp_path / "custody")}


def test_local_preparation_replay_never_launches_twice_and_is_not_activation(tmp_path):
    from astrid.core.execution.generic_host import LocalExecutionPreparation
    calls = []
    class Graph:
        def __init__(self, *, request):
            calls.append(request)
        def prepare(self):
            return {"processes": {}, "engine_binding": {}, "session_config_digest": "measured", "custody_capabilities": {}}
        def abort(self):
            calls.append("verified-clean")
    request = _local_custody_request(tmp_path)
    service = LocalExecutionPreparation(operation_id="runtime-op", channel_id="private-channel", graph_factory=Graph)
    reply = service.prepare_local_execution(request)
    assert service.prepare_local_execution(request) == reply
    assert len(calls) == 1
    assert not {"credential", "claim", "lease_token", "executor_incarnation"} & set(reply)
    changed = json.loads(json.dumps(request)); changed["profile"]["profile_digest"] = "sha256:" + "b" * 64
    with pytest.raises(HostError, match="conflict"):
        service.prepare_local_execution(changed)
    assert len(calls) == 1
    service.abort(); service.abort()
    assert calls[-1] == "verified-clean"
    assert len(calls) == 2


def test_local_partial_spawn_retains_original_failure_and_cleanup_obligation(tmp_path):
    from astrid.core.execution.generic_host import LocalExecutionPreparation
    error = RuntimeError("listener admission failed")
    graphs = []
    class Graph:
        def __init__(self, *, request):
            graphs.append(self); self.cleanup = 0
        def prepare(self):
            raise error
        def abort(self):
            self.cleanup += 1
            raise CleanupUncertainError("unregistered escaped child")
    request = _local_custody_request(tmp_path)
    service = LocalExecutionPreparation(operation_id="runtime-op", channel_id="private-channel", graph_factory=Graph)
    for _ in range(2):
        with pytest.raises(RuntimeError) as raised:
            service.prepare_local_execution(request)
        assert raised.value is error
    assert len(graphs) == 1
    with pytest.raises(CleanupUncertainError):
        service.abort()
    assert service.error is error
    assert not service._aborted
    assert service.graph is graphs[0]


@pytest.mark.parametrize("change", ["owner_epoch", "channel_id", "operation_id"])
def test_local_preparation_rejects_stale_private_binders_before_spawn(tmp_path, change):
    from astrid.core.execution.generic_host import LocalExecutionPreparation
    request = _local_custody_request(tmp_path); request[change] = "foreign"
    service = LocalExecutionPreparation(operation_id="runtime-op", channel_id="private-channel",
                                        graph_factory=lambda **_: pytest.fail("invalid binding spawned"))
    with pytest.raises(HostError):
        service.prepare_local_execution(request)


def test_bound_native_abort_clean_ack_requires_verified_graph_and_replays_exactly(tmp_path):
    from astrid.core.execution.generic_host import LocalExecutionPreparation
    from astrid.core.execution.custody_broker import _canonical, _digest_bytes
    request = _local_custody_request(tmp_path)
    target = {"pid": 201, "uid": 501, "birth_id": "listener", "audit_token_sha256": "sha256:" + "a" * 64, "audit_token_pidversion": 1}
    refs = {role: {"version": "runtime.role-custody-reference/v1", "scope_root": request["custody_scope"], "role": role, "generation": 1, "target": {**target, "pid": 201 if role == "engine_listener" else 200}} for role in ("engine", "engine_listener")}
    calls = []
    class Graph:
        def __init__(self, *, request): pass
        def prepare(self): return {"processes": {}, "engine_binding": {}, "session_config_digest": "measured", "custody_capabilities": refs}
        def known_custody_capabilities(self): return refs
        def abort(self):
            calls.append("whole-graph-verified")
            return {role: {"generation": ref["generation"], "target": ref["target"], "exit_code": 0,
                           "proof_kind": "retained-child-exit" if role == "engine" else "authenticated-retained-child-exit"} for role, ref in refs.items()}
    service = LocalExecutionPreparation(operation_id="runtime-op", channel_id="private-channel", graph_factory=Graph)
    service.prepare_local_execution(request)
    abort = {"version": request["version"], "command": "abort_local_execution", "operation_id": request["operation_id"],
             "channel_id": request["channel_id"], "owner_epoch": request["owner_epoch"], "profile_digest": request["profile"]["profile_digest"],
             "profile_binding_digest": _digest_bytes(_canonical(request["profile"])), "custody_scope": request["custody_scope"], "custody_capabilities": refs}
    reply = service.dispatch(abort)
    assert reply["status"] == "cleaned"
    assert service.dispatch(abort) == reply
    assert calls == ["whole-graph-verified"]
    changed = {**abort, "custody_capabilities": {}}
    with pytest.raises(HostError, match="replay conflicts"):
        service.dispatch(changed)


def test_native_abort_unknown_keeps_all_partial_unreported_obligations(tmp_path):
    from astrid.core.execution.generic_host import LocalExecutionPreparation
    from astrid.core.execution.custody_broker import _canonical, _digest_bytes
    request = _local_custody_request(tmp_path)
    refs = {"engine": {"pending": "retained-escrow"}}
    initiating = RuntimeError("partial listener registration failed")
    class Graph:
        def __init__(self, *, request): pass
        def prepare(self): raise initiating
        def known_custody_capabilities(self): return refs
        def abort(self): raise CleanupUncertainError("late detached child has no retained wait proof")
    service = LocalExecutionPreparation(operation_id="runtime-op", channel_id="private-channel", graph_factory=Graph)
    with pytest.raises(RuntimeError): service.prepare_local_execution(request)
    abort = {"version": request["version"], "command": "abort_local_execution", "operation_id": request["operation_id"], "channel_id": request["channel_id"],
             "owner_epoch": request["owner_epoch"], "profile_digest": request["profile"]["profile_digest"], "profile_binding_digest": _digest_bytes(_canonical(request["profile"])),
             "custody_scope": request["custody_scope"], "custody_capabilities": {}}
    reply = service.dispatch(abort)
    assert reply["status"] == "unresolved" and reply["error_code"] == "cleanup_unresolved"
    assert reply["custody_capabilities"] == refs
    assert service.error is initiating and not service._aborted


def _native_abort_graph(tmp_path, monkeypatch, *, engine_exited=False, engine_timeout=False):
    """Exercise the real native abort orchestration without launching an engine."""
    from astrid.core.execution.custody_broker import CustodyError

    calls, delivered = [], []
    now = [0.0]
    monkeypatch.setattr(generic_host.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(generic_host.time, "sleep", lambda duration: now.__setitem__(0, now[0] + duration))
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("numeric signal fallback"))
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("numeric group signal fallback"))

    class Broker:
        def __init__(self, role, pid):
            self.role, self.pid = role, pid
            self.error, self.state = None, "ready-bound"
            self._post_exec_authority_validated = True
            self.authority_error = None
            identity = {"pid": pid, "uid": 501, "birth_id": role + "-birth"}
            self.registration = {"pid": pid, "identity": identity}
            self.observed = identity
            self.target = {**identity, "audit_token_sha256": "sha256:" + "a" * 64, "audit_token_pidversion": 1}
            self.role_authority = SimpleNamespace(reference=lambda: {"generation": 1, "target": dict(self.target)})
            self.channel = None

        def signal(self, signum, *, expected_pid):
            assert expected_pid == self.pid
            calls.append((self.role, "ordinary", signum))
            if self.authority_error is not None:
                raise self.authority_error
            delivered.append((self.role, signum))

        def signal_failed_admission(self, signum, *, expected_pid):
            assert self.error is not None and self._post_exec_authority_validated
            assert self.registration is not None and expected_pid == self.pid
            calls.append((self.role, "failed-admission", signum))
            if self.authority_error is not None:
                raise self.authority_error
            delivered.append((self.role, signum))

        def _observe_registered_identity(self, pid):
            assert pid == self.pid
            return self.observed

    class Process:
        pid = 200
        returncode = 7 if engine_exited else None
        on_wait = None

        def poll(self):
            return self.returncode

        def wait(self, *, timeout):
            calls.append(("engine", "retained-wait", timeout))
            if engine_timeout:
                raise subprocess.TimeoutExpired("selected-engine", timeout)
            if self.on_wait is not None:
                self.on_wait()
            self.returncode = 7
            return self.returncode

    graph = generic_host._NativeEngineGraph(request={"profile": {
        "engine_launch": {"session_root": str(tmp_path / "out" / "sessions" / "selected")},
        "engine_endpoint": "http://127.0.0.1:8199",
    }})
    graph.engine, graph.listener = Broker("engine", 200), Broker("engine_listener", 201)
    graph.process = Process()
    monkeypatch.setattr(generic_host, "verify_tree_absent", lambda process: calls.append(("graph", "verified", process.pid)))
    monkeypatch.setattr(generic_host.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""))
    return graph, calls, delivered, CustodyError


@pytest.mark.parametrize("state", ["sealed", "ready-bound"])
def test_native_graph_abort_prepared_engine_uses_ordinary_signal(tmp_path, monkeypatch, state):
    graph, calls, delivered, _ = _native_abort_graph(tmp_path, monkeypatch)
    graph.engine.state = state
    def listener_wait_completed():
        graph._listener_exit = {"generation": 1, "target": dict(graph.listener.target), "exit_code": 3}
    graph.process.on_wait = listener_wait_completed
    cleanup = graph.abort()
    assert calls[:2] == [("engine", "ordinary", signal.SIGTERM), ("engine", "retained-wait", 5)]
    assert delivered == [("engine", signal.SIGTERM)]
    assert cleanup["engine"]["exit_code"] == 7
    assert cleanup["engine_listener"]["exit_code"] == 3
    assert cleanup["engine_listener"]["proof_kind"] == "authenticated-retained-child-exit"
    assert graph.abort() == cleanup
    assert delivered == [("engine", signal.SIGTERM)]


def test_native_graph_abort_exited_engine_cleans_listener_but_stays_unresolved(tmp_path, monkeypatch):
    graph, calls, delivered, _ = _native_abort_graph(tmp_path, monkeypatch, engine_exited=True)
    listener = graph.listener
    with pytest.raises(CleanupUncertainError, match="authenticated retained listener wait proof"):
        graph.abort()
    assert delivered == [("engine_listener", signal.SIGTERM), ("engine_listener", signal.SIGKILL)]
    assert calls[0] == ("engine", "retained-wait", 5)
    assert not any(call[0] == "graph" for call in calls)
    assert graph.listener is listener and listener.registration is not None
    assert graph._listener_exit is None and graph._clean_ack is None


@pytest.mark.parametrize("authority", ["missing", "unvalidated", "stale", "transferred"])
def test_native_graph_abort_missing_or_stale_listener_authority_retains_obligations(tmp_path, monkeypatch, authority):
    graph, calls, delivered, CustodyError = _native_abort_graph(tmp_path, monkeypatch, engine_exited=True)
    listener = graph.listener
    if authority == "missing":
        graph.listener = None
    elif authority == "unvalidated":
        listener.error = RuntimeError("registration failed before postexec validation")
        listener.state, listener._post_exec_authority_validated = "accepting", False
    else:
        listener.authority_error = CustodyError("stale generation" if authority == "stale" else "designated actor transferred")
    with pytest.raises(CleanupUncertainError, match="listener cleanup unresolved"):
        graph.abort()
    assert delivered == []
    assert not any(call[1] == "failed-admission" for call in calls)
    assert graph._listener_exit is None and graph._clean_ack is None
    assert listener.registration is not None and graph.process is not None
    assert graph._listener_cleanup_error is not None


@pytest.mark.parametrize("failed_admission", [False, True])
def test_native_graph_abort_timeout_selects_current_listener_signal_path(tmp_path, monkeypatch, failed_admission):
    graph, calls, delivered, _ = _native_abort_graph(tmp_path, monkeypatch, engine_timeout=True)
    if failed_admission:
        graph.listener.error, graph.listener.state = RuntimeError("seal persistence failed"), "accepting"
    with pytest.raises(CleanupUncertainError, match="engine cleanup deadline expired"):
        graph.abort()
    assert calls == [("engine", "ordinary", signal.SIGTERM), ("engine", "retained-wait", 5),
                     ("engine_listener", "failed-admission" if failed_admission else "ordinary", signal.SIGKILL)]
    assert delivered == [("engine", signal.SIGTERM), ("engine_listener", signal.SIGKILL)]
    assert graph._clean_ack is None and graph._listener_exit is None
    assert graph.process.returncode is None and graph.listener.registration is not None


def test_native_graph_abort_failed_admission_uses_validated_path(tmp_path, monkeypatch):
    graph, calls, delivered, _ = _native_abort_graph(tmp_path, monkeypatch)
    initiating = RuntimeError("final seal failed after postexec custody")
    for broker in (graph.engine, graph.listener):
        broker.error, broker.state = initiating, "accepting"
    graph.listener.observed = None
    with pytest.raises(CleanupUncertainError, match="authenticated retained listener wait proof"):
        graph.abort()
    assert calls == [("engine", "failed-admission", signal.SIGTERM), ("engine", "retained-wait", 5),
                     ("engine_listener", "failed-admission", signal.SIGTERM)]
    assert delivered == [("engine", signal.SIGTERM), ("engine_listener", signal.SIGTERM)]
    assert graph.engine.error is initiating and graph.listener.error is initiating
    assert graph._clean_ack is None and graph._listener_exit is None


def _cr1a_host_case(tmp_path, monkeypatch):
    """Actual retained authority, all token/start/resume/signal providers fake."""
    import copy
    from astrid.core.execution import custody_broker as cb
    identities = {101: {"pid": 101, "uid": 501, "birth_id": "host-owner"},
                  202: {"pid": 202, "uid": 501, "birth_id": "listener-birth"}}
    def details(words):
        return {"pid": words[5], "uid": words[1], "pidversion": words[7],
                "sha256": "sha256:" + hashlib.sha256(json.dumps(words).encode()).hexdigest()}
    def token(pid, version):
        words = [0, 501, 0, 0, 0, pid, 0, version]
        return {**details(words), "words": words}
    tokens = {101: token(101, 1), 202: token(202, 1)}
    monkeypatch.setattr(cb, "audit_token_details", details)
    actor = cb.AuthenticatedCleanupActor(lambda: tokens[101], identities.get)
    request = _local_custody_request(tmp_path)
    authority = cb.RoleCustodyAuthority(Path(request["custody_scope"]), "engine_listener")
    authority.reserve_before_spawn(actor=actor, owner_epoch=request["owner_epoch"], admission_id="fake-listener")
    authority.bind_pending(actor=actor, admission_id="fake-listener", identity=identities[202], token=tokens[202])
    authority.bind_target(actor=actor, generation=1, identity=identities[202], token=tokens[202])
    start = lambda _: {"pid": 202, "uid": 501, "start_abstime": 100, "start_sec": 123, "start_usec": 456, "status": 4}
    monkeypatch.setattr(cb, "_darwin_process_start", start)
    authority.establish_start_anchor(actor=actor, generation=1, expected_target=authority.reference()["target"],
        authenticated_token=tokens[202], start_provider=start, token_provider=tokens.get, clock_provider=lambda: 101)
    authority.resume_suspended_target(actor=actor, generation=1, expected_target=authority.reference()["target"],
        release_id="fake-release", identity_provider=identities.get, resume_provider=lambda _: None)
    tokens[202] = token(202, 2)
    listener_base = authority.bind_target(actor=actor, generation=1, identity=identities[202], token=tokens[202], final_ready=True)
    authority.commit_ready()
    engine = {**copy.deepcopy(listener_base), "role": "engine",
              "target": {**listener_base["target"], "pid": 201, "birth_id": "engine-birth"}}
    base = {"engine": copy.deepcopy(engine), "engine_listener": listener_base}
    def rotate():
        tokens[202] = token(202, tokens[202]["pidversion"] + 1)
        authority.signal(actor=actor, generation=1, expected_target=authority.reference()["target"], signum=15,
            identity_provider=identities.get, signal_provider=lambda *_: None, start_provider=start, token_provider=tokens.get)
        return authority.reference()
    class Graph:
        def __init__(self, *, request):
            self.listener = SimpleNamespace(role_authority=authority, cleanup_actor=actor)
            self.engine = engine
            self.calls = 0; self.rotate_during_abort = False; self.fail_exit = False
        def prepare(self): return {"processes": {}, "engine_binding": {}, "session_config_digest": "fake", "custody_capabilities": copy.deepcopy(base)}
        def known_custody_capabilities(self): return {"engine": copy.deepcopy(self.engine), "engine_listener": authority.reference()}
        def abort(self):
            self.calls += 1
            if self.rotate_during_abort: rotate()
            if self.fail_exit: raise CleanupUncertainError("authenticated exit/reap proof absent")
            return {role: {"generation": ref["generation"], "target": ref["target"], "exit_code": 0,
                           "proof_kind": "retained-child-exit" if role == "engine" else "authenticated-retained-child-exit"}
                    for role, ref in self.known_custody_capabilities().items()}
    service = generic_host.LocalExecutionPreparation(operation_id=request["operation_id"], channel_id=request["channel_id"], graph_factory=Graph)
    service.prepare_local_execution(request)
    abort = {"version": generic_host.CURRENT_ROLES_VERSION, "command": "abort_local_execution",
             **{k: request[k] for k in ("operation_id", "channel_id", "owner_epoch", "custody_scope")},
             "profile_digest": request["profile"]["profile_digest"], "profile_binding_digest": generic_host._handoff_digest(request["profile"]),
             "base_roles_digest": generic_host._handoff_digest(base), "custody_capabilities": copy.deepcopy(base)}
    return service, authority, abort, rotate, actor


@pytest.mark.parametrize("revision", [0, 1, 2])
@pytest.mark.parametrize("during", [False, True])
def test_cr1a_host_cleans_original_or_stale_chain_member_after_refresh_during_abort(tmp_path, monkeypatch, revision, during):
    import copy
    service, authority, abort, rotate, actor = _cr1a_host_case(tmp_path, monkeypatch)
    member = None
    for _ in range(revision):
        current = rotate()
        if member is None: member = current
    if member is not None: abort["custody_capabilities"]["engine_listener"] = member
    original_base = copy.deepcopy(service.result)
    service.graph.rotate_during_abort = during
    reply = service.dispatch(abort)
    assert reply["status"] == "cleaned"
    assert reply["revision"] == revision + int(during)
    assert reply["current_roles"]["engine_listener"] == authority.reference()
    assert reply["cleanup"]["engine_listener"]["target"] == authority.reference()["target"]
    assert service.dispatch(abort) == reply
    assert service.graph.calls == 1
    assert service.result == original_base


@pytest.mark.parametrize("change", ["operation", "profile", "base-digest", "extra", "foreign-token", "float-generation", "engine", "listener-pid", "role", "scope"])
def test_cr1a_host_rejects_foreign_binding_before_intent_or_cleanup(tmp_path, monkeypatch, change):
    service, _, abort, _, _ = _cr1a_host_case(tmp_path, monkeypatch)
    if change == "operation": abort["operation_id"] = "foreign"
    elif change == "profile": abort["profile_digest"] = "sha256:" + "b" * 64
    elif change == "base-digest": abort["base_roles_digest"] = "sha256:" + "b" * 64
    elif change == "extra": abort["unbound"] = True
    elif change == "foreign-token": abort["custody_capabilities"]["engine_listener"]["target"]["audit_token_pidversion"] += 9
    elif change == "float-generation": abort["custody_capabilities"]["engine_listener"]["generation"] = 1.0
    elif change == "engine": abort["custody_capabilities"]["engine"]["target"]["audit_token_pidversion"] += 1
    elif change == "listener-pid": abort["custody_capabilities"]["engine_listener"]["target"]["pid"] += 1
    elif change == "role": abort["custody_capabilities"]["engine_listener"]["role"] = "host"
    else: abort["custody_capabilities"]["engine_listener"]["scope_root"] = "/foreign"
    reply = service.dispatch(abort)
    assert reply["status"] == "unresolved" and reply["error_code"] == "binding_conflict"
    assert service._current_abort_intent is None
    assert service.graph.calls == 0


@pytest.mark.parametrize("change", ["anchor-generation", "anchor-requester", "disk-extra", "disk-prefix", "engine-token", "missing-exit", "proof-target"])
def test_cr1a_host_reports_authority_or_cleanup_failure_explicitly(tmp_path, monkeypatch, change):
    import copy
    service, authority, abort, _, _ = _cr1a_host_case(tmp_path, monkeypatch)
    if change == "anchor-generation": authority._anchor_transition["intent"]["generation"] = 2
    elif change == "anchor-requester": authority._anchor_transition["intent"]["requester"] = {}
    elif change.startswith("disk"):
        record = authority._read()
        if change == "disk-extra": record["transitions"].append(copy.deepcopy(record["transitions"][-1]))
        else: record["transitions"][1]["ack"]["status"] = "forged"
        authority._write(record)
    elif change == "engine-token": service.graph.engine["target"]["audit_token_pidversion"] += 1
    elif change == "missing-exit": service.graph.fail_exit = True
    else:
        original = service.graph.abort
        def wrong_proof():
            result = original(); result["engine_listener"]["target"]["audit_token_pidversion"] += 1; return result
        service.graph.abort = wrong_proof
    reply = service.dispatch(abort)
    assert reply["status"] == "unresolved" and reply["error_code"] == "cleanup_unresolved"
    assert not service._aborted
    prior_calls = service.graph.calls
    assert service.dispatch(abort)["status"] == "unresolved"
    assert service.graph.calls == prior_calls  # No replay of uncertain cleanup.


@pytest.mark.parametrize("first", ["v1", "v2"])
def test_cr1a_abort_intents_exclude_other_version(tmp_path, monkeypatch, first):
    service, _, abort, _, _ = _cr1a_host_case(tmp_path, monkeypatch)
    v1 = {k: v for k, v in abort.items() if k != "base_roles_digest"}
    v1["version"] = "runtime.local-execution-host/v1"
    assert service.dispatch(v1 if first == "v1" else abort)["status"] == "cleaned"
    reply = service.dispatch(abort if first == "v1" else v1)
    assert reply["status"] == "unresolved" and reply["error_code"] == "binding_conflict"
    assert service.graph.calls == 1


@pytest.mark.parametrize("failure", ["foreign", "anchor", "ledger", "engine", "exit"])
def test_cr1a_control_thread_answers_next_frame_after_unresolved(tmp_path, monkeypatch, failure):
    import socket
    from astrid.core.execution import custody_broker as cb
    service, authority, abort, _, actor = _cr1a_host_case(tmp_path, monkeypatch)
    if failure == "foreign": abort["profile_digest"] = "sha256:" + "b" * 64
    elif failure == "anchor": authority._anchor_transition["intent"]["generation"] = 2
    elif failure == "ledger":
        record = authority._read(); record["transitions"].pop(); authority._write(record)
    elif failure == "engine": service.graph.engine["target"]["audit_token_pidversion"] += 1
    else: service.graph.fail_exit = True
    monkeypatch.setattr(cb.AuthenticatedCleanupActor, "private_peer", lambda _: actor)
    caller, host = socket.socketpair()
    caller.settimeout(1)
    thread = service.serve_control(host.detach())
    try:
        generic_host._send_activation_frame(caller, abort)
        assert generic_host._read_activation_frame(caller)["status"] == "unresolved"
        next_frame = {**abort, "unexpected": True}
        generic_host._send_activation_frame(caller, next_frame)
        assert generic_host._read_activation_frame(caller)["error_code"] == "binding_conflict"
        assert thread.is_alive()
    finally:
        caller.close(); thread.join(1)
    assert not thread.is_alive()


@pytest.mark.parametrize("first", ["v1", "v2"])
def test_cr1a_concurrent_mixed_versions_cannot_begin_two_cleanup_actions(tmp_path, monkeypatch, first):
    import threading
    service, _, abort, _, _ = _cr1a_host_case(tmp_path, monkeypatch)
    v1 = {k: v for k, v in abort.items() if k != "base_roles_digest"}; v1["version"] = "runtime.local-execution-host/v1"
    entered, release = threading.Event(), threading.Event()
    original = service.graph.abort
    def waiting_abort():
        entered.set()
        assert release.wait(1)
        return original()
    service.graph.abort = waiting_abort
    replies = []
    thread = threading.Thread(target=lambda: replies.append(service.dispatch(v1 if first == "v1" else abort)))
    thread.start()
    try:
        assert entered.wait(1)
        other = service.dispatch(abort if first == "v1" else v1)
        assert other["status"] == "unresolved" and other["error_code"] == "binding_conflict"
        assert service.graph.calls == 0
    finally:
        release.set(); thread.join(1)
    assert replies[0]["status"] == "cleaned" and service.graph.calls == 1


def _admission_host_case(tmp_path, monkeypatch, *, pending=False):
    import copy
    from astrid.core.execution import custody_broker as cb
    identities = {pid: {"pid": pid, "uid": 501, "birth_id": f"birth-{pid}"} for pid in (101, 201, 202)}
    def details(words):
        return {"pid": words[5], "uid": words[1], "pidversion": words[7],
                "sha256": "sha256:" + hashlib.sha256(json.dumps(words).encode()).hexdigest()}
    def token(pid, version):
        words = [0, 501, 0, 0, 0, pid, 0, version]
        return {**details(words), "words": words}
    tokens = {pid: token(pid, 1) for pid in identities}
    monkeypatch.setattr(cb, "audit_token_details", details)
    actor = cb.AuthenticatedCleanupActor(lambda: tokens[101], identities.get)
    request = _local_custody_request(tmp_path)
    brokers = {}
    for role, pid in (("engine", 201), ("engine_listener", 202)):
        authority = cb.RoleCustodyAuthority(Path(request["custody_scope"]), role)
        authority.reserve_before_spawn(actor=actor, owner_epoch=request["owner_epoch"], admission_id=role)
        start = lambda _, pid=pid: {"pid": pid, "uid": 501, "start_abstime": 100, "start_sec": 123, "start_usec": 456, "status": 4}
        if not pending:
            authority.bind_pending(actor=actor, admission_id=role, identity=identities[pid], token=tokens[pid])
            authority.bind_target(actor=actor, generation=1, identity=identities[pid], token=tokens[pid])
            authority.establish_start_anchor(actor=actor, generation=1, expected_target=authority.reference()["target"],
                authenticated_token=tokens[pid], start_provider=start, token_provider=tokens.get, clock_provider=lambda: 101)
            authority.resume_suspended_target(actor=actor, generation=1, expected_target=authority.reference()["target"],
                release_id=role, identity_provider=identities.get, resume_provider=lambda _: None)
        brokers[role] = SimpleNamespace(role_authority=authority, cleanup_actor=actor, pid=pid, start=start)
    base = {role: broker.role_authority.reference() for role, broker in brokers.items()}
    def rotate(role):
        broker = brokers[role]; pid = broker.pid
        tokens[pid] = token(pid, tokens[pid]["pidversion"] + 1)
        return broker.role_authority.signal(actor=actor, generation=1, expected_target=broker.role_authority.reference()["target"],
            signum=15, identity_provider=identities.get, signal_provider=lambda *_: None, start_provider=broker.start, token_provider=tokens.get)
    class Graph:
        def __init__(self, *, request):
            self.engine = brokers["engine"]; self.listener = brokers["engine_listener"]
            self.calls = 0; self.fail_exit = False
        def prepare(self): raise CleanupUncertainError("partial admitted graph")
        def known_custody_capabilities(self): return {r: b.role_authority.reference() for r, b in brokers.items()}
        def abort(self):
            self.calls += 1
            if self.fail_exit: raise CleanupUncertainError("missing positive wait")
            if not pending:
                for role in brokers: rotate(role)
            return {r: {"generation": ref["generation"], "target": copy.deepcopy(ref["target"]),
                        "exit_code": None if pending else 0,
                        "proof_kind": "never-spawned" if pending else ("retained-child-exit" if r == "engine" else "authenticated-retained-child-exit")}
                    for r, ref in self.known_custody_capabilities().items()}
    service = generic_host.LocalExecutionPreparation(operation_id=request["operation_id"], channel_id=request["channel_id"], graph_factory=Graph)
    with pytest.raises(CleanupUncertainError): service.prepare_local_execution(request)
    abort = {"version": generic_host.ADMISSION_CLEANUP_VERSION, "command": "abort_local_execution",
             **{k: request[k] for k in ("operation_id", "channel_id", "owner_epoch", "custody_scope")},
             "profile_digest": request["profile"]["profile_digest"], "profile_binding_digest": generic_host._handoff_digest(request["profile"]),
             "admission_roles": copy.deepcopy(base), "admission_roles_digest": generic_host._handoff_digest(base)}
    return service, brokers, abort, rotate, actor


@pytest.mark.parametrize("pending", [False, True])
def test_admission_host_cleans_both_roles_without_fabricating_ready(tmp_path, monkeypatch, pending):
    service, brokers, abort, _, _ = _admission_host_case(tmp_path, monkeypatch, pending=pending)
    reply = service.dispatch(abort)
    assert reply["status"] == "cleaned", service.control_error
    assert reply["revisions"] == {role: 0 if pending else 1 for role in brokers}
    assert reply["current_roles"] == service.known_custody_capabilities()
    assert service.result is None and all(b.role_authority._ready_base is None for b in brokers.values())
    assert "base_roles_digest" not in reply
    assert service.dispatch(abort) == reply and service.graph.calls == 1


@pytest.mark.parametrize("mutation", ["pid", "birth", "token", "missing-role", "digest", "operation", "ready", "missing-wait", "disk", "v1-intent", "v2-intent"])
def test_admission_host_fences_foreign_or_unproved_cleanup(tmp_path, monkeypatch, mutation):
    service, brokers, abort, _, _ = _admission_host_case(tmp_path, monkeypatch)
    if mutation in {"pid", "birth", "token"}:
        target = abort["admission_roles"]["engine"]["target"]
        if mutation == "pid": target["pid"] += 1
        elif mutation == "birth": target["birth_id"] = "foreign"
        else: target["audit_token_pidversion"] += 5
        abort["admission_roles_digest"] = generic_host._handoff_digest(abort["admission_roles"])
    elif mutation == "missing-role":
        del abort["admission_roles"]["engine"]; abort["admission_roles_digest"] = generic_host._handoff_digest(abort["admission_roles"])
    elif mutation == "digest": abort["admission_roles_digest"] = "sha256:" + "a" * 64
    elif mutation == "operation": abort["operation_id"] = "foreign"
    elif mutation == "ready": service.result = {"status": "prepared"}
    elif mutation == "missing-wait": service.graph.fail_exit = True
    elif mutation == "disk":
        authority = brokers["engine"].role_authority
        record = authority._read(); record["transitions"].pop(); authority._write(record)
    elif mutation == "v1-intent": service._abort_intent = "retained"
    else: service._current_abort_intent = "retained"
    reply = service.dispatch(abort)
    assert reply["status"] == "unresolved" and not service._aborted
    calls = service.graph.calls
    assert service.dispatch(abort)["status"] == "unresolved" and service.graph.calls == calls


def test_admission_intent_excludes_v1_and_ready_v2(tmp_path, monkeypatch):
    service, _, abort, _, _ = _admission_host_case(tmp_path, monkeypatch)
    assert service.dispatch(abort)["status"] == "cleaned"
    for version in ("runtime.local-execution-host/v1", generic_host.CURRENT_ROLES_VERSION):
        assert service.dispatch({**abort, "version": version})["status"] == "unresolved"
    assert service.graph.calls == 1


def test_engine_peer_capture_waits_for_release_on_same_descriptor(monkeypatch):
    import threading
    from astrid.core.execution import custody_broker as cb
    graph = generic_host._NativeEngineGraph(request={"profile": {}})
    graph.process = SimpleNamespace(pid=201)
    target = {"pid": 201, "uid": 501, "birth_id": "engine"}
    channel = object(); captures = []
    graph.engine = SimpleNamespace(state="sealed", operation_channel=channel, registration={"identity":target},
        bind_ready_token=lambda **kw:None, role_authority=SimpleNamespace(reference=lambda: {"target": target}))
    peer = SimpleNamespace(verify=lambda: dict(target))
    graph.engine.retained_pipe_actor = lambda: captures.append(channel) or peer
    result = []
    thread = threading.Thread(target=lambda: result.append(graph._bind_engine_control_peer(channel)))
    thread.start()
    assert captures == []
    graph._engine_released.set(); thread.join(1)
    assert not thread.is_alive() and captures == [channel] and result == [peer]


@pytest.mark.parametrize("mutation", ["pid", "birth", "uid", "no-release"])
def test_engine_peer_capture_rejects_foreign_or_unreleased_peer(monkeypatch, mutation):
    from astrid.core.execution import custody_broker as cb
    graph = generic_host._NativeEngineGraph(request={"profile": {}})
    target = {"pid": 201, "uid": 501, "birth_id": "engine"}
    peer = dict(target)
    if mutation == "no-release": graph._engine_released = SimpleNamespace(wait=lambda timeout: False)
    else:
        graph._engine_released.set(); peer["birth_id" if mutation == "birth" else mutation] = "foreign" if mutation == "birth" else 999
    graph.process = SimpleNamespace(pid=201)
    channel=object()
    graph.engine = SimpleNamespace(state="sealed", operation_channel=channel, registration={"identity":target},
        bind_ready_token=lambda **kw:None, retained_pipe_actor=lambda:SimpleNamespace(verify=lambda:peer),
        role_authority=SimpleNamespace(reference=lambda: {"target": target}))
    with pytest.raises(generic_host.HostError): graph._bind_engine_control_peer(channel)


def test_owner_retention_retries_bounded_abort_until_positive_cleanup(monkeypatch):
    preparation = generic_host.LocalExecutionPreparation(operation_id="operation", channel_id="channel")
    calls = []; pauses = []
    def abort():
        calls.append(1)
        assert not preparation._aborted
        if len(calls) < 3: raise CleanupUncertainError("still owned")
        preparation._aborted = True
    preparation.abort = abort
    monkeypatch.setattr(generic_host.time, "sleep", pauses.append)
    generic_host._retain_unresolved_owner(preparation)
    assert len(calls) == 3 and pauses == [1, 1] and preparation._aborted


def test_engine_control_first_frame_pins_only_post_release_token(monkeypatch):
    import threading
    from astrid.core.execution import custody_broker as cb
    graph = generic_host._NativeEngineGraph(request={"profile": {}, "operation_id": "op", "channel_id": "ch", "owner_epoch": "epoch"})
    channel = SimpleNamespace(settimeout=lambda _: None)
    target = {"pid": 201, "uid": 501, "birth_id": "engine"}
    graph.process = SimpleNamespace(pid=201)
    graph.engine = SimpleNamespace(state="sealed", operation_channel=channel, registration={"identity":target},
        bind_ready_token=lambda **kw:None, role_authority=SimpleNamespace(reference=lambda: {"target": target}))
    graph._engine_parent = SimpleNamespace(actor=None)
    first_read = threading.Event(); frame_ready = threading.Event(); replied = threading.Event()
    versions = []; token_version = [1]; reads = []
    def read(fd):
        assert fd is channel
        if reads: raise EOFError("fake completed channel")
        reads.append(1); first_read.set(); assert frame_ready.wait(1)
        return {"command": "signal_owned_listener"}
    actor = SimpleNamespace(verify=lambda: dict(target))
    def pin():
        versions.append(token_version[0]); return actor
    monkeypatch.setattr(cb, "_read_frame", read)
    graph.engine.retained_pipe_actor=pin
    monkeypatch.setattr(cb, "signal_owned_listener", lambda frame, **kwargs: {"status": "signaled"} if kwargs["engine_actor"] is actor else pytest.fail("wrong actor"))
    monkeypatch.setattr(cb, "_send_frame", lambda fd, reply: replied.set())
    graph.listener = object()
    graph._start_native_control()
    assert first_read.wait(1) and versions == []
    token_version[0] = 2
    graph._engine_released.set(); frame_ready.set()
    assert replied.wait(1)
    graph._native_thread.join(1)
    assert versions == [2] and graph._engine_parent.actor is actor


def test_owner_retention_keeps_control_serving_until_actual_cleanup(tmp_path, monkeypatch):
    import socket
    import threading
    from astrid.core.execution import custody_broker as cb
    service, _, abort, _, actor = _admission_host_case(tmp_path, monkeypatch)
    monkeypatch.setattr(cb.AuthenticatedCleanupActor, "private_peer", lambda _: actor)
    caller, host = socket.socketpair(); caller.settimeout(1)
    service.control_thread = service.serve_control(host.detach())
    failed = threading.Event(); resume = threading.Event(); calls = []
    original_abort = service.graph.abort
    def graph_abort():
        calls.append(1)
        if len(calls) == 1:
            failed.set(); raise CleanupUncertainError("still retained")
        return original_abort()
    service.graph.abort = graph_abort
    monkeypatch.setattr(generic_host.time, "sleep", lambda _: resume.wait(1))
    owner = threading.Thread(target=lambda: generic_host._retain_unresolved_owner(service))
    owner.start()
    try:
        assert failed.wait(1) and owner.is_alive() and service.control_thread.is_alive()
        generic_host._send_activation_frame(caller, abort)
        reply = generic_host._read_activation_frame(caller)
        assert reply["status"] == "cleaned"
        resume.set(); owner.join(1)
        assert service._aborted and not owner.is_alive()
    finally:
        resume.set(); caller.close(); owner.join(1); service.control_thread.join(1)


def _selected_source_fact_graph():
    from astrid.core.execution.vibecomfy_engine_adapter import SOURCE_FACT_VERSION
    target = {"pid": 201, "uid": 501, "birth_id": "engine-birth", "audit_token_sha256": "sha256:" + "a" * 64, "audit_token_pidversion": 1}
    listener = {**target, "pid": 202, "birth_id": "listener-birth"}
    graph = generic_host._NativeEngineGraph(request={"operation_id": "op", "channel_id": "ch", "owner_epoch": "epoch",
        "profile": {"engine_launch": {"source_revision": "actual-revision", "source_content_digest": "sha256:" + "d" * 64}}})
    graph.process = SimpleNamespace(pid=201)
    graph.engine = SimpleNamespace(role_authority=SimpleNamespace(reference=lambda: {"target": dict(target)}))
    graph.listener = SimpleNamespace(registration={"pid": 202}, role_authority=SimpleNamespace(reference=lambda: {"target": dict(listener)}))
    actor = SimpleNamespace(verify=lambda: dict(target)); graph._engine_parent = actor
    calls = []; values = {"current_source_revision": "actual-revision", "current_source_content_digest": "sha256:" + "d" * 64, "_process_start_identity": "listener-birth"}
    def observe(fact, pid):
        calls.append((fact, pid)); return values[fact]
    graph._source_observer = SimpleNamespace(pins={"actual": "host-created"}, observe=observe)
    frame = {"version": SOURCE_FACT_VERSION, "command": "observe_selected_source_fact", "operation_id": "op", "channel_id": "ch", "owner_epoch": "epoch",
             "engine_pid": 201, "source_pins_digest": generic_host._handoff_digest(graph._source_observer.pins), "fact": "current_source_revision", "pid": None}
    return graph, frame, actor, calls, values, target, listener


def test_selected_host_fact_executes_at_call_time_without_ready_listener_baseline():
    graph, frame, actor, calls, values, _, _ = _selected_source_fact_graph()
    graph.listener = None
    reply = graph._observe_selected_source_fact(frame, actor)
    assert reply["value"] == "actual-revision" and calls == [("current_source_revision", None)]
    values["current_source_revision"] = None
    assert graph._observe_selected_source_fact(frame, actor)["value"] is None
    assert len(calls) == 2 and graph._listener_exit is None


@pytest.mark.parametrize("change", ["operation", "channel", "epoch", "engine-pid", "pid-type", "digest", "extra", "version", "fact", "unexpected-pid", "foreign-actor"])
def test_selected_host_fact_rejects_unbound_request_before_any_helper(change):
    graph, frame, actor, calls, _, _, _ = _selected_source_fact_graph()
    fields = {"operation": "operation_id", "channel": "channel_id", "epoch": "owner_epoch", "engine-pid": "engine_pid", "digest": "source_pins_digest", "version": "version", "fact": "fact"}
    if change in fields: frame[fields[change]] = 999 if change == "engine-pid" else "foreign"
    elif change == "pid-type": frame["engine_pid"] = 201.0
    elif change == "extra": frame["unbound"] = True
    elif change == "unexpected-pid": frame["pid"] = 999
    else: actor = SimpleNamespace(verify=lambda: {"pid": 999})
    with pytest.raises(HostError): graph._observe_selected_source_fact(frame, actor)
    assert calls == []


@pytest.mark.parametrize("change", ["unknown-pid", "bool-pid", "different-birth", "changed-token", "changed-source"])
def test_selected_host_fact_rejects_unknown_or_changed_retained_fact(change):
    graph, frame, actor, calls, values, target, _ = _selected_source_fact_graph()
    frame.update(fact="_process_start_identity", pid=202)
    if change == "unknown-pid": frame["pid"] = 203
    elif change == "bool-pid": frame["pid"] = True
    elif change == "different-birth": values["_process_start_identity"] = "other-birth"
    elif change == "changed-token":
        original = graph._source_observer.observe
        def changed(fact, pid):
            value = original(fact, pid); target["audit_token_pidversion"] += 1; return value
        graph._source_observer.observe = changed
    else: frame.update(fact="current_source_revision", pid=None); values["current_source_revision"] = "other-revision"
    with pytest.raises(HostError): graph._observe_selected_source_fact(frame, actor)
    assert len(calls) == (0 if change in {"unknown-pid", "bool-pid"} else 1)


def _native_listener_exit_v2_case(tmp_path, monkeypatch):
    import copy
    from astrid.core.execution.vibecomfy_engine_adapter import LISTENER_EXIT_V2
    service, authority, abort, rotate, actor = _cr1a_host_case(tmp_path, monkeypatch)
    graph = generic_host._NativeEngineGraph(request={"profile": {}, "operation_id": service.operation_id,
        "channel_id": service.channel_id, "owner_epoch": abort["owner_epoch"]}, enable_exit_v2=True)
    persisted = []
    graph.listener = SimpleNamespace(role_authority=authority, cleanup_actor=actor, registration={}, _persist=lambda event: persisted.append(event))
    graph._engine_parent = actor
    base = authority.ready_reference()
    frame = {"version": LISTENER_EXIT_V2, "command": "observe_owned_listener_exit", "operation_id": service.operation_id,
        "channel_id": service.channel_id, "owner_epoch": abort["owner_epoch"], "role": "engine_listener", "generation": base["generation"], "target": copy.deepcopy(base["target"]), "exit_code": -15}
    return graph, frame, authority, rotate, actor, persisted


@pytest.mark.parametrize("member", [0, 1, 2])
def test_fake_listener_exit_v2_accepts_only_retained_ready_chain_and_persists_current(tmp_path, monkeypatch, member):
    graph, frame, authority, rotate, actor, persisted = _native_listener_exit_v2_case(tmp_path, monkeypatch)
    members = [authority.ready_reference(), rotate(), rotate()]
    frame["target"] = members[member]["target"]
    ack = graph._record_listener_exit(frame, actor)
    assert ack["current_reference"] == members[-1] and ack["revision"] == 2
    assert persisted == ["listener_exit_observed_v2"]
    assert graph.listener.registration["exit_evidence"] == ack == graph._listener_exit
    assert graph._record_listener_exit(frame, actor) == ack and len(persisted) == 1


@pytest.mark.parametrize("change", ["default", "foreign-token", "birth", "pid", "generation", "float-generation", "exit-type", "binder", "extra", "actor", "pre-ready", "ledger"])
def test_fake_listener_exit_v2_fences_unproved_or_foreign_chain(tmp_path, monkeypatch, change):
    graph, frame, authority, _, actor, persisted = _native_listener_exit_v2_case(tmp_path, monkeypatch)
    if change == "default": graph._enable_exit_v2 = False
    elif change == "foreign-token": frame["target"]["audit_token_pidversion"] += 99
    elif change == "birth": frame["target"]["birth_id"] = "foreign"
    elif change == "pid": frame["target"]["pid"] += 1
    elif change == "generation": frame["generation"] += 1
    elif change == "float-generation": frame["generation"] = 1.0
    elif change == "exit-type": frame["exit_code"] = True
    elif change == "binder": frame["channel_id"] = "foreign"
    elif change == "extra": frame["extra"] = True
    elif change == "actor": actor = SimpleNamespace(verify=lambda: {"pid": 999})
    elif change == "pre-ready": authority._ready_committed = False
    else:
        record = authority._read(); record["transitions"].pop(); authority._write(record)
    with pytest.raises(Exception): graph._record_listener_exit(frame, actor)
    assert persisted == [] and graph._listener_exit is None


def test_fake_listener_exit_v2_fences_refresh_during_persistence_and_conflicting_replay(tmp_path, monkeypatch):
    graph, frame, _, rotate, actor, persisted = _native_listener_exit_v2_case(tmp_path, monkeypatch)
    graph.listener._persist = lambda event: (persisted.append(event), rotate())
    with pytest.raises(HostError, match="chain changed"): graph._record_listener_exit(frame, actor)
    assert graph._listener_exit is None and persisted == ["listener_exit_observed_v2"]


def test_native_listener_exit_default_v1_bytes_and_strict_current_reference(tmp_path, monkeypatch):
    from astrid.core.execution import custody_broker as cb
    graph, frame, authority, rotate, actor, persisted = _native_listener_exit_v2_case(tmp_path, monkeypatch)
    graph._enable_exit_v2 = False; frame["version"] = cb.ENGINE_CONTROL_VERSION
    ack = graph._record_listener_exit(frame, actor)
    assert ack == {**{k: v for k, v in frame.items() if k != "command"}, "status": "exit_recorded"}
    assert set(ack) == set(frame) - {"command"} | {"status"}
    graph._listener_exit = None; rotate()
    with pytest.raises(HostError, match="stale"): graph._record_listener_exit(frame, actor)


@pytest.mark.parametrize("observed,retained,expected", [("Wed Oct  7 12:00:00 2026", "ps-lstart:Wed Oct  7 12:00:00 2026", True),
    ("Wed Oct  7 12:00:01 2026", "ps-lstart:Wed Oct  7 12:00:00 2026", False), ("same", "same", True),
    (None, "ps-lstart:same", False), ("", "ps-lstart:", False)])
def test_selected_birth_normalization_preserves_exact_incarnation(monkeypatch, observed, retained, expected):
    monkeypatch.setattr(generic_host.sys, "platform", "darwin")
    assert generic_host._selected_birth_matches(observed, retained) is expected


def test_native_registry_normalizes_both_engine_and_listener_births_only(tmp_path, monkeypatch):
    from astrid.core.execution import custody_broker as cb
    monkeypatch.setattr(generic_host.sys, "platform", "darwin")
    born = "Wed Oct  7 12:00:00 2026"
    values = {"pid": "201", "comfy_pid": "202", "comfy_process_start_identity": born,
              "url": "http://127.0.0.1:8199", "source_revision": "selected", "source_content_digest": "sha256:" + "d" * 64,
              "config.json": "{}", "launch.json": json.dumps({"pid": 201, "url": "http://127.0.0.1:8199", "launch_token": "nonce", "process_start_identity": born})}
    for name, value in values.items(): (tmp_path / name).write_text(value)
    graph = generic_host._NativeEngineGraph(request={"profile": {"engine_endpoint": values["url"],
        "session_config_digest": "sha256:" + hashlib.sha256(b"{}").hexdigest(),
        "engine_launch": {"source_revision": values["source_revision"], "source_content_digest": values["source_content_digest"]}}})
    graph.process = SimpleNamespace(pid=201, _astrid_process_birth="ps-lstart:" + born)
    graph.custody = SimpleNamespace(_handles={202: ("ps-lstart:" + born, object())})
    graph.engine = graph.listener = SimpleNamespace(state="ready-bound")
    monkeypatch.setattr(cb, "default_process_identity", lambda pid: {"pid": pid, "parent_pid": 201, "birth_id": "ps-lstart:" + born})
    monkeypatch.setattr(generic_host.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=":8199 (LISTEN)", stderr=""))
    assert graph._measure_registry(tmp_path, "nonce", 202)["pid"] == 201
    for name in ("launch.json", "comfy_process_start_identity"):
        (tmp_path / name).write_text(json.dumps({"pid": 201, "url": values["url"], "launch_token": "nonce", "process_start_identity": "other-birth"}) if name == "launch.json" else "other-birth")
        with pytest.raises(HostError): graph._measure_registry(tmp_path, "nonce", 202)
        (tmp_path / name).write_text(values[name])


def test_fake_exit_v2_cleanup_uses_persisted_current_target_after_real_parent_wait(tmp_path, monkeypatch):
    graph, _, _, _ = _native_abort_graph(tmp_path, monkeypatch)
    current = {"generation": 1, "target": dict(graph.listener.target)}
    stale = {**graph.listener.target, "audit_token_pidversion": 0}
    graph.process.on_wait = lambda: setattr(graph, "_listener_exit", {"generation": 1, "target": stale, "current_reference": current, "exit_code": 3})
    assert graph.abort()["engine_listener"]["target"] == current["target"]


def test_fake_listener_exit_v2_conflicting_replay_cannot_replace_persisted_wait(tmp_path, monkeypatch):
    graph, frame, _, _, actor, persisted = _native_listener_exit_v2_case(tmp_path, monkeypatch)
    original = graph._record_listener_exit(frame, actor)
    with pytest.raises(HostError, match="replay conflicts"): graph._record_listener_exit({**frame, "exit_code": 0}, actor)
    assert graph._listener_exit == original and persisted == ["listener_exit_observed_v2"]


def test_launch_worker_projection_matches_fixed_execution_context(tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import _launch_worker_probe_environments
    from astrid.core.rendering.remotion_runtime import probe_context
    manifest = _write_manifest(tmp_path / 'fixed-worker')
    data = json.loads(manifest.read_text())
    data['command']['env'] = {'HOME': str(tmp_path / 'worker-home'), 'LANG': 'C'}
    data['metadata']['required_env'] = ['TMPDIR']
    manifest.write_text(json.dumps(data))
    environment = {'HOME': str(tmp_path), 'LANG': 'UTF-8', 'PATH': '/usr/bin',
                   'TMPDIR': str(tmp_path / 'worker-temp')}
    for name in ('HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'PATH', 'TMPDIR'):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    host = GenericPackHost(pack_roots=[tmp_path], credential_source=environment)
    record = host.discover()[0]
    actual, secrets = host._child_environment(
        record, tmp_path / 'attempt', explicit_env=dict(record.definition.command.env))
    try:
        candidates = _launch_worker_probe_environments((tmp_path,), environment)
        context = probe_context(environment=actual, python_executable=sys.executable)
        assert actual['HOME'] == str(tmp_path / 'worker-home')
        assert actual['LANG'] == 'C'
        assert context in [probe_context(environment=e, python_executable=sys.executable)
                           for e in candidates]
        assert context != probe_context(environment=candidates[0], python_executable=sys.executable)
        from astrid.core.execution.generic_host import _admitted_python_roots, _dependency_pythonpath
        assert candidates[1]['ASTRID_PACKS_PATH'] == str(tmp_path)
        expected_paths = tuple(dict.fromkeys((str(Path(__file__).resolve().parents[1]),
            *_dependency_pythonpath(), *(str(root) for root in _admitted_python_roots(record.source_root, record.definition)))))
        assert tuple(candidates[1]['PYTHONPATH'].split(os.pathsep)) == expected_paths
    finally:
        secrets.clear()


def test_readiness_preserves_network_guard_in_final_command_popen_environment(tmp_path, monkeypatch):
    from astrid.core.rendering import remotion_runtime
    _write_manifest(tmp_path / 'guarded-command')
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    for name, value in {'ASTRID_REMOTION_PROJECT_DIR': str(tmp_path / 'runtime'),
        'ASTRID_REMOTION_VALIDATION_ROLE': 'host', 'ASTRID_REMOTION_VALIDATION_PATH': str(tmp_path / 'validation'),
        'ASTRID_REMOTION_VALIDATION_SHA256': 'a' * 64, 'ASTRID_REMOTION_VALIDATION_LAUNCH': 'b' * 32}.items():
        monkeypatch.setenv(name, value)
    verified = []
    monkeypatch.setattr(remotion_runtime, '_validated_runtime', lambda project, *, environment: verified.append(dict(environment)))
    monkeypatch.setattr(generic_host, '_network_policy', lambda _record: {'mode': 'deny'})
    monkeypatch.setattr(generic_host, '_network_sandbox_argv', lambda argv, *_args: argv)
    captured = []
    class ReachedOwnedLaunch(Exception):
        pass
    def launch(argv, **kwargs):
        captured.append(kwargs['env'])
        raise ReachedOwnedLaunch
    monkeypatch.setattr(generic_host, 'popen_owned_group', launch)
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    with pytest.raises(ReachedOwnedLaunch):
        host._run_command_definition(record, {}, attempt / 'outputs', attempt)
    assert len(captured) == len(verified) == 1
    actual = captured[0]
    hook = attempt / '.astrid-network-hook'
    assert str(hook) in actual['PYTHONPATH'].split(os.pathsep)
    assert (hook / 'sitecustomize.py').read_text() == 'from astrid.core.execution.network_policy import install_from_environment\ninstall_from_environment()\n'
    assert json.loads(actual['ASTRID_NETWORK_POLICY']) == {'mode': 'deny'}
    assert actual['ASTRID_REMOTION_VALIDATION_ROLE'] == 'worker'
    assert str(hook) not in verified[0]['PYTHONPATH'].split(os.pathsep)


def test_launch_worker_projects_fixed_registry_selectors_and_transport_filter(tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import _launch_worker_probe_environments
    from astrid.core.rendering.transport import _build_environment
    manifest = _write_manifest(tmp_path / 'fixed-registry')
    data = json.loads(manifest.read_text())
    fixed = {'ASTRID_SOURCE_STATE': str(tmp_path / 'state.json'), 'XDG_STATE_HOME': str(tmp_path / 'state'),
             'XDG_CONFIG_HOME': str(tmp_path / 'config'), 'USER': 'fixed-user'}
    data['command']['env'] = fixed
    manifest.write_text(json.dumps(data))
    environment = {'HOME': str(tmp_path), 'PATH': '/usr/bin', 'LANG': 'C'}
    candidates = _launch_worker_probe_environments((tmp_path,), environment)
    assert all(candidates[1][name] == value for name, value in fixed.items())
    for key in tuple(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in candidates[1].items():
        monkeypatch.setenv(key, value)
    renderer = _build_environment(None)
    from astrid.core.subprocess_env import build_child_subprocess_env
    assert renderer == build_child_subprocess_env(base=candidates[1], parent=candidates[1])
    assert 'PYTHONPATH' not in renderer and 'XDG_STATE_HOME' not in renderer


@pytest.mark.parametrize('configured_pythonpath', [None, 'configured'])
def test_launch_command_projection_matches_actual_final_context(tmp_path, monkeypatch, configured_pythonpath):
    from astrid.core.rendering import remotion_runtime
    from astrid.core.subprocess_env import build_child_subprocess_env
    manifest = _write_manifest(tmp_path / 'actual-command')
    data = json.loads(manifest.read_text())
    data['command']['env'] = {'PYTHONPATH': str(tmp_path / 'command-imports')}
    environment = {'HOME': str(tmp_path), 'PATH': '/usr/bin', 'LANG': 'C'}
    if configured_pythonpath:
        environment['PYTHONPATH'] = str(tmp_path / 'configured-imports')
        data['metadata']['required_env'] = ['PYTHONPATH']
    manifest.write_text(json.dumps(data))
    for key in tuple(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    host = GenericPackHost(pack_roots=[tmp_path], credential_source=environment)
    record = host.discover()[0]
    # Use the real command builder and stop exactly at its owned Popen boundary.
    captured = []
    class ReachedOwnedLaunch(Exception):
        pass
    monkeypatch.setattr(generic_host, '_network_policy', lambda _record: None)
    def launch(argv, **kwargs):
        captured.append(kwargs['env'])
        raise ReachedOwnedLaunch
    monkeypatch.setattr(generic_host, 'popen_owned_group', launch)
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    with pytest.raises(ReachedOwnedLaunch):
        host._run_command_definition(record, {}, attempt / 'outputs', attempt)
    actual = captured[0]
    projected = generic_host._launch_worker_probe_environments((tmp_path,), environment)
    command = projected[2]
    assert 'ASTRID_PACKS_PATH' not in actual and 'ASTRID_PACKS_PATH' not in command
    selected = environment['PYTHONPATH'] if configured_pythonpath else data['command']['env']['PYTHONPATH']
    assert actual['PYTHONPATH'].endswith(selected)
    # Exact reader projections, not an accepting mock of _validated_runtime.
    assert remotion_runtime.probe_context(environment=actual, python_executable=sys.executable) == remotion_runtime.probe_context(environment=command, python_executable=sys.executable)
    assert remotion_runtime._registry_context(actual, generator=False) == remotion_runtime._registry_context(command, generator=False)
    assert remotion_runtime._registry_context(build_child_subprocess_env(base=actual, parent=actual), generator=False) == remotion_runtime._registry_context(build_child_subprocess_env(base=command, parent=command), generator=False)


class _IdleReadinessRuntime(FakeRuntime):
    """Explicit producer-shaped identity for the idle/renewal contract tests."""
    def __init__(self):
        super().__init__()
        self.identity = {"runtime_epoch": 7, "runtime_instance_id": "idle-runtime-A",
                         "instance_id": "idle-runtime-A", "coordinator_epoch": "idle-runtime-A",
                         "runtime_session_id": "idle-session-A"}
        self.nonce = 0
        self.registration_nonces = []
        self.health_calls = 0
        self.claim_calls = 0
        self.on_claim = None
        self.offline = False

    def health(self):
        self.health_calls += 1
        if self.offline:
            raise TimeoutError("fixture Runtime unavailable")
        return {**super().health(), **self.identity}

    def renew_registration_session(self):
        self.nonce += 1

    def register_executor(self, executor_id, **payload):
        self.registration_nonces.append(self.nonce)
        return super().register_executor(executor_id, **payload)

    def claim_next(self, **payload):
        self.claim_calls += 1
        self.claim_payload = payload
        if self.on_claim is not None:
            return self.on_claim(payload)
        return None

    def fork_for_host_slot(self):
        # A fake transport has no attempt ownership; share only this coordinator
        # state. Real generated transports retain their existing private fork.
        return self


@pytest.fixture
def idle_remotion_host(tmp_path, monkeypatch):
    from astrid.core.rendering import remotion_runtime as runtime
    for name in list(os.environ):
        if (name.startswith(("GIT_", "DYLD_", "LD_"))
                or name in runtime._UNSUPPORTED_PYTHON_STARTUP
                or name in {"PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "PYTHONHOME",
                            "NODE_OPTIONS", "NODE_PATH", "NODE_ICU_DATA", "OPENSSL_CONF"}):
            monkeypatch.delenv(name, raising=False)
    project, schema = tmp_path / "native-project", tmp_path / "schema"
    project.mkdir(); schema.mkdir()
    node = tmp_path / "node"
    node.write_bytes(b"fixture node identity")
    node.chmod(0o700)
    reference = tmp_path / "validation.json"
    reference.write_bytes(b'{"fixture":"launch-bound validation"}')
    reference.chmod(0o600)
    values = {
        "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "ASTRID_REMOTION_PROJECT_DIR": str(project), "ASTRID_NODE_EXECUTABLE": str(node),
        "ASTRID_TIMELINE_SCHEMA_PYTHONPATH": str(schema),
        "ASTRID_REMOTION_VALIDATION_PATH": str(reference),
        "ASTRID_REMOTION_VALIDATION_SHA256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "ASTRID_REMOTION_VALIDATION_LAUNCH": "fixture-launch", "ASTRID_REMOTION_VALIDATION_ROLE": "host",
        "ASTRID_SOURCE_STATE": str(tmp_path / "source-state"),
        "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    pack = tmp_path / "packs"
    manifest = _write_manifest(pack / "echo")
    value = json.loads(manifest.read_text())
    value["metadata"].update(adapter_family="cpu", requires_remotion=True)
    manifest.write_text(json.dumps(value))
    calls = {"native": 0, "available": True, "exception": None}
    def status(*, require_explicit_project=False):
        assert require_explicit_project is True
        calls["native"] += 1
        if calls["exception"] is not None:
            raise calls["exception"]
        return runtime.RemotionRuntimeStatus(
            calls["available"], project, reason=None if calls["available"] else "fixture native tree changed",
            node_executable=node, node_version="v20.19.4", remotion_cli=project / "cli.js")
    monkeypatch.setattr(runtime, "remotion_runtime_status", status)
    client = _IdleReadinessRuntime()
    host = GenericPackHost(pack_roots=[pack], client=client)
    host.discover()
    return SimpleNamespace(host=host, client=client, calls=calls, values=values,
                           project=project, node=node, schema=schema, reference=reference)


def test_idle_remotion_empty_polls_and_canonical_renewal_skip_only_native_walk(idle_remotion_host):
    world = idle_remotion_host
    host, client = world.host, world.client
    host.register()
    assert world.calls["native"] == 1
    for _ in range(4):
        assert host.claim_once() is None
        host._renew_executor_registration()
    assert world.calls["native"] == 1
    assert client.claim_calls == 4
    assert client.health_calls == 5
    assert client.registration_nonces == [0, 1, 2, 3, 4]
    assert len(client.registrations) == 5
    assert all(row[1]["capabilities"][0]["status"] == "ready" for row in client.registrations)
    # Explicit registration is still FULL, unlike a liveness renewal.
    host.register()
    assert world.calls["native"] == 2


@pytest.mark.parametrize("change", [
    "project", "node", "schema", "probe_tmp", "registry_context", "validation_role",
    "validation_launch", "validation_reference", "reference_content", "reference_mode", "unsupported_loader",
])
def test_idle_remotion_changed_or_unknown_context_never_inherits_ready(idle_remotion_host, monkeypatch, change):
    world = idle_remotion_host
    host = world.host
    # Discovery alone never creates native readiness for an idle poll.
    assert host.claim_once() is None
    assert world.client.claim_calls == 0 and world.calls["native"] == 0
    host.register()
    if change in {"project", "node", "schema"}:
        name = {"project": "ASTRID_REMOTION_PROJECT_DIR", "node": "ASTRID_NODE_EXECUTABLE",
                "schema": "ASTRID_TIMELINE_SCHEMA_PYTHONPATH"}[change]
        replacement = world.project.parent / ("other-" + change)
        if change == "node":
            replacement.write_bytes(b"other node"); replacement.chmod(0o700)
        else:
            replacement.mkdir()
        monkeypatch.setenv(name, str(replacement))
    elif change == "probe_tmp":
        monkeypatch.setenv("TMPDIR", str(world.project.parent / "other-temp"))
    elif change == "registry_context":
        monkeypatch.setenv("ASTRID_SOURCE_STATE", str(world.project.parent / "other-state"))
    elif change == "validation_role":
        monkeypatch.setenv("ASTRID_REMOTION_VALIDATION_ROLE", "other-role")
    elif change == "validation_launch":
        monkeypatch.setenv("ASTRID_REMOTION_VALIDATION_LAUNCH", "other-launch")
    elif change == "validation_reference":
        replacement = world.reference.with_name("other-validation.json")
        replacement.write_bytes(world.reference.read_bytes()); replacement.chmod(0o600)
        monkeypatch.setenv("ASTRID_REMOTION_VALIDATION_PATH", str(replacement))
    elif change == "reference_content":
        world.reference.write_bytes(b"changed")
    elif change == "reference_mode":
        world.reference.chmod(0o644)
    else:
        monkeypatch.setenv("NODE_OPTIONS", "--fixture-forbidden")
    assert host.claim_once() is None
    host._renew_executor_registration()
    assert world.calls["native"] == 1
    assert world.client.claim_calls == 0
    assert world.client.registrations[-1][1]["capabilities"][0]["status"] == "unavailable"
    assert not host.capabilities["test.echo"].ready


@pytest.mark.parametrize("failure", ["unavailable", "exception"])
def test_idle_remotion_claimed_task_full_preflight_failure_settles_exact_lease(idle_remotion_host, monkeypatch, failure):
    world = idle_remotion_host
    host, client = world.host, world.client
    host.register()
    task = {"id": "task-native", "capability": "test.echo", "spec": {}}
    client.tasks["task-native"] = {"task": task}
    client.on_claim = lambda _payload: {"task_id": "task-native", "lease_id": "lease-native",
        "attempt_id": "attempt-native", "fence": 9, "runtime_epoch": 7}
    if failure == "unavailable":
        world.calls["available"] = False
    else:
        world.calls["exception"] = RuntimeError("fixture full validation threw")
    def forbidden(*_args, **_kwargs):
        pytest.fail("no materialization/dispatch may occur before full selected-task admission")
    monkeypatch.setattr(host, "_materialize_inputs", forbidden)
    monkeypatch.setattr(host, "_run_command_definition", forbidden)
    with pytest.raises(HostError, match="unavailable|fixture full validation threw"):
        host.claim_once()
    assert world.calls["native"] == 2
    assert len(client.failures) == 1
    task_id, lease, _error, details = client.failures[0]
    assert (task_id, lease, details["attempt_id"], details["fence"], details["retryable"]) == (
        "task-native", "lease-native", "attempt-native", 9, False)
    assert not client.settlements and not host._active_processes
    # An exception must evict the old successful observation too; otherwise
    # the next empty poll/renewal would advertise the same known-failed input.
    assert host.claim_once() is None
    host._renew_executor_registration()
    assert world.calls["native"] == 2 and client.claim_calls == 1
    assert client.registrations[-1][1]["capabilities"][0]["status"] == "unavailable"


@pytest.mark.parametrize("field", ["runtime_instance_id", "runtime_session_id", "runtime_epoch", "schema_digest"])
def test_idle_remotion_observed_runtime_identity_change_fences_without_native_walk(idle_remotion_host, field):
    world = idle_remotion_host
    host, client = world.host, world.client
    host.register()
    if field == "schema_digest":
        client.identity[field] = "sha256:" + "2" * 64
    else:
        client.identity[field] = 8 if field == "runtime_epoch" else "other-identity"
        if field == "runtime_instance_id":
            client.identity["instance_id"] = client.identity["coordinator_epoch"] = "other-identity"
    with pytest.raises(HostError, match="runtime .* changed|runtime compatibility blocked"):
        host._renew_executor_registration()
    # Local fences remain even if a later read happens to return old identity.
    with pytest.raises(HostError, match="runtime .* changed|runtime compatibility blocked"):
        host.claim_once()
    assert world.calls["native"] == 1
    assert len(client.registrations) == 1 and client.claim_calls == 0


def test_idle_remotion_runtime_transport_failure_blocks_claims_until_successful_renewal(idle_remotion_host):
    world = idle_remotion_host
    host, client = world.host, world.client
    host.register()
    client.offline = True
    with pytest.raises(TimeoutError):
        host._renew_executor_registration()
    with pytest.raises(HostError, match="registration refresh is blocked"):
        host.claim_once()
    assert world.calls["native"] == 1 and client.claim_calls == 0
    client.offline = False
    host._renew_executor_registration()
    assert host.claim_once() is None
    assert client.claim_calls == 1 and world.calls["native"] == 1


def test_idle_remotion_dead_runtime_checked_before_initial_full_preflight(idle_remotion_host):
    world = idle_remotion_host
    world.client.offline = True
    with pytest.raises(TimeoutError):
        world.host.register()
    assert world.calls["native"] == 0 and not world.client.registrations


def test_idle_remotion_parallel_slots_copy_observation_and_honor_parent_fence(idle_remotion_host):
    world = idle_remotion_host
    host, client = world.host, world.client
    host.register()
    slots = [host._new_claim_slot() for _ in range(2)]
    for slot in slots:
        assert slot._remotion_full_preflight == host._remotion_full_preflight
        assert slot._remotion_full_preflight is not host._remotion_full_preflight
        assert slot.claim_once() is None
    host._runtime_registration_fence = "fixture parent Runtime instance changed"
    for slot in slots:
        with pytest.raises(HostError, match="parent Runtime instance changed"):
            slot.claim_once()
        slot.shutdown()
    assert client.claim_calls == 2 and world.calls["native"] == 1


def test_idle_remotion_parallel_real_claim_loop_renews_without_full_walk(idle_remotion_host):
    world = idle_remotion_host
    host, client = world.host, world.client
    host.max_concurrency = 2
    host.register()
    host._registration_refresh_deadline = 0.0
    def empty_claim(_payload):
        if client.claim_calls >= 2:
            host._shutdown.set()
        return None
    client.on_claim = empty_claim
    assert host.run(poll_seconds=0.01) == []
    assert client.claim_calls >= 2 and len(client.registrations) == 2
    assert world.calls["native"] == 1
    assert not host._claim_slots and not host._cleanup_uncertain



def test_idle_remotion_parent_fence_during_idle_check_prevents_claim_rpc(idle_remotion_host, monkeypatch):
    world = idle_remotion_host
    host, client = world.host, world.client
    host.register()
    slot = host._new_claim_slot()
    original = slot._preflight
    def checked(*args, **kwargs):
        result = original(*args, **kwargs)
        host._runtime_registration_fence = "fixture parent fenced during idle check"
        return result
    monkeypatch.setattr(slot, "_preflight", checked)
    with pytest.raises(HostError, match="parent fenced during idle check"):
        slot.claim_once()
    assert client.claim_calls == 0 and world.calls["native"] == 1
    assert host._handoff_claim_rpc_in_flight == 0
    assert host._handoff_active_attempts == 0
    slot.shutdown()


def test_idle_remotion_parent_fence_during_selected_full_check_fails_lease(idle_remotion_host, monkeypatch):
    from astrid.core.rendering import remotion_runtime as runtime
    world = idle_remotion_host
    host, client = world.host, world.client
    host.register()
    slot = host._new_claim_slot()
    original = runtime.remotion_runtime_status
    def full_check(**kwargs):
        result = original(**kwargs)
        host._runtime_registration_fence = "fixture parent fenced during selected full check"
        return result
    monkeypatch.setattr(runtime, "remotion_runtime_status", full_check)
    client.tasks["task-native"] = {"task": {"id": "task-native", "capability": "test.echo", "spec": {}}}
    client.on_claim = lambda _payload: {"task_id": "task-native", "lease_id": "lease-native",
        "attempt_id": "attempt-native", "fence": 9, "runtime_epoch": 7}
    def forbidden(*_args, **_kwargs):
        pytest.fail("parent fencing during preflight must prevent task work")
    monkeypatch.setattr(slot, "_materialize_inputs", forbidden)
    with pytest.raises(HostError, match="parent fenced during selected full check"):
        slot.claim_once()
    assert world.calls["native"] == 2 and len(client.failures) == 1
    assert client.failures[0][0:2] == ("task-native", "lease-native")
    assert client.failures[0][3]["attempt_id"] == "attempt-native"
    assert client.failures[0][3]["fence"] == 9
    assert not slot._active_processes and not client.settlements
    slot.shutdown()


def test_idle_remotion_parallel_failed_full_check_cannot_restore_parent_success(idle_remotion_host):
    world = idle_remotion_host
    host, client = world.host, world.client
    host.register()
    first = host._new_claim_slot()
    world.calls["exception"] = RuntimeError("fixture parallel native validation failed")
    task = {"task": {"id": "task-parallel", "capability": "test.echo", "spec": {}}}
    with pytest.raises(HostError, match="parallel native validation failed"):
        first.run_task(task, lease_token="lease-parallel", attempt_id="attempt-parallel", fence=12)
    assert "test.echo" not in host._remotion_full_preflight
    next_dispatch = host._new_claim_slot()
    assert "test.echo" not in next_dispatch._remotion_full_preflight
    assert next_dispatch.claim_once() is None
    assert not next_dispatch.capabilities["test.echo"].ready
    assert client.claim_calls == 0 and world.calls["native"] == 2
    first.shutdown(); next_dispatch.shutdown()


@pytest.mark.parametrize("identity_fault", ["missing", "instance-mismatch", "coordinator-mismatch"])
def test_runtime_instance_fixture_does_not_relax_identity_refusals(tmp_path, monkeypatch, identity_fault):
    _write_manifest(tmp_path / "echo")
    runtime = FakeRuntime()
    health = dict(runtime.health())
    if identity_fault == "missing":
        for name in ("instance_id", "runtime_instance_id", "coordinator_epoch"):
            health.pop(name)
    elif identity_fault == "instance-mismatch":
        health["instance_id"] = "foreign-instance"
    else:
        health["coordinator_epoch"] = "foreign-coordinator"
    monkeypatch.setattr(runtime, "health", lambda: health)
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    with pytest.raises(HostError, match="Runtime instance fields disagree"):
        host.register()
    assert runtime.registrations == [] and runtime.capability_registrations == []


@pytest.mark.parametrize("metadata,expected", [
    ({}, (None, None)),
    ({"estimated_scratch_bytes": 0, "estimated_output_bytes": 0}, (0, 0)),
    ({"estimated_scratch_bytes": 7, "estimated_output_bytes": 11}, (7, 11)),
    ({"estimated_scratch_bytes": 0}, (0, None)),
    ({"estimated_scratch_bytes": False, "estimated_output_bytes": "11"}, (None, None)),
    ({"estimated_scratch_bytes": -1, "estimated_output_bytes": 1.5}, (None, None)),
    ({"estimated_scratch_bytes": None, "estimated_output_bytes": 0}, (None, 0)),
])
def test_declared_generator_estimate_catalog_distinguishes_unknown_from_zero(tmp_path, metadata, expected):
    path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(path.read_text())
    manifest["metadata"] = {"action_declaration": {"kind": "generator"}, **metadata}
    path.write_text(json.dumps(manifest))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.register()
    record = host.capabilities["test.echo"]
    assert (record.estimated_scratch_bytes, record.estimated_output_bytes) == expected
    assert (record.manifest()["estimated_scratch_bytes"], record.manifest()["estimated_output_bytes"]) == expected
    row = runtime.registrations[-1][1]["capabilities"][0]
    known = None not in expected
    assert record.ready is known
    assert row["status"] == ("ready" if known else "unavailable")
    if known:
        assert row["unavailable_reason"] is None
        assert (row["estimated_scratch_bytes"], row["estimated_output_bytes"]) == expected
    else:
        # HC-04's existing catalog schema requires integers even for unavailable
        # rows. Their status/reason, not numeric placeholders, carry absence.
        assert "missing or invalid declared storage estimate" in row["unavailable_reason"]
        assert record.preflight["storage_estimate"]["ok"] is False


def test_non_estimated_action_keeps_legacy_catalog_behavior(tmp_path):
    _write_manifest(tmp_path / "echo")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.register()
    record = host.capabilities["test.echo"]
    assert record.ready
    assert record.estimated_scratch_bytes == 1
    assert record.estimated_output_bytes is None
    row = runtime.registrations[-1][1]["capabilities"][0]
    assert row["status"] == "ready"
    assert row["estimated_scratch_bytes"] == 1
    assert row["estimated_output_bytes"] == 0


@pytest.mark.parametrize("budget", [None, {"scratch_bytes": 0, "output_bytes": 0},
                                    {"scratch_bytes": 7, "output_bytes": 386233}])
def test_unknown_generator_estimate_refuses_before_dispatch(tmp_path, monkeypatch, budget):
    path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(path.read_text())
    manifest["metadata"] = {"action_declaration": {"kind": "generator"}}
    path.write_text(json.dumps(manifest))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    dispatch = []
    monkeypatch.setattr(host, "_run_command_definition", lambda *a, **kw: dispatch.append(kw))
    task = {"id": "unknown-estimate", "capability": "test.echo", "attempt_id": "attempt-unknown", "fence": 1}
    if budget is not None:
        task["storage_estimate"] = budget
    with pytest.raises(HostError, match="missing or invalid declared storage estimate"):
        host.run_task(task, lease_token="lease-unknown")
    assert dispatch == []
    assert runtime.settlements == []
    assert runtime.uploaded_objects == {}
    assert len(runtime.failures) == 1
    assert runtime.failures[0][3]["retryable"] is False


@pytest.mark.parametrize("budget", [0, 386232])
def test_j030_observed_output_sizes_still_refuse_underestimated_envelope(tmp_path, budget):
    output = tmp_path / "outputs"
    output.mkdir()
    # Sizes from preserved J030 evidence; no provider execution or source output
    # reproduction. Include the manifest even though it is not a media port.
    for name, size in {"music.wav": 384044, "beats.json": 696, "manifest.json": 1493}.items():
        with (output / name).open("wb") as handle:
            handle.truncate(size)
    with pytest.raises(HostError, match=f"staged output bytes 386233 exceed task output limit {budget}"):
        _task_storage_envelope(
            {"storage_estimate": {"scratch_bytes": 0, "output_bytes": budget}},
            tmp_path,
            [{"path": str(output / "music.wav")}, {"path": str(output / "beats.json")}],
        )


@pytest.mark.parametrize("budget", [None, {"scratch_bytes": 7, "output_bytes": 10},
                                    {"scratch_bytes": 0, "output_bytes": 0}])
def test_known_generator_requires_its_declared_task_estimate(tmp_path, monkeypatch, budget):
    path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(path.read_text())
    manifest["metadata"] = {"action_declaration": {"kind": "generator"},
                            "estimated_scratch_bytes": 7, "estimated_output_bytes": 11}
    path.write_text(json.dumps(manifest))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    dispatch = []
    monkeypatch.setattr(host, "_run_command_definition", lambda *a, **kw: dispatch.append(kw))
    task = {"id": "known-estimate", "capability": "test.echo", "attempt_id": "attempt-known", "fence": 1}
    if budget is not None:
        task["storage_estimate"] = budget
    with pytest.raises(HostError, match="requires .*storage_estimate"):
        host.run_task(task, lease_token="lease-known")
    assert dispatch == []
    assert runtime.settlements == []
    assert runtime.uploaded_objects == {}
    assert len(runtime.failures) == 1
    assert runtime.failures[0][3]["retryable"] is False


def test_existing_image_generator_estimate_remains_source_policy_bound():
    from astrid.core.generation.storage_policy import CLOUD_T2I_STORAGE_POLICY

    packs = Path(generic_host.__file__).resolve().parents[2] / "packs"
    host = GenericPackHost(pack_roots=[packs / "generation"])
    host.discover()
    record = host.capabilities["generation.generate_image"]
    assert record.requires_storage_estimate
    assert record.storage_estimate_unavailable_reason is None
    assert {"scratch_bytes": record.estimated_scratch_bytes,
            "output_bytes": record.estimated_output_bytes} == CLOUD_T2I_STORAGE_POLICY.estimate


@pytest.fixture
def request_estimator_host(tmp_path):
    """A synthetic producer; no chiptune synthesis or shared Runtime state."""
    from astrid.core.execution.storage_estimator import StorageEstimateRequest
    root = tmp_path / "synthetic"
    (root / "actions").mkdir(parents=True)
    source = root / "actions" / "estimate.py"
    source.write_text('''import json
def estimate(*, spec, input_facts):
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {"scratch_bytes": len(raw), "output_bytes": 2 + sum(f["size_bytes"] for f in input_facts)}
''')
    descriptor = {"schema_version": 1, "id": "synthetic.bytes", "version": 1,
                  "policy_version": "exact-json-v1", "path": "actions/estimate.py", "function": "estimate"}
    declaration = {"kind": "generator", "description": "Synthetic byte estimator",
                   "invocation": {"kind": "command", "command": {
        "argv": ["{python_exec}", "-c", "raise RuntimeError('provider must not execute')"]}},
        "inputs": {"type": "object", "properties": {"value": {"type": "string", "default": "default"},
                                                  "extras": {"type": "array"}},
                   "additionalProperties": True},
        "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt",
                     "artifact_type": "text/plain"}],
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"],
                     "storage_estimator": descriptor}}
    manifest = root / "pack.yaml"
    manifest.write_text(json.dumps({"schema_version": 3, "id": "synthetic", "name": "Synthetic",
                                   "version": "1.0.0", "actions": {"bytes": declaration}}))
    runtime = FakeRuntime()
    runtime.confirm_claim_authority = lambda **kw: generic_host._AuthenticatedAttempt(tuple(sorted(kw.items())))
    host = GenericPackHost(pack_roots=[root], client=runtime)
    host.discover()
    spec = {"capability_id": "synthetic.bytes", "params": {"value": "default", "extras": [{"raw": '雪\\"'}]},
            "output_policy": {"keep": "all"}, "raw_extra": [True, 1, None]}
    request = StorageEstimateRequest("synthetic.bytes", host.capabilities["synthetic.bytes"].capability_digest, 1, spec)
    return SimpleNamespace(host=host, runtime=runtime, root=root, source=source,
                           manifest=manifest, request=request, descriptor=descriptor)


def _estimator_task(world, pair):
    return {"id": "estimated", "attempt_id": "attempt-estimated", "fence": 1,
            "project_id": "synthetic-project", "capability": world.request.capability_id,
            "capability_digest": world.request.capability_digest,
            "spec": {"spec": world.request.spec, "schema_version": "1"}, "storage_estimate": dict(pair)}


def test_request_estimator_discovery_and_legacy_catalog_never_execute_code(request_estimator_host):
    w = request_estimator_host
    sentinel = w.root.parent / "imported"
    w.source.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n" + w.source.read_text())
    w.host.discover()
    w.host.register()
    assert not sentinel.exists()
    assert w.host.capabilities[w.request.capability_id].ready
    assert not w.host.capabilities[w.request.capability_id].manifest()["ready"]
    row = w.runtime.registrations[-1][1]["capabilities"][0]
    assert row["status"] == "unavailable"
    assert "unsupported by legacy catalog" in row["unavailable_reason"]
    assert "storage_estimate_contract" not in row


def test_request_estimator_preparation_preserves_canonical_spec_and_recomputes(request_estimator_host, monkeypatch):
    from dataclasses import replace

    from astrid.core.execution import storage_estimator as estimates
    from astrid.core.foundation.hash import canonical_json_digest
    w = request_estimator_host
    calls = []
    original = estimates.run_source_estimator
    def counted(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)
    monkeypatch.setattr(estimates, "run_source_estimator", counted)
    prepared = w.host.prepare_storage_estimate(w.request)
    assert prepared.request.spec == w.request.spec and prepared.request.spec is not w.request.spec
    assert prepared.spec_digest == "sha256:" + canonical_json_digest(w.request.spec)
    assert prepared.estimator["version"] == 1
    assert prepared.estimator["implementation_digest"].startswith("sha256:")
    assert w.host.capabilities[w.request.capability_id].request_storage_estimate_contract == {
        "mode": "request_estimated", "estimator": prepared.estimator}
    assert prepared.to_dict()["spec"] == w.request.spec
    assert w.host.verify_storage_preparation(w.request, prepared) == prepared
    assert len(calls) == 2
    forged = replace(prepared, storage_estimate={"scratch_bytes": 0, "output_bytes": 0})
    with pytest.raises(HostError, match="mismatch"):
        w.host.verify_storage_preparation(w.request, forged)
    assert len(calls) == 3  # Caller digest is identical, still recomputed/refused.
    assert not (w.root.parent / "attempt").exists()
    assert not w.runtime.heartbeats and not w.runtime.settlements


@pytest.mark.parametrize("change", ["params", "defaults", "output_policy", "raw_extra", "fact-id", "fact-size"])
def test_request_estimator_binding_covers_complete_spec_and_facts(request_estimator_host, change):
    import copy
    from dataclasses import replace
    w = request_estimator_host
    request = replace(w.request, input_facts=({"object_id": "sha256:" + "a" * 64, "size_bytes": 3},))
    prepared = w.host.prepare_storage_estimate(request)
    changed = copy.deepcopy(request)
    if change == "params":
        changed.spec["params"]["extras"].append({"escaped": "雪\n"})
    elif change == "defaults":
        changed.spec["params"]["value"] = "different-default"
    elif change == "output_policy":
        changed.spec["output_policy"]["keep"] = "primary"
    elif change == "raw_extra":
        changed.spec["raw_extra"].append(0)
    elif change == "fact-id":
        changed.input_facts[0]["object_id"] = "sha256:" + "b" * 64
    else:
        changed.input_facts[0]["size_bytes"] = 4
    with pytest.raises(HostError, match="mismatch"):
        w.host.verify_storage_preparation(changed, prepared)


@pytest.mark.parametrize("change", ["capability", "schema", "spec-capability", "missing-default", "nonfinite", "nonobject-spec"])
def test_request_estimator_invalid_request_never_imports(request_estimator_host, monkeypatch, change):
    import copy
    from dataclasses import replace

    from astrid.core.execution import storage_estimator as estimates
    w = request_estimator_host
    request = copy.deepcopy(w.request)
    if change == "capability":
        request = replace(request, capability_digest="sha256:" + "0" * 64)
    elif change == "schema":
        request = replace(request, schema_version=2)
    elif change == "spec-capability":
        request.spec["capability_id"] = "foreign.action"
    elif change == "missing-default":
        del request.spec["params"]["value"]
    elif change == "nonobject-spec":
        request = replace(request, spec=[])
    else:
        request.spec["params"]["extras"] = float("nan")
    monkeypatch.setattr(estimates, "run_source_estimator", lambda *a, **kw: pytest.fail("invalid request imported producer"))
    with pytest.raises((HostError, ValueError)):
        w.host.prepare_storage_estimate(request)


@pytest.mark.parametrize("change", ["source", "producer", "policy", "version", "function", "missing", "escape", "legacy"])
def test_request_estimator_source_and_declaration_changes_invalidate(request_estimator_host, change):
    w = request_estimator_host
    prepared = w.host.prepare_storage_estimate(w.request)
    if change == "source":
        w.source.write_text(w.source.read_text() + "\n# changed estimator implementation\n")
    elif change == "producer":
        (w.root / "actions" / "provider.py").write_text("PRODUCER_VERSION = 2\n")
    else:
        manifest = json.loads(w.manifest.read_text())
        descriptor = manifest["actions"]["bytes"]["metadata"]["storage_estimator"]
        if change == "policy":
            descriptor["policy_version"] = "v2"
        elif change == "version":
            descriptor["version"] = 2
        elif change == "function":
            descriptor["function"] = "missing"
        elif change == "missing":
            descriptor["path"] = "actions/missing.py"
        elif change == "escape":
            descriptor["path"] = "../outside.py"
        else:
            descriptor["schema_version"] = 0
        w.manifest.write_text(json.dumps(manifest))
    with pytest.raises(HostError, match="source digest changed"):
        w.host.verify_storage_preparation(w.request, prepared)
    w.host.discover()
    assert w.host.capabilities[w.request.capability_id].capability_digest != w.request.capability_digest
    with pytest.raises(HostError):
        w.host.verify_storage_preparation(w.request, prepared)


@pytest.mark.parametrize("delta", [-1, 1])
def test_request_estimator_rejects_smaller_and_larger_pairs_before_workspace(request_estimator_host, monkeypatch, delta):
    w = request_estimator_host
    pair = dict(w.host.prepare_storage_estimate(w.request).storage_estimate)
    pair["scratch_bytes"] += delta
    monkeypatch.setattr(w.host, "_allocate_attempt_root", lambda *a: pytest.fail("allocated workspace"))
    monkeypatch.setattr(w.host, "_run_command_definition", lambda *a, **kw: pytest.fail("provider invoked"))
    with pytest.raises(HostError, match="requires storage_estimate"):
        w.host.run_task(_estimator_task(w, pair), lease_token="lease")
    assert not w.runtime.heartbeats and not w.runtime.settlements
    assert len(w.runtime.failures) == 1


def test_request_estimator_exact_pair_and_spec_reach_existing_materialization(request_estimator_host, monkeypatch, tmp_path):
    w = request_estimator_host
    prepared = w.host.prepare_storage_estimate(w.request)
    task = _estimator_task(w, prepared.storage_estimate)
    w.runtime.tasks[task["id"]] = task
    allocated = []
    attempt = tmp_path / "private-attempt"
    def allocate(*args):
        allocated.append(args)
        attempt.mkdir()
        return attempt
    class BoundaryReached(Exception):
        pass
    original_materialize = w.host._materialize_inputs
    def materialize(spec, root, **kwargs):
        values = original_materialize(spec, root, **kwargs)
        assert values["extras"] == w.request.spec["params"]["extras"]
        assert values["value"] == "default"
        assert spec["spec"] is w.request.spec
        assert spec["spec"] == prepared.request.spec
        assert kwargs["storage_estimate"] == prepared.storage_estimate
        raise BoundaryReached()
    monkeypatch.setattr(w.host, "_allocate_attempt_root", allocate)
    monkeypatch.setattr(w.host, "_materialize_inputs", materialize)
    with pytest.raises(BoundaryReached):
        w.host.run_task(task, lease_token="lease")
    assert len(allocated) == 1
    assert not w.runtime.settlements and not w.runtime.uploaded_objects


@pytest.mark.parametrize("denial", ["actor", "project", "input"])
def test_request_estimator_denied_authority_never_imports_or_allocates(request_estimator_host, monkeypatch, denial):
    from astrid.core.execution import storage_estimator as estimates
    w = request_estimator_host
    task = _estimator_task(w, {"scratch_bytes": 1, "output_bytes": 2})
    if denial == "input":
        task["input_object_ids"] = ["sha256:" + "a" * 64]
    else:
        def denied(**kw):
            raise HostError(f"denied {denial}")
        w.runtime.confirm_claim_authority = denied
    monkeypatch.setattr(estimates, "run_source_estimator", lambda *a, **kw: pytest.fail("producer imported"))
    monkeypatch.setattr(w.host, "_allocate_attempt_root", lambda *a: pytest.fail("allocated workspace"))
    with pytest.raises(HostError, match="denied|input facts require"):
        w.host.run_task(task, lease_token="lease")
    assert not w.runtime.heartbeats and not w.runtime.settlements


@pytest.mark.parametrize("bad", [None, {}, {"scratch_bytes": True, "output_bytes": 1},
    {"scratch_bytes": "1", "output_bytes": 1}, {"scratch_bytes": -1, "output_bytes": 0},
    {"scratch_bytes": 1.5, "output_bytes": 0}, {"scratch_bytes": 9007199254740991, "output_bytes": 1}])
def test_request_estimator_invalid_pairs_fail_closed(request_estimator_host, bad):
    w = request_estimator_host
    w.source.write_text(f"def estimate(**kwargs):\n    return {bad!r}\n")
    w.host.discover()
    from dataclasses import replace
    request = replace(w.request, capability_digest=w.host.capabilities[w.request.capability_id].capability_digest)
    with pytest.raises(HostError):
        w.host.prepare_storage_estimate(request)


@pytest.mark.parametrize("side_effect", ["write", "network", "fork"])
def test_request_estimator_os_confinement_prevents_side_effects(request_estimator_host, side_effect):
    from dataclasses import replace
    w = request_estimator_host
    sentinel = w.root / "should-not-exist"
    actions = {"write": f"open({str(sentinel)!r}, 'w').write('bad')",
               "network": "__import__('socket').socket().connect(('127.0.0.1', 9))",
               "fork": "__import__('os').fork()"}
    w.source.write_text(f"def estimate(**kwargs):\n    {actions[side_effect]}\n    return {{'scratch_bytes': 1, 'output_bytes': 1}}\n")
    w.host.discover()
    request = replace(w.request, capability_digest=w.host.capabilities[w.request.capability_id].capability_digest)
    with pytest.raises(HostError):
        w.host.prepare_storage_estimate(request)
    assert not sentinel.exists()


@pytest.mark.parametrize("pair", [{"scratch_bytes": 0, "output_bytes": 0}, {"scratch_bytes": 7, "output_bytes": 11}])
def test_static_generator_exact_pair_reaches_workspace_boundary_unchanged(tmp_path, monkeypatch, pair):
    path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(path.read_text())
    manifest["metadata"] = {"action_declaration": {"kind": "generator"},
        "estimated_scratch_bytes": pair["scratch_bytes"], "estimated_output_bytes": pair["output_bytes"]}
    path.write_text(json.dumps(manifest))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    class BoundaryReached(Exception):
        pass
    def allocate(*args):
        raise BoundaryReached()
    monkeypatch.setattr(host, "_allocate_attempt_root", allocate)
    with pytest.raises(BoundaryReached):
        host.run_task({"id": "static", "capability": "test.echo", "attempt_id": "attempt-static",
                       "fence": 1, "storage_estimate": pair}, lease_token="lease-static")
    assert not runtime.failures and not runtime.settlements


@pytest.mark.parametrize("field", ["spec_digest", "source_digest", "estimator"])
def test_request_estimator_caller_binding_fields_are_untrusted(request_estimator_host, field):
    from dataclasses import replace
    w = request_estimator_host
    prepared = w.host.prepare_storage_estimate(w.request)
    value = {**prepared.estimator, "implementation_digest": "sha256:" + "0" * 64} if field == "estimator" else "0" * 64
    with pytest.raises(HostError, match="mismatch"):
        w.host.verify_storage_preparation(w.request, replace(prepared, **{field: value}))


@pytest.mark.parametrize("guard", ["deadline", "evidence"])
def test_request_estimator_existing_execution_guards_bound_helper(request_estimator_host, guard):
    from dataclasses import replace
    w = request_estimator_host
    if guard == "deadline":
        w.source.write_text("def estimate(**kwargs):\n    while True: pass\n")
        w.host.execution_policy = ExecutionGuardPolicy(deadline_seconds=0.5)
    else:
        w.source.write_text("def estimate(**kwargs):\n    print('x'*10000)\n    return {'scratch_bytes': 0, 'output_bytes': 0}\n")
        w.host.execution_policy = ExecutionGuardPolicy(evidence_cap_bytes=1024)
    w.host.discover()
    request = replace(w.request, capability_digest=w.host.capabilities[w.request.capability_id].capability_digest)
    with pytest.raises(HostError, match="deadline|evidence cap"):
        w.host.prepare_storage_estimate(request)
    assert not w.runtime.heartbeats and not w.runtime.settlements


@pytest.mark.parametrize("bad", [None, {"schema_version": 0}, "legacy_dynamic"])
def test_request_estimator_unsupported_declaration_is_unavailable(request_estimator_host, bad):
    w = request_estimator_host
    manifest = json.loads(w.manifest.read_text())
    manifest["actions"]["bytes"]["metadata"]["storage_estimator"] = bad
    # Static-looking zero cannot rescue unsupported dynamic metadata.
    manifest["actions"]["bytes"]["metadata"].update(estimated_scratch_bytes=0, estimated_output_bytes=0)
    w.manifest.write_text(json.dumps(manifest))
    w.host.discover()
    w.host.register()
    record = w.host.capabilities[w.request.capability_id]
    assert not record.ready
    row = w.runtime.registrations[-1][1]["capabilities"][0]
    assert row["status"] == "unavailable" and "unsupported" in row["unavailable_reason"]


@pytest.mark.parametrize("version", [None, 2, True, "2", 1])
def test_request_estimator_execution_rejects_unadmitted_schema(request_estimator_host, monkeypatch, version):
    from astrid.core.execution import storage_estimator as estimates
    w = request_estimator_host
    task = _estimator_task(w, {"scratch_bytes": 1, "output_bytes": 2})
    task["spec"]["schema_version"] = version
    monkeypatch.setattr(estimates, "run_source_estimator", lambda *a, **kw: pytest.fail("imported estimator"))
    monkeypatch.setattr(w.host, "_allocate_attempt_root", lambda *a: pytest.fail("allocated workspace"))
    with pytest.raises(HostError, match="schema version mismatch"):
        w.host.run_task(task, lease_token="lease")


@pytest.mark.parametrize("pair", [{"scratch_bytes": True, "output_bytes": 2},
                                  {"scratch_bytes": "1", "output_bytes": 2},
                                  {"scratch_bytes": 1.0, "output_bytes": 2}])
def test_request_estimator_caller_pairs_are_not_coerced(request_estimator_host, monkeypatch, pair):
    from astrid.core.execution import storage_estimator as estimates
    w = request_estimator_host
    monkeypatch.setattr(estimates, "run_source_estimator", lambda *a, **kw: pytest.fail("estimator invoked"))
    monkeypatch.setattr(w.host, "_allocate_attempt_root", lambda *a: pytest.fail("allocated workspace"))
    with pytest.raises(HostError, match="invalid or overflowing"):
        w.host.run_task(_estimator_task(w, pair), lease_token="lease")


def test_request_estimator_admitted_declaration_projects_exact_c_v2_descriptor(request_estimator_host):
    w = request_estimator_host
    health = w.runtime.health()
    from banodoco_workspace_client.generated import Health
    w.runtime.health = lambda: Health.from_json({**health, "runtime_session_id": "fixture-session",
                                               "capability_catalog_versions": [1, 2]})
    w.host.register()
    record = w.host.capabilities[w.request.capability_id]
    declaration = record.definition.metadata["action_declaration"]
    assert declaration["metadata"]["storage_estimator"] == record.definition.metadata["storage_estimator"]
    prepared = w.host.prepare_storage_estimate(w.request)
    row = w.runtime.registrations[-1][1]["capabilities"][0]
    assert row["storage_estimate_contract"] == {"mode": "request_estimated", "estimator": prepared.estimator}
    assert set(row["storage_estimate_contract"]["estimator"]) == {"id", "version", "implementation_digest"}
    assert row["definition_digest"] == w.request.capability_digest
    assert "storage_estimator" not in row["action_declaration"].get("metadata", {})
    assert "storage_estimator" in record.definition.metadata["action_declaration"]["metadata"]
    assert row["status"] == "ready"  # Authenticated v2 preserves actual source/preflight readiness.


def _chiptune_estimator_record():
    pack = Path(generic_host.__file__).resolve().parents[2] / "packs" / "chiptune"
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[pack], client=runtime)
    host.discover()
    return host, runtime, host.capabilities["chiptune.compose"]


def test_chiptune_request_estimator_registration_projects_only_the_public_identity():
    host, runtime, record = _chiptune_estimator_record()
    health = runtime.health()
    from banodoco_workspace_client.generated import Health
    runtime.health = lambda: Health.from_json({**health, "runtime_session_id": "fixture-session",
                                              "capability_catalog_versions": [1, 2]})

    host.register()

    row = next(row for row in runtime.registrations[-1][1]["capabilities"]
               if row["capability_id"] == "chiptune.compose")
    expected = {
        "mode": "request_estimated",
        "estimator": {
            "id": "chiptune.compose.storage",
            "version": 1,
            "implementation_digest": "sha256:8db66fca7c8d78a02cef2f50d310aa9d426f82b7755a54d4b15db42526fc5e22",
        },
    }
    assert row["storage_estimate_contract"] == expected
    assert set(row["storage_estimate_contract"]) == {"mode", "estimator"}
    assert set(row["storage_estimate_contract"]["estimator"]) == {
        "id", "version", "implementation_digest"}
    assert "storage_estimator" not in row["action_declaration"]["metadata"]
    assert "storage_estimator" in record.definition.metadata["action_declaration"]["metadata"]
    assert record.definition.metadata["storage_estimator"] == (
        record.definition.metadata["action_declaration"]["metadata"]["storage_estimator"])
    assert row["status"] == "ready"  # Runtime exposes it only with the live preparation binding.


@pytest.mark.parametrize("change", [
    "missing-mode", "malformed-mode", "missing-id", "malformed-id",
    "missing-version", "nonpositive-version", "malformed-version",
    "missing-implementation-digest", "malformed-implementation-digest",
])
def test_chiptune_request_estimator_identity_rejects_missing_or_malformed_fields(change):
    import copy
    from dataclasses import replace

    from astrid.core.execution.storage_estimator import StorageEstimatorError

    _host, _runtime, record = _chiptune_estimator_record()
    metadata = copy.deepcopy(record.definition.metadata)
    declaration = metadata["action_declaration"]
    raw = metadata["storage_estimator"]
    if change == "missing-mode":
        raw.pop("mode")
    elif change == "malformed-mode":
        raw["mode"] = "static"
    elif change == "missing-id":
        raw["estimator"].pop("id")
    elif change == "malformed-id":
        raw["estimator"]["id"] = ""
    elif change == "missing-version":
        raw["estimator"].pop("version")
    elif change == "nonpositive-version":
        raw["estimator"]["version"] = 0
    elif change == "malformed-version":
        raw["estimator"]["version"] = True
    elif change == "missing-implementation-digest":
        raw["estimator"].pop("implementation_digest")
    else:
        raw["estimator"]["implementation_digest"] = "sha256:bad"
    declaration["metadata"]["storage_estimator"] = copy.deepcopy(raw)
    invalid = replace(record, definition=replace(record.definition, metadata=metadata))

    with pytest.raises(StorageEstimatorError):
        _ = invalid.request_storage_estimate_contract


@pytest.mark.parametrize("change", ["spec", "pair"])
def test_request_estimator_existing_sdk_retry_keeps_prepared_request(request_estimator_host, change):
    """Reuse the existing SDK's in-memory transport double, no Runtime store."""
    import copy

    from astrid.sdk.remote import RemoteTasks
    from tests.stage1.test_invocation_runtime_cutover import _AdmissionTransport

    w = request_estimator_host
    prepared = w.host.prepare_storage_estimate(w.request)
    transport = _AdmissionTransport()
    transport.list_capabilities = lambda **kw: [[{
        "capability_id": w.request.capability_id, "definition_digest": w.request.capability_digest,
        "status": "ready"}], None]
    tasks = RemoteTasks(transport)
    arguments = dict(project_id="synthetic-project", capability=w.request.capability_id,
        capability_digest=w.request.capability_digest, spec=prepared.request.spec,
        storage_estimate=prepared.storage_estimate, idempotency_key="same-prepared-request")
    first = tasks.create(**arguments)
    again = tasks.create(**arguments)
    assert first.ok and again.ok and first.data == again.data
    assert len(transport.receipts) == 1
    assert transport.calls[0]["spec"] == prepared.request.spec
    assert transport.calls[0]["storage_estimate"] == prepared.storage_estimate
    changed = copy.deepcopy(arguments)
    if change == "spec":
        changed["spec"]["params"]["extras"].append({"raw": "changed"})
    else:
        changed["storage_estimate"]["output_bytes"] += 1
    with pytest.raises(RuntimeError, match="different input"):
        tasks.create(**changed)
    assert len(transport.receipts) == 1
    assert not w.runtime.heartbeats and not w.runtime.settlements


@pytest.mark.parametrize("module", ["entrypoint", "relative-helper", "relative-helper-symlink"])
def test_request_estimator_import_compiles_only_pinned_bytes(request_estimator_host, module):
    from astrid.core.execution.storage_estimator import (
        StorageEstimatorError,
        _invoke,
        estimator_descriptor,
    )

    w = request_estimator_host
    if module.startswith("relative-helper"):
        (w.root / "actions" / "helper.py").write_text("VALUE = 1\n")
        if module.endswith("symlink"):
            (w.root / "actions" / "replacement.py").write_text("raise RuntimeError('wrong logical source')\n")
        w.source.write_text("from .helper import VALUE\ndef estimate(**kwargs):\n    return {'scratch_bytes': VALUE, 'output_bytes': 0}\n")
        w.host.discover()
    descriptor = estimator_descriptor(w.host.capabilities[w.request.capability_id])
    # An external edit after inventory admission cannot execute even if the
    # whole tree is later restored before the final source fence check.
    files = {str(p.relative_to(w.root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in w.root.rglob("*.py")}
    sentinel = w.root.parent / "unadmitted-import"
    source = w.source if module == "entrypoint" else w.root / "actions" / "helper.py"
    if module.endswith("symlink"):
        source.unlink()
        source.symlink_to("replacement.py")
    else:
        source.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n")
    with pytest.raises(StorageEstimatorError, match="source bytes changed"):
        _invoke(w.root, descriptor, {"spec": w.request.spec, "input_facts": [], "source_files": files})
    assert not sentinel.exists()


def test_request_estimator_flat_reply_matches_current_a_c_j_contract(request_estimator_host):
    from astrid.core.execution.storage_estimator import preparation_digest
    from astrid.core.foundation.hash import canonical_json_digest

    w = request_estimator_host
    prepared = w.host.prepare_storage_estimate(w.request)
    reply = prepared.to_dict()
    assert set(reply) == {"capability_id", "capability_digest", "schema_version", "spec", "input_facts",
                          "spec_digest", "source_digest", "estimator", "storage_estimate", "preparation_digest"}
    assert reply["schema_version"] == 1 and type(reply["schema_version"]) is int
    assert reply["spec"] == w.request.spec and reply["input_facts"] == []
    assert reply["source_digest"] == "sha256:" + w.host.capabilities[w.request.capability_id].source_digest
    assert reply["spec_digest"] == "sha256:" + canonical_json_digest(w.request.spec)
    assert reply.pop("preparation_digest") == preparation_digest(prepared) == "sha256:" + canonical_json_digest(reply)


@pytest.mark.parametrize("count", [1, 1000])
def test_chiptune_declared_estimator_runs_in_existing_confined_helper(count):
    from astrid.core.execution.storage_estimator import StorageEstimateRequest
    from astrid.packs.chiptune.actions.compose.planner import estimate_compose_storage

    host, runtime, record = _chiptune_estimator_record()
    params = {"duration_s": 2, "bpm": 132, "key": "A minor", "seed": 1,
              "sections": [{"start_s": 0, "end_s": 2, "energy": 0.4,
                            "mood": "bright", "caller_note": (chr(38899) + chr(92) + chr(34)) * count}],
              "duck_db": -10, "master_db": -16, "style": "nes"}
    spec = {"family": record.id, "params": params, "output_policy": {}}
    request = StorageEstimateRequest(record.id, record.capability_digest, 1, spec)
    prepared = host.prepare_storage_estimate(request)
    expected = estimate_compose_storage(params)
    assert prepared.request.spec == spec
    assert prepared.storage_estimate == {"scratch_bytes": expected.scratch_bytes,
                                         "output_bytes": expected.output_bytes}
    assert prepared.estimator == record.request_storage_estimate_contract["estimator"]
    assert not runtime.heartbeats and not runtime.settlements
    host.shutdown()


def test_request_estimator_executes_resolved_interpreter_through_private_alias_chain(
    request_estimator_host, tmp_path, monkeypatch,
):
    import sys

    w = request_estimator_host
    binary = tmp_path / "private-bin"
    binary.mkdir()
    (binary / "python3").symlink_to(Path(sys.executable).resolve(strict=True))
    (binary / "python").symlink_to("python3")
    monkeypatch.setattr(sys, "executable", str(binary / "python"))
    prepared = w.host.prepare_storage_estimate(w.request)
    assert prepared.storage_estimate["scratch_bytes"] > 0
    assert prepared.storage_estimate["output_bytes"] == 2


@pytest.fixture
def retained_cpu_storage(tmp_path, monkeypatch):
    """Real pack prediction/serialization, with offline authenticated-owner seams."""
    import copy
    import runtime_protocol.verified_host_launch as launch
    from astrid.core.execution import storage_estimator as estimates
    from astrid.packs.chiptune.actions.compose import planner
    pack = Path(generic_host.__file__).resolve().parents[2] / 'packs/chiptune'
    host = GenericPackHost(pack_roots=[pack])
    host.discover()
    record = host.capabilities['chiptune.compose']
    params = {port.name: copy.deepcopy(port.default) for port in record.definition.inputs if port.default is not None}
    params.update(duration_s=2.0, sections=[{'start_s': 0.0, 'end_s': 2.0, 'energy': 0.6,
        'mood': 'bright', 'cadence_bars': 0}], hits=None, duck=None, vo_mask=None, mutes=None)
    request = estimates.StorageEstimateRequest(record.id, record.capability_digest, 1,
        {'family': record.id, 'params': params, 'output_policy': {}})
    installed = tmp_path / 'installed'
    installed.mkdir()
    launcher = tmp_path / 'launcher.py'
    launcher.write_text('# offline launch fixture\n')
    module = installed / 'astrid/core/fixture.py'
    module.parent.mkdir(parents=True)
    module.write_text('# admitted installed closure\n')
    retained = {'source_root': str(installed), 'host_files': {'astrid/core/fixture.py': 'pin'},
        'actions': {}, 'launcher': {'path': str(launcher), 'sha256': 'pin'}}
    selection = {'pack_id': 'chiptune', 'distributions': {}, 'digest': 'a' * 64}
    valid = [True]
    def selected(_):
        if not valid[0]:
            raise HostError('selected action profile or owner is stale')
        return retained, selection, {}
    monkeypatch.setattr(host, '_selected_action', selected)
    monkeypatch.setattr(launch, 'installed_host_closure', lambda *a, **kw: {'files': dict(retained['host_files'])})
    host._attempt_policy_factory = generic_host._AttemptPolicyFactory(host._executable_admission)
    bootstrap = tmp_path / 'bootstrap'
    bootstrap.mkdir()
    monkeypatch.setattr(host._attempt_policy_factory, 'bootstrap_inputs', lambda _: (
        (Path(sys.executable).resolve(),), (bootstrap,), (installed,), ()))
    # Only the existing confined estimator process is injected. The authentic
    # admitted descriptor/digest/request/source checks and pure pack code run.
    calls = []
    def estimator(_, descriptor, frozen, **kwargs):
        calls.append(descriptor)
        pair = planner.estimate_compose_storage(frozen.spec['params'])
        return {'scratch_bytes': pair.scratch_bytes, 'output_bytes': pair.output_bytes}
    monkeypatch.setattr(estimates, 'run_source_estimator', estimator)
    monkeypatch.setattr(host, '_allocate_attempt_root', lambda *a: pytest.fail('preparation allocated attempt'))
    monkeypatch.setattr(generic_host.subprocess, 'Popen', lambda *a, **kw: pytest.fail('preparation launched child'))
    return SimpleNamespace(host=host, record=record, request=request, retained=retained,
        selection=selection, valid=valid, calls=calls, planner=planner)


def test_retained_cpu_storage_preparation_composes_authentic_pack_once(retained_cpu_storage, tmp_path):
    w = retained_cpu_storage
    pack = w.planner.estimate_compose_storage(w.request.spec['params'])
    prepared, budget = w.host._prepare_storage_with_control(w.request)
    assert (pack.scratch_bytes, pack.output_bytes) == (1583, 386320)
    assert prepared.storage_estimate == {'scratch_bytes': pack.scratch_bytes + budget.scratch_bytes,
                                       'output_bytes': pack.output_bytes}
    assert prepared.estimator == w.record.request_storage_estimate_contract['estimator']
    assert w.host.verify_storage_preparation(w.request, prepared) == prepared
    assert len(w.calls) == 2
    assert not list(tmp_path.rglob('.renderer-policy-*'))
    for delta in (-1, 1, budget.scratch_bytes):
        forged = replace(prepared, storage_estimate={**prepared.storage_estimate,
            'scratch_bytes': prepared.storage_estimate['scratch_bytes'] + delta})
        with pytest.raises(HostError, match='mismatch'):
            w.host.verify_storage_preparation(w.request, forged)


@pytest.mark.parametrize('directory_length,closure_count', [(1, 1), (120, 9), (200, 3000)])
def test_retained_cpu_storage_bound_covers_serialized_path_and_closure_variation(
        retained_cpu_storage, tmp_path, monkeypatch, directory_length, closure_count):
    from astrid.core.execution.renderer_transport import RetainedRendererProfile
    w = retained_cpu_storage
    installed = Path(w.retained['source_root'])
    closure = installed / ('q' * directory_length)
    closure.mkdir()
    files = {}
    for i in range(closure_count):
        path = closure / f'module_{i}.py'
        path.write_text('# retained fixture\n')
        files[str(path.relative_to(installed))] = 'pin'
    w.selection['distributions'] = {'fixture': {'files': files}}
    prepared, budget = w.host._prepare_storage_with_control(w.request)
    attempt = tmp_path / ('a' * directory_length)
    attempt.mkdir()
    for part in ('inputs', 'outputs'):
        (attempt / part).mkdir()
    raw_files, roots, searches, excluded = w.host._attempt_policy_factory.bootstrap_inputs(w.record)
    policy = generic_host._ConfinementPolicy(attempt, attempt / 'outputs',
        generic_host._SandboxProjectScope(attempt / 'inputs', (attempt / 'inputs',)),
        raw_files, roots, generic_host._SandboxEnvironmentPolicy(), bootstrap_search_roots=searches,
        host_control_budget=budget)
    policy = generic_host._selected_action_policy(policy, w.retained,
        generic_host._selected_action_read_files(w.retained, w.selection))
    monkeypatch.setattr(generic_host.sys, 'platform', 'darwin')
    monkeypatch.setattr(generic_host.shutil, 'which', lambda _: '/usr/bin/sandbox-exec')
    plan = generic_host._confined_launch_plan([sys.executable], policy=policy)
    profile = plan.renderer_profile
    assert type(profile) is RetainedRendererProfile
    profile.validate()
    actual = profile.path.stat().st_size
    assert actual <= budget.scratch_bytes
    if closure_count == 3000:
        assert actual > 912950  # The originally failed policy is within this scale.
    assert prepared.storage_estimate['scratch_bytes'] == w.planner.estimate_compose_storage(
        w.request.spec['params']).scratch_bytes + budget.scratch_bytes
    _assert_live_storage_envelope(prepared.storage_estimate, attempt, attempt / 'outputs')
    _task_storage_envelope({'storage_estimate': prepared.storage_estimate}, attempt, [])
    before = sorted(attempt.rglob('*'))
    with pytest.raises(ValueError, match='byte/count/path bound'):
        RetainedRendererProfile.create(profile.path.read_text(), policy)
    assert sorted(attempt.rglob('*')) == before
    if closure_count == 1:
        # A genuine workload overrun must still fail with the composed budget,
        # including when the workload chooses a policy-looking filename.
        for kind, target, limit in (
                ('scratch', attempt / '.renderer-policy-workload',
                 prepared.storage_estimate['scratch_bytes'] - actual),
                ('output', attempt / 'outputs/.renderer-policy-workload',
                 prepared.storage_estimate['output_bytes'])):
            with target.open('wb') as stream:
                stream.truncate(limit + 1)
            with pytest.raises(HostError, match=kind):
                _assert_live_storage_envelope(prepared.storage_estimate, attempt, attempt / 'outputs')
            with pytest.raises(HostError, match=kind):
                _task_storage_envelope({'storage_estimate': prepared.storage_estimate}, attempt, [])
            target.unlink()


@pytest.mark.parametrize('fault', ['bytes', 'path'])
def test_retained_cpu_control_bound_refuses_before_materialization(tmp_path, monkeypatch, fault):
    from astrid.core.execution import renderer_transport as transport
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    budget = transport.HostControlStorageBudget(1 if fault == 'bytes' else 100,
        1 if fault == 'path' else 10000)
    policy = SimpleNamespace(attempt_root=attempt, host_control_budget=budget)
    monkeypatch.setattr(transport.tempfile, 'mkdtemp', lambda **kw: pytest.fail('allocated control directory'))
    with pytest.raises(ValueError, match='byte/count/path bound'):
        transport.RetainedRendererProfile.create('(deny default)\n', policy)
    assert list(attempt.iterdir()) == []


@pytest.mark.parametrize('fault', ['profile', 'closure', 'during_estimator'])
def test_retained_cpu_storage_drift_refuses(retained_cpu_storage, monkeypatch, fault):
    import runtime_protocol.verified_host_launch as launch
    from astrid.core.execution import storage_estimator as estimates
    w = retained_cpu_storage
    prepared = w.host.prepare_storage_estimate(w.request)
    if fault == 'profile':
        w.valid[0] = False
    elif fault == 'closure':
        monkeypatch.setattr(launch, 'installed_host_closure', lambda *a, **kw: {'files': {}})
    else:
        original = estimates.run_source_estimator
        def changed(*a, **kw):
            result = original(*a, **kw)
            w.valid[0] = False
            return result
        monkeypatch.setattr(estimates, 'run_source_estimator', changed)
    with pytest.raises(HostError, match='stale|changed'):
        w.host.verify_storage_preparation(w.request, prepared)


@pytest.mark.parametrize('fault', ['content', 'replacement', 'symlink', 'hardlink', 'mode', 'directory'])
def test_retained_cpu_profile_identity_fences_remain_offline(tmp_path, fault):
    from astrid.core.execution.renderer_transport import RetainedRendererProfile
    attempt = tmp_path / 'attempt'
    output = attempt / 'outputs'
    output.mkdir(parents=True)
    policy = SimpleNamespace(attempt_root=attempt, host_control_budget=None,
        project=SimpleNamespace(write_roots=()), private_write_root=output)
    profile = RetainedRendererProfile.create('(version 1)\n(deny default)\n', policy)
    path = profile.path
    if fault == 'directory':
        path.parent.chmod(0o755)
    elif fault == 'mode':
        path.chmod(0o600)
    elif fault == 'hardlink':
        os.link(path, path.with_name('alias'))
    elif fault in ('replacement', 'symlink'):
        path.rename(path.with_name('original'))
        if fault == 'symlink':
            path.symlink_to(path.with_name('original'))
        else:
            path.write_text('(version 1)\n(deny default)\n')
            path.chmod(0o400)
    else:
        path.chmod(0o600)
        path.write_text('(allow default)\n')
        path.chmod(0o400)
    with pytest.raises((ValueError, OSError)):
        profile.validate()


def test_retained_cpu_profile_workload_write_scope_remains_forbidden(tmp_path):
    from astrid.core.execution.renderer_transport import RetainedRendererProfile
    policy = SimpleNamespace(attempt_root=tmp_path, host_control_budget=None,
        project=SimpleNamespace(write_roots=()), private_write_root=tmp_path)
    with pytest.raises(ValueError, match='overlaps workload writes'):
        RetainedRendererProfile.create('(deny default)\n', policy)
    assert not list(tmp_path.glob('.renderer-policy-*'))


@pytest.mark.parametrize('kind', ['scratch', 'output'])
@pytest.mark.parametrize('phase', ['live', 'final'])
def test_policy_named_workload_storage_still_overruns(tmp_path, kind, phase):
    attempt = tmp_path / 'attempt'
    output = attempt / 'outputs'
    output.mkdir(parents=True)
    root = output if kind == 'output' else attempt
    (root / '.renderer-policy-workload').write_bytes(b'ordinary workload bytes')
    pair = {'scratch_bytes': 0, 'output_bytes': 0}
    with pytest.raises(HostError, match=kind):
        if phase == 'live':
            _assert_live_storage_envelope(pair, attempt, output)
        else:
            _task_storage_envelope({'storage_estimate': pair}, attempt, [])


@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_retained_cpu_admission_recomputes_whole_pair_before_allocation(
        retained_cpu_storage, tmp_path, monkeypatch, delta):
    w = retained_cpu_storage
    runtime = FakeRuntime()
    runtime.confirm_claim_authority = lambda **kw: generic_host._AuthenticatedAttempt(tuple(sorted(kw.items())))
    w.host.client = runtime
    pair = dict(w.host.prepare_storage_estimate(w.request).storage_estimate)
    pair['scratch_bytes'] += delta
    runtime.tasks['estimated'] = _estimator_task(w, pair)
    class BoundaryReached(Exception):
        pass
    if delta:
        with pytest.raises(HostError, match='requires storage_estimate'):
            w.host.run_task(_estimator_task(w, pair), lease_token='lease')
        assert runtime.failures and not runtime.settlements
    else:
        attempt = tmp_path / 'admitted-attempt'
        def allocate(*a):
            attempt.mkdir()
            (attempt / 'inputs').mkdir()
            (attempt / 'outputs').mkdir()
            return attempt
        monkeypatch.setattr(w.host, '_allocate_attempt_root', allocate)
        monkeypatch.setattr(w.host._attempt_policy_factory, 'build', lambda *a, **kw:
            generic_host._ConfinementPolicy(attempt, attempt / 'outputs',
                generic_host._SandboxProjectScope(attempt / 'inputs', (attempt / 'inputs',)),
                (), (), generic_host._SandboxEnvironmentPolicy()))
        def materialize(*a, **kw):
            control = w.host._confinement_policy.host_control_budget
            assert control.scratch_bytes + w.planner.estimate_compose_storage(
                w.request.spec['params']).scratch_bytes == pair['scratch_bytes']
            assert kw['storage_estimate'] == pair
            raise BoundaryReached()
        monkeypatch.setattr(w.host, '_materialize_inputs', materialize)
        with pytest.raises(BoundaryReached):
            w.host.run_task(_estimator_task(w, pair), lease_token='lease')
    assert not runtime.uploaded_objects and not runtime.settlements
    assert bool(runtime.heartbeats) is (delta == 0)  # Successful admission renews its lease.


def test_offline_request_preparation_source_drift_still_refuses(request_estimator_host, monkeypatch):
    from astrid.core.execution import storage_estimator as estimates
    w = request_estimator_host
    monkeypatch.setattr(estimates, 'run_source_estimator', lambda *a, **kw:
        {'scratch_bytes': 10, 'output_bytes': 20})
    prepared = w.host.prepare_storage_estimate(w.request)
    w.source.write_text(w.source.read_text() + '\n# drift\n')
    with pytest.raises(HostError, match='source digest changed'):
        w.host.verify_storage_preparation(w.request, prepared)


@pytest.fixture
def selected_renderer_schema(tmp_path, monkeypatch):
    from tests.core.execution.test_remotion_closure_binding import roots, sha
    from astrid.core.execution import remotion_closure, host_renderer_selection, declared_executables
    from astrid.core.rendering import remotion_runtime
    project, installed, receipt, arguments = roots.__wrapped__(tmp_path, monkeypatch)
    # C's installed renderer owns its modules directly, as required by the
    # existing Runtime-input containment check.
    (project / "node_modules").unlink()
    (installed / "node_modules").rename(project / "node_modules")
    installed = project
    arguments["installed_project"] = project
    for name in remotion_runtime._REQUIRED_PACKAGES:
        package = installed / "node_modules/@banodoco" / name
        package.mkdir(parents=True, exist_ok=True)
        (package / "package.json").write_text('{"version":"fixture"}')
        if name == "timeline-composition":
            (project / "vendor/timeline-composition/package.json").write_bytes((package / "package.json").read_bytes())
    schema = installed / "node_modules/@banodoco/timeline-schema/python"
    package = schema / "banodoco_timeline_schema"
    package.mkdir(parents=True)
    for name in remotion_runtime._REQUIRED_SCHEMA_FILES:
        (package / name).write_text("{}" if name.endswith(".json") else "# selected schema fixture\n")
    patches = {}
    for relative in remotion_closure._RETAINED_TRANSPORT_PATCHES:
        path = installed / "node_modules/@remotion/renderer/dist" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("disposable retained transport " + relative)
        patches[relative] = sha(path)
    monkeypatch.setattr(remotion_closure, "_RETAINED_TRANSPORT_PATCHES", patches)
    binding = remotion_closure.RemotionClosureBinding.admit(**arguments)
    node = declared_executables.ExecutableBinding(
        "node", declared_executables.FilePin.measure(Path(sys.executable).resolve()), (), (), tuple(os.uname()))
    selected = host_renderer_selection.HostRendererSelection(binding, node)
    # Only executable inspection is injected; selection activation and every
    # closure inventory/hash/containment check use the production validators.
    inspected = []
    def inspect(name, path):
        inspected.append((name, path))
        assert (name, path) == ("node", node.executable.path)
        return node
    monkeypatch.setattr(declared_executables, "_bind", inspect)
    site = tmp_path / "site-packages"
    site.mkdir()
    launch = {"source_checkout": str(site), "renderer_selection": selected.payload()}
    return SimpleNamespace(selected=selected, schema=schema, package=package, launch=launch,
                           arguments=arguments, inspected=inspected)


def test_selected_renderer_schema_projection_uses_retained_project_without_sdk_schema(selected_renderer_schema, monkeypatch):
    from astrid.core.rendering import remotion_runtime
    world = selected_renderer_schema
    assert not (Path(world.launch["source_checkout"]) / "banodoco_timeline_schema").exists()
    for name in ("ASTRID_REMOTION_PROJECT_DIR", "ASTRID_NODE_EXECUTABLE", "ASTRID_TIMELINE_SCHEMA_PYTHONPATH"):
        monkeypatch.setenv(name, "/unselected/ambient")
    selected, environment = generic_host._activate_selected_renderer_environment(world.launch)
    assert type(selected) is type(world.selected)
    assert selected.closure == world.selected.closure
    assert world.inspected == [("node", selected.node.executable.path)]
    # Exercise the actual Runtime input verifier that refused C's SDK-site
    # projection. It hashes this offline fixture; it launches no native process.
    inputs = remotion_runtime._runtime_inputs(selected.closure.project, environment, sys.executable, {})
    assert inputs["schema"] == str(world.schema)
    assert Path(environment[remotion_runtime.TIMELINE_SCHEMA_PYTHONPATH_ENV]).resolve() == world.schema
    assert inputs["configured_project"] == str(selected.closure.project)
    assert inputs["configured_node"] == str(selected.node.executable.path)


@pytest.mark.parametrize("fault", [
    "missing-admitted", "missing-after-activation", "changed-after-activation",
    "escaping-file", "escaping-package", "project-link", "unbound-schema",
    "substituted-project", "substituted-closure", "unbound-selection",
])
def test_selected_renderer_schema_projection_refuses_unbound_or_changed_inputs(selected_renderer_schema, monkeypatch, tmp_path, fault):
    from astrid.core.execution import host_renderer_selection, remotion_closure
    world = selected_renderer_schema
    value = dict(world.launch["renderer_selection"])
    target = world.package / "generated.py"
    if fault == "missing-admitted":
        target.unlink()
        binding = remotion_closure.RemotionClosureBinding.admit(**world.arguments)
        value = replace(world.selected, closure=binding).payload()
    elif fault == "unbound-schema":
        binding = replace(world.selected.closure, files=tuple(
            (path, digest) for path, digest in world.selected.closure.files if path != target))
        # Model a retained selection whose schema file is absent from its pins;
        # inventory validation must refuse, even though the file exists.
        value["closure_sha256"] = host_renderer_selection._digest([(str(p), h) for p, h in binding.files])
    elif fault == "substituted-project":
        value["project"] = str(tmp_path)
    elif fault == "substituted-closure":
        value["installed_project"] = str(tmp_path)
    elif fault == "unbound-selection":
        value["closure_sha256"] = "0" * 64
    else:
        activate = host_renderer_selection.activate_renderer_selection
        def activate_then_drift(payload):
            selected = activate(payload)
            if fault == "missing-after-activation":
                target.unlink()
            elif fault == "changed-after-activation":
                target.write_text("# changed selected bytes")
            elif fault == "escaping-file":
                outside = tmp_path / "foreign-schema.py"
                outside.write_bytes(target.read_bytes())
                target.unlink(); target.symlink_to(outside)
            elif fault == "escaping-package":
                outside = tmp_path / "foreign-package"
                world.package.rename(outside)
                world.package.symlink_to(outside, target_is_directory=True)
            else:
                link = selected.closure.project / "node_modules"
                link.rename(tmp_path / "original-modules")
                link.symlink_to(tmp_path, target_is_directory=True)
            return selected
        monkeypatch.setattr(host_renderer_selection, "activate_renderer_selection", activate_then_drift)
    with pytest.raises((ValueError, HostError, OSError, KeyError)):
        generic_host._activate_selected_renderer_environment({**world.launch, "renderer_selection": value})


@pytest.mark.parametrize("launch", [{}, {"source_checkout": "/sdk/site"},
                                      {"selected_actions": [{"action_id": "fixture"}]}])
def test_selected_renderer_absence_preserves_launch_environment(monkeypatch, launch):
    from astrid.core.execution import host_renderer_selection
    monkeypatch.setattr(host_renderer_selection, "activate_renderer_selection",
                        lambda *_: pytest.fail("no renderer selection to activate"))
    before = dict(os.environ)
    assert generic_host._activate_selected_renderer_environment(launch) == (None, {})
    assert dict(os.environ) == before


@pytest.fixture(params=["generation.generate_speech", "chiptune.compose"])
def selected_package_data(tmp_path, monkeypatch, request):
    """Offline selected file pins; real pack shape, projection and serializer."""
    from runtime_protocol import verified_host_launch as gate
    action_id = request.param
    pack_id = action_id.split(".")[0]
    source = Path(generic_host.__file__).resolve().parents[3]
    pack_root = source / "astrid/packs" / pack_id
    host = GenericPackHost(pack_roots=[pack_root])
    record = generic_host._launch_static_records((pack_root,))[action_id]
    installed = tmp_path / "installed"
    fixture_host = installed / "astrid/core/fixture.py"
    fixture_host.parent.mkdir(parents=True)
    fixture_host.write_text("# retained Host fixture\n")
    launcher = tmp_path / "launcher.py"
    launcher.write_text("# retained launcher fixture\n")
    retained = {"source_root": str(installed), "host_files": {}, "actions": {},
                "launcher": {"path": str(launcher), "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest()}}
    selection = {"pack_id": pack_id, "action_id": action_id, "distributions": {}}
    w = SimpleNamespace(host=host, retained=retained, selection=selection)
    monkeypatch.setattr(gate, "installed_host_closure", lambda *a, **kw: {"files": dict(retained["host_files"])})
    host._attempt_policy_factory = generic_host._AttemptPolicyFactory(host._executable_admission)
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    monkeypatch.setattr(host._attempt_policy_factory, "bootstrap_inputs", lambda _: (
        (Path(sys.executable).resolve(),), (bootstrap,), (installed,), ()))
    monkeypatch.setattr(generic_host.subprocess, "Popen", lambda *a, **kw: pytest.fail("offline fixture launched child"))
    package = installed / "jsonschema_specifications"
    leaf = package / "schemas/draft202012/vocab"
    leaf.mkdir(parents=True)
    data = leaf / "core.json"
    data.write_text('{"selected":"nested package data"}\n')
    module = package / "__init__.py"
    module.write_text("# selected dependency fixture\n")
    chosen = installed / "astrid/packs" / pack_id / "actions/selected.py"
    chosen.parent.mkdir(parents=True)
    chosen.write_text("# selected action fixture\n")
    sibling = leaf / "unselected.json"
    sibling.write_text('{"unselected":true}')
    other_pack = installed / "astrid/packs/unselected/actions/run.py"
    other_pack.parent.mkdir(parents=True)
    other_pack.write_text("# another action's source\n")
    ambient = tmp_path / "ambient-site/unselected/__init__.py"
    ambient.parent.mkdir(parents=True)
    ambient.write_text("# ambient code\n")
    for path in (data, module, chosen, other_pack):
        w.retained["host_files"][str(path.relative_to(installed))] = hashlib.sha256(path.read_bytes()).hexdigest()
    # All selected fixture files carry real pins. Host-source/owner transport
    # is injected as in retained_cpu_storage; file identity uses the unchanged
    # Runtime-owned reader, also called by the genuine child bootstrap.
    fixture_host = installed / "astrid/core/fixture.py"
    w.retained["host_files"][str(fixture_host.relative_to(installed))] = hashlib.sha256(fixture_host.read_bytes()).hexdigest()
    launcher = Path(w.retained["launcher"]["path"])
    w.retained["launcher"]["sha256"] = hashlib.sha256(launcher.read_bytes()).hexdigest()
    w.selection.update(pack_id=pack_id, action_id=action_id)
    measurements = []
    def selected(record):
        retained, selection, context = w.retained, w.selection, {}
        for raw, pin in generic_host._selected_action_read_files(retained, selection).items():
            measurements.append(raw)
            gate._local_read(Path(raw), pin)
        return retained, selection, context
    monkeypatch.setattr(w.host, "_selected_action", selected)
    attempt = tmp_path / "materialized-attempt"
    for part in ("inputs", "outputs"):
        (attempt / part).mkdir(parents=True)
    files, roots, searches, excluded = w.host._attempt_policy_factory.bootstrap_inputs(record)
    policy = generic_host._ConfinementPolicy(attempt, attempt / "outputs",
        generic_host._SandboxProjectScope(attempt / "inputs", (attempt / "inputs",)),
        files, (*roots, installed), generic_host._SandboxEnvironmentPolicy(),
        bootstrap_search_roots=searches, excluded_python_roots=excluded)
    return SimpleNamespace(host=w.host, retained=w.retained, selection=w.selection, record=record,
        installed=installed, package=package, leaf=leaf, data=data, sibling=sibling,
        other_pack=other_pack, ambient=ambient, launcher=launcher, policy=policy, measurements=measurements)


def test_selected_package_data_ancestry_is_literal_and_budgeted(selected_package_data, monkeypatch):
    w = selected_package_data
    projected = []
    serialize = generic_host._serialize_confinement_profile
    def capture(policy, **kwargs):
        result = serialize(policy, **kwargs)
        projected.append((policy, result))
        return result
    monkeypatch.setattr(generic_host, "_serialize_confinement_profile", capture)
    budget = w.host._host_control_storage_bound(w.record)
    predicted, prediction = projected.pop()
    read_files = generic_host._selected_action_read_files(w.retained, w.selection)
    # This is a finite projection from pinned file paths, never a tree walk.
    with monkeypatch.context() as no_scan:
        no_scan.setattr(Path, "iterdir", lambda *_: pytest.fail("projection scanned a directory"))
        no_scan.setattr(Path, "rglob", lambda *_: pytest.fail("projection scanned a tree"))
        policy = generic_host._selected_action_policy(w.policy, w.retained, read_files)
    ancestry = (w.installed, w.package, w.package / "schemas",
                w.package / "schemas/draft202012", w.leaf)
    # Launcher contributes only its existing immediate parent, never upward
    # enumeration through a non-installed Runtime/interpreter location.
    added = set(policy.bootstrap_search_roots) - set(w.policy.bootstrap_search_roots)
    assert all(p.is_relative_to(w.installed) or p == w.launcher.parent for p in added)
    monkeypatch.setattr(generic_host.sys, "platform", "darwin")
    monkeypatch.setattr(generic_host.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    policy = replace(policy, host_control_budget=budget)
    plan = generic_host._confined_launch_plan([sys.executable], policy=policy)
    materialized, profile = projected.pop()
    assert plan.renderer_profile.path.read_text() == profile
    plan.renderer_profile.validate()
    assert len(prediction.encode()) == budget.scratch_bytes
    assert len(profile.encode()) <= budget.scratch_bytes
    for path in ancestry:
        rule = f'(allow file-read* (literal "{path}"))'
        assert rule in prediction and rule in profile
        assert f'(allow file-map-executable (literal "{path}"))' not in profile
    assert set(ancestry) <= set(policy.bootstrap_search_roots)
    assert set(ancestry) <= set(predicted.bootstrap_search_roots)
    for raw, pin in read_files.items():
        assert f'(allow file-read* (literal "{raw}"))' in profile
        assert hashlib.sha256(Path(raw).read_bytes()).hexdigest() == pin
    assert w.measurements  # Original owner measurement ran during prediction.
    for path in (w.sibling, w.other_pack, w.ambient):
        assert f'(allow file-read* (literal "{path}"))' not in profile
        assert not any(f'(allow file-read* (subpath "{p}"))' in profile for p in (path, *path.parents))
        assert f'(allow file-map-executable (literal "{path}"))' not in profile
    for directory in ancestry:
        assert f'(allow file-read* (subpath "{directory}"))' not in profile
        assert f'(allow file-write* (subpath "{directory}"))' not in profile
        assert f'(allow file-map-executable (subpath "{directory}"))' not in profile
    # Removing only the formerly omitted enumeration literals changes the
    # serialized Host-control bytes, proving prediction includes the fix.
    old_searches = set(w.policy.bootstrap_search_roots) | {Path(p).parent for p in read_files}
    omitted = set(ancestry) - old_searches
    assert w.package / "schemas" in omitted
    prior = "\n".join(line for line in prediction.split("\n") if line not in {
        f'(allow file-read* (literal "{p}"))' for p in omitted})
    assert len(prediction.encode()) > len(prior.encode())
    assert not any(line.startswith("(allow network") for line in profile.splitlines())


@pytest.mark.parametrize("fault", ["traversal", "escaping-file", "escaping-directory", "not-directory", "root-alias", "changed-pin"])
def test_selected_package_data_ancestry_refuses_invalid_selection(selected_package_data, tmp_path, fault):
    from runtime_protocol import verified_host_launch as gate
    w = selected_package_data
    reads = generic_host._selected_action_read_files(w.retained, w.selection)
    if fault == "changed-pin":
        w.data.write_text('{"changed":true}')
        with pytest.raises(gate.HostLaunchError, match="differs from its reviewed pin"):
            w.host._host_control_storage_bound(w.record)
        assert str(w.data) in w.measurements
        return
    if fault == "traversal":
        reads[str(w.installed / "../outside.json")] = "0" * 64
    elif fault == "escaping-file":
        outside = tmp_path / "outside.json"
        outside.write_bytes(w.data.read_bytes())
        w.data.unlink(); w.data.symlink_to(outside)
    elif fault in {"escaping-directory", "not-directory"}:
        original = tmp_path / "original-data"
        w.leaf.rename(original)
        if fault == "escaping-directory":
            w.leaf.symlink_to(original, target_is_directory=True)
        else:
            w.leaf.write_text("not a directory")
    else:
        moved = tmp_path / "original-installed"
        w.installed.rename(moved)
        w.installed.symlink_to(moved, target_is_directory=True)
    with pytest.raises(HostError):
        generic_host._selected_action_policy(w.policy, w.retained, reads)
