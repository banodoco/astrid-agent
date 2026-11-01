from __future__ import annotations

import dataclasses
import hashlib
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
from astrid.core.execution.generic_host import (
    AdapterRegistry,
    CleanupUncertainError,
    CommandCleanupUnresolved,
    GenericPackHost,
    HostCancelled,
    HostError,
    HostRegistrationError,
    RuntimeProtocolClient,
    _assert_live_storage_envelope,
    _attempt_tree_bytes,
    _completed_process_evidence,
    _managed_asset_identity,
    _storage_tree_bytes,
    _task_storage_envelope,
    _terminate_process_group,
)
from astrid.core.execution.guards import ExecutionGuardPolicy


@pytest.fixture(autouse=True)
def _release_handoff_journal_locks(monkeypatch):
    """Close the process-lifetime flock fd each preparation keeps on its journal.

    A real host owns that lock until the process exits; these tests build a
    preparation per case inside one pytest process, so each must be released at
    teardown (closing the descriptor drops the flock).
    """
    acquired = []
    original = generic_host.LocalExecutionPreparation._acquire_handoff_journal

    def tracked(self, scope):
        acquired.append(self)
        return original(self, scope)

    monkeypatch.setattr(generic_host.LocalExecutionPreparation, "_acquire_handoff_journal", tracked)
    yield
    for preparation in acquired:
        descriptor, preparation._handoff_lock_fd = preparation._handoff_lock_fd, None
        if descriptor is not None:
            os.close(descriptor)


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

    def refresh_local_connection(self):
        return False

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
        assert child_env["ASTRID_RUNTIME_ENDPOINT"] == "http://127.0.0.1:8765"
        assert child_env["ASTRID_RUNTIME_CREDENTIAL"] == "worker-token"
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


def test_manifest_credential_missing_from_all_sources_does_not_block_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_credential_manifest(tmp_path)
    monkeypatch.setenv("ASTRID_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("FAL_KEY", raising=False)
    host = GenericPackHost(pack_roots=[tmp_path], credential_source={})
    record = host.discover()[0]
    host.preflight(record.id)
    record = host.capabilities[record.id]
    assert record.ready is True
    assert record.preflight["credentials"] == {"ok": True, "missing": ["FAL_KEY"]}
    child_env, secrets = host._child_environment(record, tmp_path / "attempt")
    assert "FAL_KEY" not in child_env
    assert not secrets


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
    port_digest = hashlib.sha256(b"image_ref").hexdigest()[:16]
    assert staged == tmp_path / "attempt" / "inputs" / f"managed-{port_digest}-{digest}.jpg"
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
    image_port = hashlib.sha256(b"reference_image_ref").hexdigest()[:16]
    video_port = hashlib.sha256(b"driving_video_ref").hexdigest()[:16]
    assert image_path == tmp_path / "attempt-ordered" / "inputs" / f"managed-{image_port}-{image_digest}.bin"
    assert video_path == tmp_path / "attempt-ordered" / "inputs" / f"managed-{video_port}-{video_digest}.bin"
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


def test_managed_asset_identity_accepts_runtime_canonical_film_media_id():
    media_id = "sha256:95fc75f6ef87b32177308d0bb34a699f07a1960c443edc630377d1dec88f0602"
    canonical_asset = {
        "media_id": media_id,
        "thumbnailUrl": "/api/runtime/v1/media/sha256:thumbnail",
        "generationId": "generation-film",
    }
    assert _managed_asset_identity("film", canonical_asset) == (media_id, media_id.removeprefix("sha256:"))
    assert _managed_asset_identity("film", {**canonical_asset, "content_sha256": media_id}) == (
        media_id,
        media_id.removeprefix("sha256:"),
    )
    bare_digest = media_id.removeprefix("sha256:")
    assert _managed_asset_identity("film", {"media_id": bare_digest}) == (bare_digest, bare_digest)


def test_managed_asset_identity_keeps_locator_rejection_and_explicit_opaque_digest():
    media_id = "opaque-runtime-media-id"
    digest = hashlib.sha256(b"managed test object").hexdigest()
    assert _managed_asset_identity("asset", {"media_id": media_id, "digest": digest}) == (media_id, digest)
    locator_values = {
        "url": "/api/runtime/v1/objects/sha256%3A" + "0" * 64,
        "uri": "file:///Users/example/film.mov",
        "path": "/Users/example/film.mov",
        "source_path": "/Users/example/film.mov",
        "locator": "/api/runtime/v1/objects/sha256%3A" + "0" * 64,
        "realm": "file:///Users/example/film.mov",
        "file": "/api/runtime/v1/objects/sha256%3A" + "0" * 64,
    }
    for key, value in locator_values.items():
        with pytest.raises(HostError, match="unmanaged locator"):
            _managed_asset_identity("film", {
                "media_id": "sha256:" + "0" * 64,
                key: value,
            })
    with pytest.raises(HostError, match="unmanaged locator"):
        _managed_asset_identity("film", {
            "media_id": "sha256:" + "0" * 64,
            "file": "file:///Users/example/film.mov",
        })
    with pytest.raises(HostError, match="requires object_id and digest"):
        _managed_asset_identity("asset", {"media_id": media_id})


def test_materialize_inputs_fetches_media_id_only_registry_entry(tmp_path):
    payload = b"canonical managed film fixture"
    digest = hashlib.sha256(payload).hexdigest()
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"assets": {"film": {
        "media_id": digest,
        "thumbnailUrl": "/api/runtime/v1/media/thumbnail-id",
        "generationId": "generation-film",
        "type": "video/mp4",
    }}}), encoding="utf-8")

    class Objects(FakeRuntime):
        def get_object(self, object_digest):
            assert object_digest == digest
            return payload

    host = GenericPackHost(pack_roots=[tmp_path], client=Objects())
    values = host._materialize_inputs({
        "input_object_ids": [digest],
        "spec": {"inputs": {"assets_registry": str(registry)}},
    }, tmp_path / "attempt", authorized_input_object_ids=[digest])
    derived = json.loads(Path(values["assets_registry"]).read_text(encoding="utf-8"))
    asset = derived["assets"]["film"]
    assert Path(asset["file"]).read_bytes() == payload
    assert asset["media_id"] == digest
    assert asset["thumbnailUrl"] == "/api/runtime/v1/media/thumbnail-id"
    assert asset["generationId"] == "generation-film"


def test_command_cwd_must_stay_in_attempt_or_source_scope(tmp_path):
    _write_manifest(tmp_path / "echo", cwd=str(tmp_path / "outside"))
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    record = host.capabilities["test.echo"]
    with pytest.raises(HostError, match="cwd escapes"):
        host._run_command_definition(record, {}, tmp_path / "attempt" / "outputs", tmp_path / "attempt")


def _descendant_still_running(pid: int) -> bool:
    """A zombie has exited and only awaits reaping by its parent, so it is not running.

    Orphaned descendants are reparented to init; where init does not reap
    (containers without a reaper) a terminated descendant stays a zombie that
    ``os.kill(pid, 0)`` still reports as present.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


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
        if not _descendant_still_running(child_pid):
            break
        time.sleep(0.05)
    else:
        pytest.fail("descendant survived cancellation")


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


@pytest.mark.posix_native  # POSIX-only process-group API
def test_cleanup_does_not_signal_a_reused_group_after_leader_exit(monkeypatch, owned_process_groups):
    """An exited leader PID must never be reused as a killpg target."""
    process = owned_process_groups.popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait()

    def unexpected_killpg(*_args):
        raise AssertionError("cleanup signaled a group after its leader exited")

    monkeypatch.setattr(os, "killpg", unexpected_killpg)
    monkeypatch.setattr(os, "kill", unexpected_killpg)
    # An unobserved raw Popen cannot certify its descendants after root exit.
    # Refusal is required; leader exit is not a cleanup-custody substitute.
    with pytest.raises(CleanupUncertainError, match="ancestry capture"):
        _terminate_process_group(process)


@pytest.mark.posix_native  # POSIX-only process-group API
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


@pytest.mark.posix_native  # POSIX-only process-group API
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


@pytest.mark.parametrize(("bad_metadata", "bad_matrix"), [
    ({"adapter_family": "unregistered-family"}, {}),
    ({"adapter_family": "cpu", "requires_remotion": "sometimes"}, {}),
    ({"adapter_family": "cpu"}, {"resource_keys": "provider"}),
])
def test_bad_adapter_is_registered_unavailable_without_changing_admission_digests(tmp_path, bad_metadata, bad_matrix):
    bad_root = tmp_path / "bad"
    _write_manifest(bad_root, capability_id="test.bad")
    bad_manifest = json.loads((bad_root / "executor.yaml").read_text(encoding="utf-8"))
    bad_manifest["metadata"].update(bad_metadata)
    (bad_root / "executor.yaml").write_text(json.dumps(bad_manifest), encoding="utf-8")
    good_root = tmp_path / "good"
    _write_manifest(good_root, capability_id="test.good")
    matrix_rows = []
    for capability_id in ("test.bad", "test.good"):
        row = {
            "id": capability_id,
            "disposition": "optional",
            "evidence_reason": "fixture capability row",
            "adapter_family": bad_metadata.get("adapter_family", "cpu") if capability_id == "test.bad" else "cpu",
            "resource_keys": ["cpu"],
            "required_binaries": [],
            "required_packages": [],
        }
        if capability_id == "test.bad":
            row.update(bad_matrix)
        matrix_rows.append(row)
    matrix_path = tmp_path / "matrix.json"
    expected_matrix = {row["id"]: dict(row) for row in matrix_rows}
    matrix_path.write_text(json.dumps({"schema_version": 1, "capabilities": matrix_rows}), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], capability_matrix=matrix_path, client=runtime)

    before = {record.id: (record.capability_digest, record.source_digest) for record in host.discover()}
    source_epoch_before = host.source_epoch
    result = host.register()

    rows = {row["capability_id"]: row for row in runtime.registrations[0][1]["capabilities"]}
    assert rows["test.bad"]["status"] == "unavailable"
    assert rows["test.bad"]["required_resource_keys"] == []
    assert rows["test.bad"]["unavailable_reason"].startswith("adapter:reason=")
    assert rows["test.good"]["status"] == "ready"
    manifests = {item["id"]: item for item in result["capabilities"]}
    assert manifests["test.bad"]["adapter_family"] is None
    assert manifests["test.bad"]["adapter_error"]
    assert (manifests["test.bad"]["capability_digest"], manifests["test.bad"]["source_digest"]) == before["test.bad"]
    assert host.capabilities["test.bad"].matrix == expected_matrix["test.bad"]
    assert host.matrix == expected_matrix
    assert host.source_epoch == source_epoch_before
    with pytest.raises(HostError, match="unavailable: adapter:reason="):
        host.admit("executor", "test.bad")

    local_result = GenericPackHost(pack_roots=[tmp_path], capability_matrix=matrix_path).register()
    local_bad = next(item for item in local_result["capabilities"] if item["id"] == "test.bad")
    assert local_bad["adapter_family"] is None and local_bad["adapter_error"]


def test_replaced_capability_record_reparses_current_adapter_matrix(tmp_path):
    _write_manifest(tmp_path / "good", capability_id="test.replace")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    original = host.discover()[0]

    invalid = dataclasses.replace(original, matrix={"adapter_family": "invalid-family"})
    host.capabilities[original.id] = invalid
    host.register()
    assert runtime.registrations[-1][1]["capabilities"][0]["status"] == "unavailable"
    assert host.capabilities[original.id].resource_keys == ()
    host.capabilities[original.id] = dataclasses.replace(invalid, matrix={})
    host.register(deliberate=True)
    assert runtime.registrations[-1][1]["capabilities"][0]["status"] == "ready"
    assert host.admit("executor", original.id)[0]["id"] == original.id


def test_invalid_capability_matrix_carries_only_safe_startup_context(tmp_path, caplog):
    from astrid.core.execution import generic_host

    matrix = tmp_path / "capabilities.json"
    matrix.write_text('{"secret":"private-token-should-not-appear"', encoding="utf-8")
    with pytest.raises(HostError) as failure:
        GenericPackHost._load_matrix(matrix)

    with caplog.at_level("ERROR", logger=generic_host.__name__):
        generic_host._log_cli_exit_failure(failure.value)

    assert "field=capability_matrix" in caplog.text
    assert str(matrix) in caplog.text
    assert "private-token-should-not-appear" not in caplog.text


def test_malformed_network_policy_is_a_capability_scoped_registration_and_admission_error(tmp_path):
    bad_root = tmp_path / "network_bad"
    _write_manifest(bad_root, capability_id="test.network_bad")
    bad_manifest = json.loads((bad_root / "executor.yaml").read_text(encoding="utf-8"))
    bad_manifest["isolation"] = {"mode": "subprocess", "network": True}
    bad_manifest["metadata"].update({"adapter_family": "provider", "network_policy": ["not", "an object"]})
    (bad_root / "executor.yaml").write_text(json.dumps(bad_manifest), encoding="utf-8")
    good_root = tmp_path / "network_good"
    _write_manifest(good_root, capability_id="test.network_good")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)

    host.register()

    rows = {row["capability_id"]: row for row in runtime.registrations[0][1]["capabilities"]}
    assert rows["test.network_bad"]["status"] == "unavailable"
    assert "network:reason=network_policy" in rows["test.network_bad"]["unavailable_reason"]
    assert rows["test.network_good"]["status"] == "ready"
    with pytest.raises(HostError, match="unavailable: network:reason=network_policy"):
        host.admit("executor", "test.network_bad")


def test_remotion_status_exception_only_unreadies_remotion_capability(tmp_path, monkeypatch):
    from astrid.core.rendering import remotion_runtime

    render_root = tmp_path / "render"
    _write_manifest(render_root, capability_id="test.render")
    definition = json.loads((render_root / "executor.yaml").read_text(encoding="utf-8"))
    definition["metadata"].update({"adapter_family": "render", "requires_remotion": True})
    (render_root / "executor.yaml").write_text(json.dumps(definition), encoding="utf-8")
    cpu_root = tmp_path / "cpu"
    _write_manifest(cpu_root, capability_id="test.cpu")

    def broken_status(*, require_explicit_project=False):
        raise RuntimeError("opaque project detail")

    monkeypatch.setattr(remotion_runtime, "remotion_runtime_status", broken_status)
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)

    host.register()

    rows = {row["capability_id"]: row for row in runtime.registrations[0][1]["capabilities"]}
    assert rows["test.render"]["status"] == "unavailable"
    assert "Remotion validation failed: opaque project detail" in rows["test.render"]["unavailable_reason"]
    assert rows["test.cpu"]["status"] == "ready"
    assert host.capabilities["test.cpu"].ready


def test_shared_host_integrity_error_from_remotion_stays_fatal(tmp_path, monkeypatch):
    from astrid.core.rendering import remotion_runtime

    render_root = tmp_path / "render"
    _write_manifest(render_root, capability_id="test.render")
    definition = json.loads((render_root / "executor.yaml").read_text(encoding="utf-8"))
    definition["metadata"].update({"adapter_family": "render", "requires_remotion": True})
    (render_root / "executor.yaml").write_text(json.dumps(definition), encoding="utf-8")

    def broken_status(*, require_explicit_project=False):
        raise HostError("host identity integrity failed")

    monkeypatch.setattr(remotion_runtime, "remotion_runtime_status", broken_status)
    with pytest.raises(HostError, match="host identity integrity failed"):
        GenericPackHost(pack_roots=[tmp_path], client=FakeRuntime()).register()


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
    unready_root = tmp_path / "missing_binary"
    unready_root.mkdir()
    unready_definition = json.loads((root / "executor.yaml").read_text(encoding="utf-8"))
    unready_definition["id"] = "fixture.missing_binary"
    unready_definition["isolation"]["binaries"] = ["astrid-test-missing-binary"]
    (unready_root / "executor.yaml").write_text(json.dumps(unready_definition), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    host.preflight()

    assert host.capabilities["fixture.required_env"].ready is True
    assert host.capabilities["fixture.missing_binary"].ready is False
    assert host.claim_once() is None
    assert runtime.claim_payload["capability_ids"] == ["fixture.required_env"]
    assert host.capabilities["fixture.required_env"].preflight["credentials"]["missing"] == ["ASTRID_PROVIDER_KEY"]

    # Rotation updates diagnostics on the next claim cycle without changing
    # the admitted candidate set.
    monkeypatch.setenv("ASTRID_PROVIDER_KEY", "fixture-secret")
    assert host.claim_once() is None
    assert runtime.claim_payload["capability_ids"] == ["fixture.required_env"]
    assert host.capabilities["fixture.required_env"].preflight["credentials"]["missing"] == []


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
    from astrid_runtime_client import ApiError

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
    from astrid_runtime_client import ApiError

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

    monkeypatch.setattr("astrid_runtime_client.WorkspaceClient", WorkerGenerated)
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

    monkeypatch.setattr("astrid_runtime_client.WorkspaceClient", WorkerGenerated)
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
    from astrid_runtime_client import ApiError, WorkspaceClient

    captured = {}

    def timed_out(request, *, timeout):
        captured["timeout"] = timeout
        captured["request_id"] = request.get_header("X-request-id")
        raise TimeoutError("fixture timeout")

    monkeypatch.setattr("astrid_runtime_client.generated._urlopen_loopback", timed_out)

    with pytest.raises(ApiError) as failure:
        WorkspaceClient("http://127.0.0.1:8765", timeout=0.125).health()

    assert captured["timeout"] == 0.125
    assert captured["request_id"].startswith("request-")
    assert failure.value.code == "transport_timeout"
    assert failure.value.request_id == captured["request_id"]
    assert failure.value.details == {}


def test_vendored_runtime_client_sends_its_credential_to_loopback_only(monkeypatch):
    from astrid_runtime_client import WorkspaceClient

    sent = []
    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda opener, request, data=None, timeout=None: sent.append(request))

    with pytest.raises(ValueError, match="refusing to send a runtime credential to non-loopback host 'runtime.example'"):
        WorkspaceClient("http://runtime.example:8765", token="secret").health()

    assert sent == []


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_vendored_runtime_client_rejects_unbounded_timeout(timeout):
    from astrid_runtime_client import WorkspaceClient

    with pytest.raises(ValueError, match="finite and positive"):
        WorkspaceClient("http://127.0.0.1:8765", timeout=timeout)


def test_vendored_runtime_client_preserves_server_failure_diagnostic():
    from astrid_runtime_client import ApiError, WorkspaceClient

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
    from astrid_runtime_client import ApiError
    from astrid_runtime_client.contract_metadata import SCHEMA_DIGEST
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

        def __init__(self, _endpoint, _credential, *, credential_file=None):
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

    monkeypatch.setattr("astrid_runtime_client.WorkspaceClient", WorkerGenerated)
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

    monkeypatch.setattr("astrid_runtime_client.WorkspaceClient", WorkerGenerated)
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


def test_run_accepts_at_free_disk_floor_without_task_estimate(tmp_path, monkeypatch):
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

    monkeypatch.delenv("ASTRID_RUNTIME_FREE_DISK_FLOOR_BYTES", raising=False)
    monkeypatch.setattr(
        "astrid.core.execution.generic_host.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1024**3),
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


def test_output_capture_diagnostics_do_not_exhaust_task_evidence(tmp_path):
    manifest_path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["command"]["argv"][2] += "; import sys; print('stdout log'); print('stderr log', file=sys.stderr)"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(
        pack_roots=[tmp_path], client=runtime,
        execution_policy=ExecutionGuardPolicy(evidence_cap_bytes=2),
    )
    host.discover()
    task = {
        "task": {
            "id": "task-output-capture",
            "capability": "test.echo",
            "project_id": "demo",
            "attempt_id": "attempt-output-capture",
            "fence": 1,
            "spec": {"spec": {"inputs": {}}},
        }
    }
    runtime.tasks["task-output-capture"] = task

    result = host.run_task(task, lease_token="lease-output-capture")

    assert result["task"]["status"] == "completed"
    assert runtime.failures == []
    evidence = runtime.settlements[0][2]["result"]["execution_guards"]["evidence"]
    assert evidence["observed_bytes"] == 2
    assert evidence["largest_paths"] == [
        {"path": "outputs/answer.txt", "bytes": 2, "classification": "generated"}
    ]


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


def test_explicit_task_storage_envelope_rejects_output_overrun_and_cleans_up(tmp_path):
    _write_manifest(tmp_path / "echo")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
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


def test_render_uses_free_space_through_execution_evidence_and_settlement(tmp_path, monkeypatch):
    manifest_path = _write_manifest(tmp_path / "echo", capability_id="rendering.render")
    manifest = json.loads(manifest_path.read_text())
    manifest["command"]["argv"][-1] = (
        "from pathlib import Path; "
        "root=Path('{out}').parent; "
        "(root/'.remotion-runtime-fixture').mkdir(); "
        "(root/'.remotion-runtime-fixture'/'frame.jpeg').write_bytes(b'x'*3*1024**2); "
        "Path('{out}/answer.txt').write_bytes(b'y'*3*1024**2)"
    )
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(
        "astrid.core.execution.generic_host.shutil.disk_usage",
        lambda _: SimpleNamespace(free=8 * 1024**3),
    )
    runtime = FakeRuntime()
    host = GenericPackHost(
        pack_roots=[tmp_path], client=runtime,
        execution_policy=ExecutionGuardPolicy(evidence_cap_bytes=2 * 1024**2),
    )
    host.discover()
    task = {"task": {
        "id": "render-over-estimate", "capability": "rendering.render", "project_id": "demo",
        "attempt_id": "render-over-estimate-attempt", "fence": 1,
        "storage_estimate": {"scratch_bytes": 1, "output_bytes": 1},
        "spec": {"spec": {"inputs": {}}},
    }}
    runtime.tasks["render-over-estimate"] = task
    host.run_task(task, lease_token="render-lease")
    assert len(runtime.settlements) == 1
    assert runtime.failures == []


def test_live_storage_overrun_preserves_bounded_scratch_diagnostic_before_cleanup(tmp_path):
    manifest_path = _write_manifest(tmp_path / "echo")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["command"]["argv"] = [
        "{python_exec}",
        "-c",
        (
            "from pathlib import Path; import time; "
            "root=Path('{out}').parent; "
            "(root/'.remotion-runtime-fixture').mkdir(); "
            "(root/'managed-objects'/'source.bin').write_bytes(b'x'*7); "
            "(root/'.remotion-runtime-fixture'/'frame.bin').write_bytes(b'x'*80); "
            "time.sleep(2)"
        ),
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
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
        "path": ".remotion-runtime-fixture/frame.bin",
        "bytes": 80,
        "classification": "renderer_workspace",
    }
    assert not list(tmp_path.glob("astrid-attempt-*"))


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
    assert render.capabilities["rendering.render"].estimated_scratch_bytes == 0
    assert render.capabilities["rendering.render"].estimated_output_bytes == 0


def test_render_is_ready_without_remotion_and_reports_remotion_separately(monkeypatch):
    monkeypatch.delenv("ASTRID_REMOTION_PROJECT_DIR", raising=False)
    monkeypatch.delenv("ASTRID_NODE_EXECUTABLE", raising=False)
    monkeypatch.delenv("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", raising=False)
    # V3 exports render from the pack declaration, not an executor folder.
    pack_root = Path(generic_host.__file__).resolve().parents[2] / "packs" / "rendering"
    host = GenericPackHost(pack_roots=[pack_root])
    host.discover()
    assert host.capabilities["rendering.render"].manifest_path == pack_root / "pack.yaml"
    assert host.capabilities["rendering.render"].adapter.remotion == "selected"

    host.preflight("rendering.render")

    record = host.capabilities["rendering.render"]
    # Remotion gates only the tasks that render with it; media-only renders go on.
    assert record.ready is True
    assert record.preflight["remotion"]["ok"] is False
    assert "ASTRID_REMOTION_PROJECT_DIR" in record.preflight["remotion"]["reason"]
    assert record.preflight["remotion"]["gates"] == "tasks that render with rendering.remotion"
    assert generic_host._gated_remotion_faults([record]) == {"rendering.render": record.preflight["remotion"]["reason"]}


def test_missing_source_remotion_install_registers_actionable_reasons(tmp_path, monkeypatch):
    from astrid.core.rendering import remotion_runtime
    from astrid.sdk.host_bootstrap import _provision_render_runtime_env

    source = tmp_path / "checkout"
    project = source / "remotion"
    project.mkdir(parents=True)
    (project / "package.json").write_text(json.dumps({"engines": {"node": "=20.19.4"}}))
    child_env = {"PATH": ""}
    _provision_render_runtime_env(source, child_env)
    assert remotion_runtime.REMOTION_PROJECT_DIR_ENV not in child_env
    monkeypatch.setattr(remotion_runtime, "REPO_ROOT", source)
    monkeypatch.delenv(remotion_runtime.REMOTION_PROJECT_DIR_ENV, raising=False)
    packs = tmp_path / "packs"
    for capability_id, requires_remotion in (
        ("test.selected", "selected"), ("test.always", True), ("test.offline", False)
    ):
        path = _write_manifest(packs / capability_id, capability_id=capability_id)
        manifest = json.loads(path.read_text())
        manifest["metadata"].update(adapter_family="cpu", requires_remotion=requires_remotion)
        path.write_text(json.dumps(manifest))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[packs], client=runtime)

    host.register()

    registered = {row["capability_id"]: row for row in runtime.registrations[0][1]["capabilities"]}
    row = registered["test.always"]
    assert row["status"] == "unavailable"
    assert row["unavailable_reason"].startswith("remotion:reason=")
    assert str(project / "node_modules") in row["unavailable_reason"]
    assert f"run `npm ci` in {project} with Node v20.19.4 on PATH" in row["unavailable_reason"]
    # A capability whose Remotion requirement follows the task's renderer is
    # advertised ready; the actionable reason is still reported, per task.
    for capability_id in ("test.selected", "test.offline"):
        assert registered[capability_id]["status"] == "ready"
        assert registered[capability_id]["unavailable_reason"] is None
    check = host.capabilities["test.selected"].preflight["remotion"]
    assert check["ok"] is False and check["gates"] == "tasks that render with rendering.remotion"
    assert str(project / "node_modules") in check["reason"]
    assert generic_host._gated_remotion_faults(host.capabilities.values()) == {"test.selected": check["reason"]}


def test_unavailable_reason_reports_the_gpu_gate_not_the_task_gated_remotion_check():
    """A failed GPU gate unreadies a capability; a gated Remotion failure alone never does."""
    gated = {"ok": False, "reason": "remotion is broken", "gates": "tasks that render with rendering.remotion"}
    gpu_gate = {"ok": False, "qualified": False, "reason": "no qualified NVIDIA GPU"}
    both = SimpleNamespace(id="test.render", ready=False, preflight={"remotion": gated, "gpu": gpu_gate}, matrix={})
    assert generic_host._preflight_unavailable_reason(both) == "no qualified NVIDIA GPU"
    only_remotion = SimpleNamespace(id="test.render", ready=True, preflight={"remotion": gated}, matrix={})
    assert generic_host._preflight_unavailable_reason(only_remotion) == "capability preflight is not ready"
    assert generic_host._gated_remotion_faults([only_remotion, both]) == {"test.render": "remotion is broken"}


REMOTION_FAULT = "fixture Remotion install is broken"


def _selected_render_host(tmp_path, monkeypatch, *, remotion_ok: bool, argv_exit: int = 0, stderr: str = "") -> SimpleNamespace:
    """A leased ``remotion == 'selected'`` render fake whose child records its argv."""
    from astrid.core.rendering import remotion_runtime

    argv_file = tmp_path / "child-argv.json"
    code = (
        "import json, sys; from pathlib import Path; "
        f"Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:])); "
        f"sys.stderr.write({stderr!r}); "
        "Path('{out}/answer.txt').write_text('ok'); "
        f"sys.exit({argv_exit})"
    )
    root = tmp_path / "packs" / "render"
    root.mkdir(parents=True)
    (root / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "test.render", "name": "Render", "kind": "external", "version": "1.0",
        "inputs": [{"name": "selector", "type": "string", "required": False}],
        "command": {
            "argv": ["{python_exec}", "-c", code],
            "input_args": [{"input": "selector", "flag": "--selector", "optional": True}],
        },
        "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt", "artifact_type": "text/plain"}],
        "metadata": {"resource_keys": ["cpu"], "estimated_scratch_bytes": 1,
                     "adapter_family": "cpu", "requires_remotion": "selected"},
    }), encoding="utf-8")

    def status(*, require_explicit_project=False):
        return remotion_runtime.RemotionRuntimeStatus(
            remotion_ok, tmp_path, reason=None if remotion_ok else REMOTION_FAULT)

    monkeypatch.setattr(remotion_runtime, "remotion_runtime_status", status)
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path / "packs"], client=runtime)
    host.register()

    def run(selector=None, *, name="task-1"):
        inputs = {} if selector is None else {"selector": selector}
        task = {"task": {"id": name, "capability": "test.render", "project_id": "demo",
                         "attempt_id": f"attempt-{name}", "fence": 1, "spec": {"spec": {"inputs": inputs}}}}
        runtime.tasks[name] = task
        return host.run_task(task, lease_token=f"lease-{name}")

    def child_argv():
        return json.loads(argv_file.read_text()) if argv_file.exists() else None

    return SimpleNamespace(host=host, runtime=runtime, run=run, child_argv=child_argv)


def test_a_task_selecting_remotion_fails_at_lease_with_the_remotion_reason(tmp_path, monkeypatch):
    world = _selected_render_host(tmp_path, monkeypatch, remotion_ok=False)
    # The capability is advertised ready, so the Runtime admits the task.
    row = world.runtime.registrations[0][1]["capabilities"][0]
    assert (row["status"], row["unavailable_reason"]) == ("ready", None)

    with pytest.raises(HostError, match=r"rendering\.remotion, which this host cannot run: " + REMOTION_FAULT):
        world.run("rendering.remotion")

    assert world.child_argv() is None  # no child was launched
    assert not world.runtime.settlements
    task_id, _lease, error, details = world.runtime.failures[0]
    assert task_id == "task-1" and details["retryable"] is False
    assert "test.render: this task renders with rendering.remotion, which this host cannot run" in error
    assert REMOTION_FAULT in error


def test_an_omitted_selector_renders_with_ffmpeg_when_remotion_is_unavailable(tmp_path, monkeypatch):
    world = _selected_render_host(tmp_path, monkeypatch, remotion_ok=False)

    world.run()

    assert world.child_argv() == ["--selector", "rendering.ffmpeg"]
    result = world.runtime.settlements[0][2]["result"]
    assert result["renderer_selection"] == {
        "selector": "rendering.ffmpeg", "chosen_by": "host", "remotion_ok": False, "remotion_reason": REMOTION_FAULT,
    }


def test_an_omitted_selector_renders_with_remotion_when_available(tmp_path, monkeypatch):
    world = _selected_render_host(tmp_path, monkeypatch, remotion_ok=True)

    world.run()

    assert world.child_argv() == ["--selector", "rendering.remotion"]
    result = world.runtime.settlements[0][2]["result"]
    assert result["renderer_selection"] == {
        "selector": "rendering.remotion", "chosen_by": "host", "remotion_ok": True, "remotion_reason": None,
    }


@pytest.mark.parametrize("remotion_ok", [True, False])
def test_an_explicit_ffmpeg_task_runs_whatever_the_remotion_state(tmp_path, monkeypatch, remotion_ok):
    world = _selected_render_host(tmp_path, monkeypatch, remotion_ok=remotion_ok)

    world.run("rendering.ffmpeg")

    assert world.child_argv() == ["--selector", "rendering.ffmpeg"]
    selection = world.runtime.settlements[0][2]["result"]["renderer_selection"]
    assert (selection["selector"], selection["chosen_by"], selection["remotion_ok"]) == (
        "rendering.ffmpeg", "task", remotion_ok)


def test_a_timeline_ffmpeg_refuses_still_fails_with_both_reasons_when_the_host_chose_ffmpeg(tmp_path, monkeypatch):
    # A timeline that needs Remotion (here: ffmpeg refuses it) still requires
    # it: the host chose ffmpeg only because Remotion is unavailable, so the
    # settled failure names ffmpeg's reasons and the Remotion fault.
    refusal = "rendering.ffmpeg does not support this render request: transitions need rendering.remotion"
    world = _selected_render_host(tmp_path, monkeypatch, remotion_ok=False, argv_exit=3, stderr=refusal)

    with pytest.raises(HostError):
        world.run()

    error = world.runtime.failures[0][2]
    assert refusal in error
    assert "chose rendering.ffmpeg because rendering.remotion is unavailable on this host: " + REMOTION_FAULT in error
    assert not world.runtime.settlements


@pytest.mark.parametrize(("remotion_ok", "selector"), [(True, None), (False, "rendering.ffmpeg")])
def test_a_failed_render_is_not_blamed_on_remotion_unless_the_host_chose_ffmpeg_for_it(
    tmp_path, monkeypatch, remotion_ok, selector
):
    world = _selected_render_host(tmp_path, monkeypatch, remotion_ok=remotion_ok, argv_exit=3, stderr="render refused")

    with pytest.raises(HostError):
        world.run(selector)

    error = world.runtime.failures[0][2]
    assert "render refused" in error
    assert "unavailable on this host" not in error


@pytest.mark.parametrize(("declared", "expected"), [(True, "always"), (False, "never"), ("selected", "selected")])
def test_requires_remotion_accepts_only_true_false_or_selected(tmp_path, declared, expected):
    path = _write_manifest(tmp_path / "echo")
    definition = GenericPackHost(pack_roots=[tmp_path]).discover()[0].definition
    for value in (None, "true", "false", "always", "never", "Selected", 1, 0, [], {}):
        with pytest.raises(HostError, match="requires_remotion must be true, false or 'selected'"):
            AdapterRegistry.from_matrix(definition, {"requires_remotion": value})
        bad = dataclasses.replace(definition, metadata={**definition.metadata, "requires_remotion": value})
        with pytest.raises(HostError, match="requires_remotion must be true, false or 'selected'"):
            AdapterRegistry.resolve(bad)
    assert AdapterRegistry.from_matrix(definition, {"requires_remotion": declared}).remotion == expected
    declared_in_pack = dataclasses.replace(definition, metadata={**definition.metadata, "requires_remotion": declared})
    assert AdapterRegistry.resolve(declared_in_pack).remotion == expected
    # Absent means never, and the pack's declaration survives a matrix that is silent about it.
    assert AdapterRegistry.resolve(definition).remotion == "never"
    assert AdapterRegistry.from_matrix(definition, {}).remotion == "never"
    assert AdapterRegistry.from_matrix(declared_in_pack, {}).remotion == expected
    assert path.is_file()


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
    assert "credentials" not in reason_by_check
    assert host.capabilities["required.provider"].preflight["credentials"]["missing"] == ["ASTRID_TEST_PROVIDER_KEY"]
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
@pytest.mark.posix_native  # POSIX-only process-group API
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
    graph.engine = SimpleNamespace(role_authority=SimpleNamespace(reference=lambda: {"target": target}))
    channel = object(); captures = []
    peer = SimpleNamespace(verify=lambda: dict(target))
    monkeypatch.setattr(cb.AuthenticatedCleanupActor, "private_peer", lambda fd: captures.append(fd) or peer)
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
    graph.engine = SimpleNamespace(role_authority=SimpleNamespace(reference=lambda: {"target": target}))
    monkeypatch.setattr(cb.AuthenticatedCleanupActor, "private_peer", lambda _: SimpleNamespace(verify=lambda: peer))
    with pytest.raises(generic_host.HostError): graph._bind_engine_control_peer(object())


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
    graph.engine = SimpleNamespace(channel=channel, role_authority=SimpleNamespace(reference=lambda: {"target": target}))
    graph._engine_parent = SimpleNamespace(actor=None)
    first_read = threading.Event(); frame_ready = threading.Event(); replied = threading.Event()
    versions = []; token_version = [1]; reads = []
    def read(fd):
        assert fd is channel
        if reads: raise EOFError("fake completed channel")
        reads.append(1); first_read.set(); assert frame_ready.wait(1)
        return {"command": "signal_owned_listener"}
    actor = SimpleNamespace(verify=lambda: dict(target))
    def pin(fd):
        assert fd is channel
        versions.append(token_version[0]); return actor
    monkeypatch.setattr(cb, "_read_frame", read)
    monkeypatch.setattr(cb.AuthenticatedCleanupActor, "private_peer", pin)
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



def test_readiness_preserves_network_guard_in_final_command_popen_environment(tmp_path, monkeypatch):
    _write_manifest(tmp_path / 'guarded-command')
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    monkeypatch.setattr(generic_host, '_network_policy', lambda _record: {'mode': 'deny'})
    monkeypatch.setattr(generic_host, '_network_sandbox_argv', lambda argv, *_args: argv)
    captured = []
    class ReachedOwnedLaunch(Exception):
        pass
    def launch(argv, **kwargs):
        captured.append(kwargs['env'])
        raise ReachedOwnedLaunch
    monkeypatch.setattr(generic_host, 'spawn_task_scope', launch)
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    with pytest.raises(ReachedOwnedLaunch):
        host._run_command_definition(record, {}, attempt / 'outputs', attempt)
    assert len(captured) == 1
    actual = captured[0]
    hook = attempt / '.astrid-network-hook'
    assert str(hook) in actual['PYTHONPATH'].split(os.pathsep)
    assert (hook / 'sitecustomize.py').read_text() == 'from astrid.core.execution.network_policy import install_from_environment\ninstall_from_environment()\n'
    assert json.loads(actual['ASTRID_NETWORK_POLICY']) == {'mode': 'deny'}


@pytest.mark.parametrize("allow_runtime_connection", [False, True])
def test_child_environment_writes_exact_network_hook_bytes_on_windows(
    tmp_path, monkeypatch, allow_runtime_connection
):
    _write_manifest(tmp_path / "guarded-command")
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    write_text = Path.write_text

    def crlf_translating_write_text(path, content, *args, **kwargs):
        return write_text(path, content.replace("\n", "\r\n"), *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", crlf_translating_write_text)
    env, secrets = host._child_environment(
        record,
        attempt,
        network_policy={"mode": "deny"},
        allow_runtime_connection=allow_runtime_connection,
    )
    expected = (
        "from astrid.core.execution.network_policy import install_from_environment\n"
        "install_from_environment()\n"
        if allow_runtime_connection
        else generic_host._STRICT_NETWORK_HOOK_SOURCE
    )
    try:
        assert (attempt / ".astrid-network-hook" / "sitecustomize.py").read_bytes() == (
            expected.encode("utf-8")
        )
    finally:
        env.clear()
        secrets.clear()



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
    project, schema = tmp_path / "native-project", tmp_path / "schema"
    project.mkdir(); schema.mkdir()
    node = tmp_path / "node"
    node.write_bytes(b"fixture node identity")
    node.chmod(0o700)
    values = {
        "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "ASTRID_REMOTION_PROJECT_DIR": str(project), "ASTRID_NODE_EXECUTABLE": str(node),
        "ASTRID_TIMELINE_SCHEMA_PYTHONPATH": str(schema),
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
                           project=project, node=node, schema=schema)


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


@pytest.mark.parametrize("change", ["project", "node", "schema"])
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
    assert not host._claim_slots and not host._unresolved_task_scopes



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


def test_failed_command_finalizer_retains_original_and_attempt(tmp_path, monkeypatch):
    root = tmp_path / "retained-command"
    _write_manifest(root)
    host = GenericPackHost(pack_roots=[root])
    host.discover()
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    output.mkdir(parents=True)
    class Original:
        pid = 100
        returncode = None
        def poll(self):
            raise CleanupUncertainError("first native uncertainty")
    process = Original()
    monkeypatch.setattr(generic_host, "spawn_task_scope", lambda *a, **k: process)
    def failed_teardown(_process, **kwargs):
        assert _process is process
        raise CleanupUncertainError("retained teardown remains unresolved")
    monkeypatch.setattr(generic_host, "_terminate_process_group", failed_teardown)
    with pytest.raises(HostError, match="owned command cleanup uncertain"):
        host._run_command_definition(host.capabilities["test.echo"], {}, output, attempt)
    assert process in host._active_processes
    assert host._unresolved_task_scopes[0] == "first native uncertainty"
    assert attempt.is_dir()


def test_failed_worker_release_retains_original(tmp_path, monkeypatch):
    host = GenericPackHost(pack_roots=[tmp_path])
    attempt = tmp_path / "attempt"
    process = SimpleNamespace(pid=101, returncode=0, poll=lambda: 0,
                              communicate=lambda: ("", ""))
    # A deterministic child result exercises the real worker finalizer only.
    def acquire(*args, **kwargs):
        (attempt / ".astrid-capability-result.json").write_text(
            json.dumps({"ok": True, "result": {"outputs": []}}))
        return process
    monkeypatch.setattr(generic_host, "spawn_task_scope", acquire)
    monkeypatch.setattr(host, "_track_process", lambda p: host._active_processes.add(id(p)))
    monkeypatch.setattr(host, "_untrack_process", lambda p: host._active_processes.discard(id(p)))
    def refuse(_process):
        raise CleanupUncertainError("retained worker release failure")
    monkeypatch.setattr(generic_host, "_terminate_process_group", refuse)
    with pytest.raises(
        CommandCleanupUnresolved,
        match="cleanup unresolved: retained worker release failure",
    ) as caught:
        host.invoke_capability(capability_kind="executor", capability_id="test.echo", request={}, attempt=attempt)
    assert caught.value.execution_error is None
    assert isinstance(caught.value.__cause__, CleanupUncertainError)
    assert str(caught.value.__cause__) == "retained worker release failure"
    assert id(process) in host._active_processes
    assert "retained worker release failure" in host._unresolved_task_scopes


@pytest.mark.darwin_native
def test_native_persistent_capture_failure_contains_known_root_and_scrubs_fake_auth(tmp_path, monkeypatch):
    """Fault-injected native host boundary; not a natural kern5 diagnosis.

    The unadmitted leaf has a separate fixture-creator pipe for cooperative
    EOF cleanup after the refusal assertions. No PID/group signal fallback is
    available, and its eventual absence does not discharge the poisoned owner.
    """
    from astrid.core.execution import custody_broker as custody
    assert sys.platform == "darwin"
    root = tmp_path / "fake-codex-pack"
    manifest_path = _write_manifest(root, capability_id="generation.generate_image_codex")
    go = tmp_path / "go"
    child_record = tmp_path / "unadmitted-child.json"
    read_fd, write_fd = os.pipe()
    program = (
        "import os,time,json; from pathlib import Path\n"
        f"go=Path({str(go)!r})\n"
        "while not go.exists(): time.sleep(.005)\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        f" os.read({read_fd},1); os._exit(0)\n"
        f"record=Path({str(child_record)!r}); temporary=record.with_suffix('.tmp'); temporary.write_text(json.dumps({{'pid':pid}})); os.replace(temporary,record)\n"
        "while True: time.sleep(.05)\n"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["command"]["argv"] = ["{python_exec}", "-I", "-B", "-c", program]
    manifest_path.write_text(json.dumps(manifest))
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[root], client=runtime, attempt_base=tmp_path / "attempts")
    host.discover()
    task = {"task": {"id": "fake-auth-task", "capability": "generation.generate_image_codex",
        "project_id": "demo", "attempt_id": "fault-attempt", "fence": 1,
        "spec": {"spec": {"inputs": {}}}}}
    runtime.tasks["fake-auth-task"] = task
    attempt = tmp_path / "attempts" / "fake-auth-task-fault-attempt"
    fake_auth = attempt / "codex-home" / "auth.json"
    launched, captures, signals = [], [], []
    real_launch = generic_host.popen_owned_group
    real_capture = custody.current_process_audit_token
    real_signal = custody.ProcessCustody.signal
    def capture(pid):
        if launched and pid not in {launched[0].pid, os.getpid()}:
            record_deadline = time.monotonic() + 1.0
            while not child_record.exists() and time.monotonic() < record_deadline:
                time.sleep(.005)
            assert child_record.is_file(), "fixture parent did not publish child identity before fault injection"
            assert json.loads(child_record.read_text())["pid"] == pid
            captures.append(pid)
            raise custody.NativeTokenCaptureError("injected persistent LIVE no-port failure",
                substage="task_name_for_pid", kern_return=5, port_acquired=False)
        return real_capture(pid)
    def signal_owned(owner, pid, birth, signum, **kwargs):
        assert pid in owner._handles, "attempted to signal unadmitted fixture leaf"
        signals.append((pid, int(signum)))
        return real_signal(owner, pid, birth, signum, **kwargs)
    def acquire(argv, **kwargs):
        fake_auth.parent.mkdir(parents=True, exist_ok=True)
        fake_auth.write_text('{"fake":"never-a-real-credential"}')
        fake_auth.chmod(0o600)
        kwargs["pass_fds"] = tuple(kwargs.get("pass_fds", ())) + (read_fd,)
        kwargs["_delegate_inventory"] = False
        process = real_launch(argv, **kwargs)
        launched.append(process)
        go.write_text("release fixture spawn")
        return process
    monkeypatch.setattr(custody, "current_process_audit_token", capture)
    monkeypatch.setattr(custody.ProcessCustody, "signal", signal_owned)
    monkeypatch.setattr(generic_host, "spawn_task_scope", acquire)
    trace = {"fault_injected": True, "natural_kernel_cause_proven": False}
    try:
        with pytest.raises(HostError, match="owned cleanup incomplete"):
            host.run_task(task, lease_token="fixture-lease")
        assert len(launched) == 1 and len(captures) == 2
        process = launched[0]
        child_pid = json.loads(child_record.read_text())["pid"]
        assert all(pid == child_pid for pid in captures)
        assert child_pid not in process._astrid_custody._handles
        assert all(pid != child_pid for pid, _ in signals)
        assert process.wait(timeout=0) in {-signal.SIGTERM, -signal.SIGKILL}
        assert process in host._active_processes and host._unresolved_task_scopes
        assert process._astrid_custody._uncertain is not None and process._astrid_tree_uncertain
        assert attempt.is_dir() and not fake_auth.exists()
        assert runtime.uploaded_objects == {} and runtime.settlements == []
        # The doubt fails this task only: the host keeps admitting work.
        try:
            host.claim_once()
        except HostError as exc:
            assert "cleanup uncertainty" not in str(exc) and "admissions are blocked" not in str(exc)
        trace.update(original_root_wait=process.returncode, unadmitted_pid=child_pid,
                     signals=signals, captures=captures, fake_auth_scrubbed=True,
                     owner_still_retained=True, original_uncertainty_preserved=True)
    finally:
        # Independent fixture authority only: cooperative EOF, never PID signal.
        os.close(write_fd)
        os.close(read_fd)
        if launched and launched[0].poll() is None:
            process_group.contain_retained_handles(launched[0],
                cleanup_deadline=time.monotonic()+2.0, first_error="fixture failure cleanup remains uncertain")
        (tmp_path / "native-persistent-fault-trace.json").write_text(json.dumps(trace, indent=2))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        observed = custody.ordinary_candidate_observation(child_pid)
        if observed["outcome"] == "ABSENT":
            break
        time.sleep(.02)
    else:
        pytest.fail("cooperative fixture leaf EOF cleanup did not reach absence")
    trace["fixture_only_cooperative_leaf_absence"] = True
    (tmp_path / "native-persistent-fault-trace.json").write_text(json.dumps(trace, indent=2))
    assert host._unresolved_task_scopes and process._astrid_custody._uncertain is not None


def test_shutdown_cpu_uncertainty_drains_retained_owners_and_graph_before_refusal(tmp_path, monkeypatch):
    import threading
    host = GenericPackHost(pack_roots=[tmp_path])
    adapter = generic_host._ManagedTaskAdapter(threading.Event())
    monkeypatch.setattr(type(host.managed_tool_session), "current_adapter", property(lambda self: adapter))
    class Original:
        _astrid_tree_uncertain = "first acquisition cause"
    process = Original()
    host._active_processes.add(process)
    host._handoff_observation_known = True
    host._handoff_active_attempts = 0
    host._latch_cleanup_uncertainty("first host cause")
    calls = []
    def contain(original, *, cleanup_deadline, first_error):
        assert original is process and first_error == "first acquisition cause"
        calls.append(("retained", cleanup_deadline))
    monkeypatch.setattr(process_group, "contain_retained_handles", contain)
    host._native_engine_graph = SimpleNamespace(abort=lambda: calls.append(("graph", None)))
    monkeypatch.setattr(host.managed_tool_session, "close", lambda **kwargs: pytest.fail("poisoned admission token released"))
    with pytest.raises(CleanupUncertainError, match="CPU attempt cleanup remains unresolved"):
        host.shutdown()
    assert [row[0] for row in calls] == ["retained", "graph"]
    assert adapter.fenced and process in host._active_processes
    assert "first host cause" in host._unresolved_task_scopes



def test_command_task_scope_unresolved_fails_task_without_latching_host(tmp_path: Path) -> None:
    root = tmp_path / "escapee"
    root.mkdir()
    code = (
        "import subprocess,sys,time;from pathlib import Path;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True);"
        "Path('{out}').mkdir(parents=True,exist_ok=True);Path('{out}','pid').write_text(str(c.pid));"
        "time.sleep(60)"
    )
    (root / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "test.escapee", "name": "Escapee", "kind": "external",
        "version": "1.0", "command": {"argv": ["{python_exec}", "-c", code]},
        "outputs": [{"name": "o", "type": "file", "path_template": "{out}/pid"}],
    }), encoding="utf-8")
    host = GenericPackHost(pack_roots=[tmp_path])
    host.discover()
    attempt = tmp_path / "attempt"
    output_root = attempt / "outputs"
    output_root.mkdir(parents=True)
    pid_file = output_root / "pid"
    escapee = []

    def cancelled():
        if pid_file.exists() and pid_file.read_text():
            escapee.append(int(pid_file.read_text()))
            return True
        return False

    try:
        with pytest.raises(HostError, match="cleanup unresolved"):
            host._run_command_definition(host.capabilities["test.escapee"], {}, output_root, attempt,
                                         cancelled=cancelled)
        assert host._unresolved_task_scopes and str(escapee[0]) in host._unresolved_task_scopes[0]
        assert not host._active_processes
    finally:
        for pid in escapee:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass



def test_attempt_cleanup_failure_fails_that_task_and_next_task_runs(tmp_path, monkeypatch):
    _write_manifest(tmp_path / "echo")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    host.register()

    def task(n):
        body = {"task": {"id": f"task-{n}", "capability": "test.echo", "project_id": "demo",
                         "attempt_id": f"attempt-{n}", "fence": 1, "spec": {"spec": {"inputs": {}}}}}
        runtime.tasks[f"task-{n}"] = body
        return body

    real = generic_host._cleanup_ephemeral_attempt
    def fail_once(root):
        monkeypatch.setattr(generic_host, "_cleanup_ephemeral_attempt", real)
        raise HostError("injected attempt cleanup failure")
    monkeypatch.setattr(generic_host, "_cleanup_ephemeral_attempt", fail_once)
    with pytest.raises(HostError, match="owned cleanup incomplete"):
        host.run_task(task(1), lease_token="lease-1")
    assert any("injected attempt cleanup failure" in note for note in host._unresolved_task_scopes)
    # The host keeps accepting work; only the affected task failed.
    assert host.run_task(task(2), lease_token="lease-2")["task"]["status"] == "completed"
