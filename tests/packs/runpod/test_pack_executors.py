"""Test the four external.runpod executors with mocked runpod_lifecycle."""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import inspect
import json
import tempfile
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from astrid.core.contracts.errors import AstridError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["provision", "session"])
def test_outer_adapter_allocation_unknown_keeps_identity_and_blocks_another_launch(
    command: str, produces_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import AllocationUnknown

    from astrid.packs.runpod.shared import common as _common

    resolved = {
        **_common._RUNPOD_COMPUTE_DEFAULTS,
        "gpu_type": "NVIDIA GeForce RTX 5090",
        "allowed_cuda_versions": ["13.0"],
        "image": "runpod/base:cuda1300",
        "storage_name": "existing",
        "volume_in_gb": 0,
        "require_storage": True,
        "datacenter_id": None,
    }

    launches: list[str] = []

    async def ambiguous_launch(config, *, name):
        assert config.attach_only is True
        assert config.disk_size_gb == 0
        assert config.storage_name == "existing"
        assert config.worker_image == "runpod/base:cuda1300"
        assert config.allowed_cuda_versions == ("13.0",)
        assert config.template_id == ""
        launches.append(name)
        marker = json.loads((produces_dir / "pod_handle.json").read_text())
        assert marker["state"] == "allocation_pending"
        assert marker["request_name"] == name
        assert marker["operation_id"][:12] in name
        raise AllocationUnknown(name, "NVIDIA GeForce RTX 5090", 72, "existing", "vol-id")

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-rpa_0000000000000000000000000000000000000000000000")
    with patch("runpod_lifecycle.launch", ambiguous_launch), \
         patch.object(_common, "_resolve_compute_profile", return_value=resolved), \
         patch.object(_common, "_preflight_storage"), \
         patch.object(_common, "_get_hourly_rate", return_value=0.5):
        with pytest.raises(AstridError) as first:
            _common.main([command, "--produces-dir", str(produces_dir)])
        assert first.value.code == "allocation_unknown"
        first_marker = json.loads((produces_dir / "pod_handle.json").read_text())
        with pytest.raises(AstridError, match="requires reconciliation") as second:
            _common.main([command, "--produces-dir", str(produces_dir)])
        assert second.value.code == "allocation_unknown"
        assert second.value.state_snapshot["operation_id"] == first_marker["operation_id"]
        with pytest.raises(AstridError, match="no confirmed pod ID"):
            _common._load_handle_and_config(produces_dir / "pod_handle.json")

    assert len(launches) == 1
    marker = json.loads((produces_dir / "pod_handle.json").read_text())
    assert marker["state"] == "allocation_unknown"
    assert marker["reconciliation_required"] is True
    assert marker["operation_id"] == first_marker["operation_id"]
    assert marker["request_name"] == launches[0]
    assert marker["storage_volume_id_attempted"] == "vol-id"
    assert "pod_id" not in marker
    assert "test-key-rpa_" not in (produces_dir / "pod_handle.json").read_text()


@pytest.mark.parametrize("command", ["provision", "session"])
def test_launcher_preserves_known_pod_from_nonretryable_launch_failure(
    command: str, produces_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from runpod_lifecycle import PostCreateHookFailure

    from astrid.packs.runpod.shared import common as _common

    resolved = {
        **_common._RUNPOD_COMPUTE_DEFAULTS,
        "gpu_type": "NVIDIA GeForce RTX 5090",
        "allowed_cuda_versions": ["13.0"],
        "image": "runpod/base:cuda1300",
        "storage_name": None,
        "volume_in_gb": 0,
        "require_storage": False,
        "datacenter_id": None,
    }
    launches: list[str] = []

    async def known_pod_failure(config, *, name):
        launches.append(name)
        marker = json.loads((produces_dir / "pod_handle.json").read_text(encoding="utf-8"))
        assert marker["state"] == "allocation_pending"
        assert marker["request_name"] == name
        raise PostCreateHookFailure("pod-created-before-hook-error", RuntimeError("state hook failed"))

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-rpa_0000000000000000000000000000000000000000000000")
    with patch("runpod_lifecycle.launch", known_pod_failure), \
         patch.object(_common, "_resolve_compute_profile", return_value=resolved), \
         patch.object(_common, "_preflight_storage"), \
         patch.object(_common, "_get_hourly_rate", return_value=0.5), \
         patch.object(_common, "_terminate_pod_id", new_callable=AsyncMock) as terminate:
        with pytest.raises(AstridError) as raised:
            _common.main([command, "--produces-dir", str(produces_dir)])

        assert raised.value.code == "allocation_known"
        with pytest.raises(AstridError, match="already identifies pod"):
            _common.main([command, "--produces-dir", str(produces_dir)])
        terminate.assert_not_awaited()

    marker_text = (produces_dir / "pod_handle.json").read_text(encoding="utf-8")
    marker = json.loads(marker_text)
    assert len(launches) == 1
    assert marker["state"] == "allocated_unverified"
    assert marker["pod_id"] == "pod-created-before-hook-error"
    assert marker["request_name"] == launches[0]
    assert marker["reconciliation_required"] is True
    assert marker["failure_type"] == "PostCreateHookFailure"
    assert "test-key-rpa_" not in marker_text


def test_attached_claim_without_exclusive_authority_refuses_teardown(
    produces_dir: Path,
) -> None:
    from astrid.packs.runpod.shared import common as _common

    handle_path = produces_dir / "pod_handle.json"
    handle_path.write_text("{}", encoding="utf-8")
    attached_handle = {
        "schema_version": "astrid.runpod.claim.v1",
        "state": "claimed",
        "pod_id": "pod-shared",
        "acquisition_mode": "attach",
        "release_authorized": False,
    }

    class Args:
        pod_handle = None

    with patch.object(_common, "_load_handle_and_config", return_value=(attached_handle, object())):
        with pytest.raises(AstridError, match="without exclusive release authority") as raised:
            _common.cmd_teardown(Args(), produces_dir)

    assert raised.value.code == "release_not_authorized"
    assert not (produces_dir / "teardown_receipt.json").exists()


def test_datacenter_is_forwarded_only_when_lifecycle_config_supports_it() -> None:
    from astrid.packs.runpod.shared import common as _common

    class SupportedConfig:
        def __init__(self, *, api_key: str, datacenter_id: str | None = None) -> None:
            self.api_key = api_key
            self.datacenter_id = datacenter_id

    class UnsupportedConfig:
        def __init__(self, *, api_key: str) -> None:
            self.api_key = api_key

    config = _common._new_runpod_config(
        SupportedConfig,
        datacenter_id="EU-RO-1",
        template_id=None,
        api_key="secret",
    )
    assert config.datacenter_id == "EU-RO-1"

    with pytest.raises(AstridError, match="does not support datacenter"):
        _common._new_runpod_config(
            UnsupportedConfig,
            datacenter_id="EU-RO-1",
            template_id=None,
            api_key="secret",
        )


@pytest.fixture
def produces_dir() -> Path:
    """Create a temporary produces directory for executor output."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def mock_pod() -> MagicMock:
    """Create a mock RunPod Pod object."""
    pod = MagicMock()
    pod.id = "pod-abc123"
    pod.name = "astrid-test-pod-1700000000"
    pod._storage_volume = None

    # Make async methods
    pod.wait_ready = AsyncMock()
    pod._ensure_ssh_details = AsyncMock(return_value={"ip": "1.2.3.4", "port": 2222})
    pod.is_idle = AsyncMock(return_value=True)
    pod.terminate = AsyncMock()
    pod.exec_ssh = AsyncMock(return_value=("stdout", "stderr", 0))
    return pod


@pytest.fixture
def mock_launch(mock_pod: MagicMock) -> MagicMock:
    """Mock runpod_lifecycle.launch."""
    return AsyncMock(return_value=mock_pod)


@pytest.fixture
def mock_get_pod(mock_pod: MagicMock) -> MagicMock:
    """Mock runpod_lifecycle.get_pod / discovery.get_pod."""
    return AsyncMock(return_value=mock_pod)


@pytest.fixture
def mock_ship_and_run_detached() -> MagicMock:
    """Mock runpod_lifecycle.ship_and_run_detached."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "ok"
    result.stderr = ""
    result.terminated = False
    result.artifact_root = None
    result.breach_log = []
    return AsyncMock(return_value=result)


def _patch_runpod_lifecycle(
    mock_launch: MagicMock,
    mock_get_pod: MagicMock,
    mock_ship_and_run: MagicMock,
) -> dict:
    """Patch all runpod_lifecycle imports used by the pack executors."""
    patchers = {
        "launch": patch("runpod_lifecycle.launch", mock_launch),
        "get_pod": patch("runpod_lifecycle.get_pod", mock_get_pod),
        "ship_and_run_detached": patch("runpod_lifecycle.ship_and_run_detached", mock_ship_and_run),
        "RunPodConfig": patch("runpod_lifecycle.RunPodConfig", MagicMock()),
    }
    return patchers


# ---------------------------------------------------------------------------
# Schema assertions
# ---------------------------------------------------------------------------

_POD_HANDLE_REQUIRED_KEYS = {
    "pod_id",
    "ssh",
    "name",
    "name_prefix",
    "terminate_at",
    "gpu_type",
    "hourly_rate",
    "provisioned_at",
    "config_snapshot",
}

_CONFIG_SNAPSHOT_REQUIRED_KEYS = {
    "api_key_ref",
    "datacenter_id",
    "image",
    "container_disk_in_gb",
    "volume_in_gb",
    "storage_name",
    "network_volume_id",
    "ports",
}

_COST_SIDECAR_REQUIRED_KEYS = {"amount", "currency", "source"}

_SUPPORTED_DETACHED_KWARGS = {
    "remote_script",
    "pod",
    "local_root",
    "remote_root",
    "exclude",
    "upload_mode",
    "timeout",
    "name_prefix",
    "terminate_after_exec",
    "poll_interval",
}

_UNSUPPORTED_DETACHED_KWARGS = {
    "guard_factory",
    "poll_command_template",
    "poll_exit_marker",
    "artifact_paths",
}

_SESSION_SMOKE_SCRIPT = (
    "nvidia-smi -L >/tmp/astrid-smoke-gpu.txt && "
    "echo ok && "
    "mkdir -p output && "
    "printf 'smoke\\n' > output/smoke.txt"
)


def test_runpod_lifecycle_detached_signature_is_v03_contract() -> None:
    """Pin the installed v0.3 detached-run API Astrid is allowed to target."""
    import runpod_lifecycle

    version = importlib_metadata.version("runpod-lifecycle")
    assert tuple(int(part) for part in version.split(".")[:2]) >= (0, 3)

    signature = inspect.signature(runpod_lifecycle.ship_and_run_detached)
    params = set(signature.parameters)

    assert _SUPPORTED_DETACHED_KWARGS <= params
    assert _UNSUPPORTED_DETACHED_KWARGS.isdisjoint(params)


def _assert_detached_call_uses_v03_contract(mock_call: MagicMock) -> None:
    """Fail if Astrid starts passing kwargs absent from runpod-lifecycle v0.3."""
    assert mock_call.await_count > 0
    for call in mock_call.await_args_list:
        kwargs = set(call.kwargs)
        assert kwargs <= _SUPPORTED_DETACHED_KWARGS
        assert _UNSUPPORTED_DETACHED_KWARGS.isdisjoint(kwargs)


def _assert_pod_handle_shape(handle: dict) -> None:
    """Verify pod_handle.json matches the locked schema."""
    for key in _POD_HANDLE_REQUIRED_KEYS:
        assert key in handle, f"pod_handle.json missing required key: {key}"

    # api_key_ref must be an env var name, never a literal key
    api_key_ref = handle["config_snapshot"]["api_key_ref"]
    assert isinstance(api_key_ref, str), "api_key_ref must be a string"
    assert not api_key_ref.startswith("rpa_"), (
        f"api_key_ref is {api_key_ref!r} — looks like a literal API key, "
        f"but must be an env var name like RUNPOD_API_KEY"
    )

    # Must not have breach_log (it's a PodGuard in-memory attribute)
    assert "breach_log" not in handle, "pod_handle.json must NOT contain breach_log"
    assert "project" not in handle
    assert "run_id" not in handle
    assert "handle_path" not in handle

    for key in _CONFIG_SNAPSHOT_REQUIRED_KEYS:
        assert key in handle["config_snapshot"], (
            f"config_snapshot missing required key: {key}"
        )

    # hourly_rate must be a positive float at the top level
    assert isinstance(handle["hourly_rate"], (int, float))
    assert handle["hourly_rate"] > 0


def _assert_cost_shape(cost: dict) -> None:
    """Verify cost sidecar matches CostEntry shape."""
    for key in _COST_SIDECAR_REQUIRED_KEYS:
        assert key in cost, f"cost.json missing required key: {key}"
    assert cost["currency"] == "USD", f"expected USD, got {cost['currency']!r}"
    assert isinstance(cost["amount"], (int, float))
    assert cost["amount"] >= 0
    # basis is optional metadata but should be present
    assert "basis" in cost, "cost.json should include optional 'basis' metadata"


def test_pod_handle_builder_keeps_durable_and_transient_shapes_compatible() -> None:
    """Provision and session use one secret-safe pod_handle.json shape."""
    from astrid.packs.runpod.shared.common import _build_pod_handle

    pod = MagicMock()
    pod.id = "pod-shape"
    pod.name = "astrid-shape"
    ssh = {"ip": "1.2.3.4", "port": 2222}

    provision_handle = _build_pod_handle(
        pod=pod,
        ssh=ssh,
        name_prefix="astrid",
        terminate_at="2026-05-24T12:00:00+00:00",
        gpu_type="NVIDIA GeForce RTX 4090",
        hourly_rate=0.34,
        provisioned_at="2026-05-24T10:00:00+00:00",
        datacenter_id="US-GA-1",
        image="runpod/pytorch:latest",
        container_disk_gb=200,
        volume_in_gb=200,
        storage_name="astrid-storage",
        network_volume_id="vol-storage",
        ports=None,
    )
    session_handle = _build_pod_handle(
        pod=pod,
        ssh=ssh,
        name_prefix="astrid",
        terminate_at="2026-05-24T12:00:00+00:00",
        gpu_type="NVIDIA GeForce RTX 4090",
        hourly_rate=0.34,
        provisioned_at="2026-05-24T10:00:00+00:00",
        datacenter_id="US-GA-1",
        image="runpod/pytorch:latest",
        container_disk_gb=200,
        volume_in_gb=200,
        storage_name="astrid-storage",
        network_volume_id="vol-storage",
        ports=None,
    )

    assert list(provision_handle) == list(session_handle)
    assert list(provision_handle["config_snapshot"]) == list(session_handle["config_snapshot"])
    assert provision_handle["config_snapshot"]["storage_name"] == "astrid-storage"
    assert session_handle["config_snapshot"]["storage_name"] == "astrid-storage"
    assert "project" not in provision_handle
    assert "run_id" not in provision_handle
    assert "handle_path" not in provision_handle
    _assert_pod_handle_shape(provision_handle)


# ---------------------------------------------------------------------------
# Provision executor
# ---------------------------------------------------------------------------


def test_provision_writes_pod_handle_and_cost(
    produces_dir: Path,
    mock_launch: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """Provision executor writes pod_handle.json and cost.json with correct shapes."""
    with patch("runpod_lifecycle.launch", mock_launch), \
         patch("runpod_lifecycle.RunPodConfig", MagicMock()):
        # Import under patches so they take effect
        from astrid.packs.runpod.actions.provision.run import cmd_provision

        class Args:
            gpu_type = "NVIDIA GeForce RTX 4090"
            storage_name = None
            max_runtime_seconds = None
            name_prefix = None
            image = None
            container_disk_gb = None
            datacenter_id = None
            produces_dir = produces_dir

        # Set env for API key resolution
        import os
        os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"

        try:
            exit_code = cmd_provision(Args(), produces_dir)
            assert exit_code == 0

            # Verify pod_handle.json
            handle_path = produces_dir / "pod_handle.json"
            assert handle_path.is_file(), "pod_handle.json not written"
            handle = json.loads(handle_path.read_text())
            _assert_pod_handle_shape(handle)
            assert handle["config_snapshot"]["storage_name"] is None

            # Verify cost.json
            cost_path = produces_dir / "cost.json"
            assert cost_path.is_file(), "cost.json not written"
            cost = json.loads(cost_path.read_text())
            _assert_cost_shape(cost)
        finally:
            if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
                del os.environ["RUNPOD_API_KEY"]


@pytest.mark.parametrize(
    ("failure_phase", "failure"),
    [
        ("readiness", TimeoutError("readiness timeout")),
        ("ssh", ConnectionError("ssh discovery failed")),
    ],
)
def test_provision_persists_allocation_before_readiness_and_marks_cleanup_pending(
    produces_dir: Path,
    mock_launch: MagicMock,
    mock_get_pod: MagicMock,
    mock_pod: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    failure: Exception,
) -> None:
    """A post-allocation edge leaves a reloadable, truthful recovery breadcrumb."""
    observed: dict[str, object] = {}

    async def wait_ready(*, timeout: int) -> None:
        observed["before_failure"] = json.loads(
            (produces_dir / "pod_handle.json").read_text(encoding="utf-8")
        )
        if failure_phase == "readiness":
            raise failure

    async def ensure_ssh_details() -> dict[str, object]:
        observed["before_failure"] = json.loads(
            (produces_dir / "pod_handle.json").read_text(encoding="utf-8")
        )
        raise failure

    mock_pod.wait_ready = AsyncMock(side_effect=wait_ready)
    mock_pod._ensure_ssh_details = AsyncMock(side_effect=ensure_ssh_details)

    class Args:
        gpu_type = "NVIDIA GeForce RTX 4090"
        storage_name = None
        max_runtime_seconds = None
        name_prefix = None
        image = None
        container_disk_gb = None
        datacenter_id = None
        produces_dir = produces_dir

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-rpa_0000000000000000000000000000000000000000000000")
    with patch("runpod_lifecycle.launch", mock_launch), \
         patch("runpod_lifecycle.get_pod", mock_get_pod), \
         patch("runpod_lifecycle.RunPodConfig", MagicMock()), \
         patch("astrid.packs.runpod.shared.common._get_hourly_rate", return_value=0.5):
        with pytest.raises(AstridError, match=str(failure)) as raised:
            from astrid.packs.runpod.actions.provision.run import cmd_provision

            cmd_provision(Args(), produces_dir)

    before_failure = observed["before_failure"]
    assert isinstance(before_failure, dict)
    assert before_failure["pod_id"] == "pod-abc123"
    assert before_failure["state"] == "provisioning"
    assert "ssh" not in before_failure
    assert before_failure["hourly_rate"] == 0.5
    assert before_failure["config_snapshot"]["api_key_ref"] == "RUNPOD_API_KEY"
    for key in _CONFIG_SNAPSHOT_REQUIRED_KEYS:
        assert key in before_failure["config_snapshot"]

    handle = json.loads((produces_dir / "pod_handle.json").read_text(encoding="utf-8"))
    assert handle["pod_id"] == "pod-abc123"
    assert handle["state"] == "cleanup_pending"
    assert handle["cleanup_pending"] is True
    assert handle["cleanup_phase"] == failure_phase
    assert str(failure) in handle["cleanup_error"]
    assert raised.value.state_snapshot["cleanup_pending"] is True

    class TeardownArgs:
        pod_handle = None
        produces_dir = produces_dir

    from astrid.packs.runpod.actions.provision.run import cmd_teardown

    with patch("runpod_lifecycle.get_pod", mock_get_pod), \
         patch("runpod_lifecycle.RunPodConfig", MagicMock()):
        assert cmd_teardown(TeardownArgs(), produces_dir) == 0
    receipt = json.loads((produces_dir / "teardown_receipt.json").read_text(encoding="utf-8"))
    assert receipt["pod_id"] == "pod-abc123"
    assert receipt["status"] == "terminated"
    mock_get_pod.assert_awaited_once_with("pod-abc123", ANY, name="astrid-test-pod-1700000000")
    mock_pod.terminate.assert_awaited_once()


def test_provisional_handle_retains_cleanup_pending_until_reconciliation(
    produces_dir: Path,
    mock_launch: MagicMock,
    mock_get_pod: MagicMock,
    mock_pod: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cleanup remains recoverable and a later exact-pod retry succeeds."""
    mock_pod.wait_ready = AsyncMock(side_effect=TimeoutError("readiness timeout"))
    mock_pod.terminate = AsyncMock(side_effect=[RuntimeError("teardown transport failed"), None])

    class ProvisionArgs:
        gpu_type = "NVIDIA GeForce RTX 4090"
        storage_name = None
        max_runtime_seconds = None
        name_prefix = None
        image = None
        container_disk_gb = None
        datacenter_id = None
        produces_dir = produces_dir

    class TeardownArgs:
        pod_handle = None
        produces_dir = produces_dir

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-rpa_0000000000000000000000000000000000000000000000")
    with patch("runpod_lifecycle.launch", mock_launch), \
         patch("runpod_lifecycle.get_pod", mock_get_pod), \
         patch("runpod_lifecycle.RunPodConfig", MagicMock()), \
         patch("astrid.packs.runpod.shared.common._get_hourly_rate", return_value=0.5):
        from astrid.packs.runpod.actions.provision.run import cmd_provision, cmd_teardown

        with pytest.raises(AstridError, match="readiness timeout"):
            cmd_provision(ProvisionArgs(), produces_dir)

        with pytest.raises(AstridError, match="teardown failed: teardown transport failed"):
            cmd_teardown(TeardownArgs(), produces_dir)

        pending = json.loads((produces_dir / "pod_handle.json").read_text(encoding="utf-8"))
        assert pending["pod_id"] == "pod-abc123"
        assert pending["state"] == "cleanup_pending"
        assert pending["cleanup_pending"] is True

        assert cmd_teardown(TeardownArgs(), produces_dir) == 0

    receipt = json.loads((produces_dir / "teardown_receipt.json").read_text(encoding="utf-8"))
    assert receipt["pod_id"] == "pod-abc123"
    assert receipt["status"] == "terminated"
    assert mock_get_pod.await_args_list == [
        (("pod-abc123", ANY), {"name": "astrid-test-pod-1700000000"}),
        (("pod-abc123", ANY), {"name": "astrid-test-pod-1700000000"}),
    ]
    assert mock_pod.terminate.await_count == 2


def test_provision_forwards_and_persists_allowed_cuda_versions(
    produces_dir: Path, mock_launch: MagicMock, mock_pod: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astrid.packs.runpod.actions.provision.run import cmd_provision

    class Args:
        gpu_type = "NVIDIA GeForce RTX 4090"
        allowed_cuda_versions = "13.0"
        storage_name = None
        max_runtime_seconds = None
        name_prefix = None
        image = None
        container_disk_gb = None
        volume_in_gb = None
        volume_mount_path = None
        datacenter_id = None
        ports = None
        require_storage = False
        produces_dir = produces_dir

    captured: dict[str, object] = {}
    def config_factory(**kwargs):
        captured.update(kwargs)
        return MagicMock(**kwargs)

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-rpa_0000000000000000000000000000000000000000000000")
    with patch("runpod_lifecycle.RunPodConfig", side_effect=config_factory), \
         patch("runpod_lifecycle.launch", mock_launch), \
         patch("astrid.packs.runpod.shared.common._get_hourly_rate", return_value=0.5):
        assert cmd_provision(Args(), produces_dir) == 0

    assert captured["allowed_cuda_versions"] == ("13.0",)
    handle = json.loads((produces_dir / "pod_handle.json").read_text())
    assert handle["config_snapshot"]["allowed_cuda_versions"] == ["13.0"]
    resolved = json.loads((produces_dir / "compute_resolved.json").read_text())
    assert resolved["allowed_cuda_versions"] == ["13.0"]


def test_empty_explicit_cuda_filter_fails_before_launch(produces_dir: Path) -> None:
    from astrid.packs.runpod.shared.common import _resolve_compute_profile

    args = MagicMock()
    args.allowed_cuda_versions = ",  ,"
    with pytest.raises(AstridError, match="at least one version"):
        _resolve_compute_profile(args, produces_dir)


def test_provision_storage_required_fails_before_launch_with_ensure_storage_hint(
    produces_dir: Path,
    mock_launch: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Storage-required provision fails before launch when no storage name is configured."""
    from astrid.core.integrations.runpod.storage import ENSURE_STORAGE_HINT
    from astrid.packs.runpod.actions.provision.run import cmd_provision

    class Args:
        gpu_type = "NVIDIA GeForce RTX 4090"
        storage_name = None
        require_storage = True
        max_runtime_seconds = None
        name_prefix = None
        image = None
        container_disk_gb = None
        datacenter_id = None
        ports = None
        produces_dir = produces_dir

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-rpa_0000000000000000000000000000000000000000000000")

    with patch("runpod_lifecycle.launch", mock_launch), \
         patch("runpod_lifecycle.Pod.create_storage", AsyncMock()) as create_storage:
        with pytest.raises(AstridError) as raised:
            cmd_provision(Args(), produces_dir)

    assert ENSURE_STORAGE_HINT in raised.value.cause
    mock_launch.assert_not_awaited()
    create_storage.assert_not_called()


def test_provision_named_storage_missing_fails_before_launch_without_creation(
    produces_dir: Path,
    mock_launch: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A provided storage_name must already exist; provision never creates it implicitly."""
    from astrid.core.integrations.runpod.storage import ENSURE_STORAGE_HINT
    from astrid.packs.runpod.actions.provision.run import cmd_provision

    class Args:
        gpu_type = "NVIDIA GeForce RTX 4090"
        storage_name = "missing-volume"
        require_storage = False
        max_runtime_seconds = None
        name_prefix = None
        image = None
        container_disk_gb = None
        datacenter_id = None
        ports = None
        produces_dir = produces_dir

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-rpa_0000000000000000000000000000000000000000000000")

    with patch("runpod_lifecycle.launch", mock_launch), \
         patch("runpod_lifecycle.api.get_network_volumes", return_value=[]), \
         patch("runpod_lifecycle.api.create_network_volume") as create_storage:
        with pytest.raises(AstridError) as raised:
            cmd_provision(Args(), produces_dir)

    assert "missing-volume" in raised.value.cause
    assert ENSURE_STORAGE_HINT in raised.value.cause
    mock_launch.assert_not_awaited()
    create_storage.assert_not_called()


def test_provision_configured_storage_name_is_recorded_in_canonical_handle(
    produces_dir: Path,
    mock_launch: MagicMock,
    mock_pod: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provision persists the configured storage name, not only the volume id."""
    from astrid.packs.runpod.actions.provision.run import cmd_provision

    class Args:
        gpu_type = "NVIDIA GeForce RTX 4090"
        storage_name = "astrid-storage"
        require_storage = False
        max_runtime_seconds = None
        name_prefix = None
        image = None
        container_disk_gb = None
        datacenter_id = None
        ports = None
        produces_dir = produces_dir

    mock_pod._storage_volume = "vol-astrid-storage"
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-rpa_0000000000000000000000000000000000000000000000")

    with patch("runpod_lifecycle.launch", mock_launch), \
         patch("runpod_lifecycle.api.get_network_volumes", return_value=[{"id": "vol-astrid-storage", "name": "astrid-storage"}]), \
         patch("runpod_lifecycle.RunPodConfig", MagicMock()):
        exit_code = cmd_provision(Args(), produces_dir)

    assert exit_code == 0
    handle = json.loads((produces_dir / "pod_handle.json").read_text(encoding="utf-8"))
    _assert_pod_handle_shape(handle)
    assert handle["config_snapshot"]["storage_name"] == "astrid-storage"
    assert handle["config_snapshot"]["network_volume_id"] == "vol-astrid-storage"
    assert "project" not in handle
    assert "run_id" not in handle
    assert "handle_path" not in handle


def test_session_storage_required_fails_before_launch_with_ensure_storage_hint(
    produces_dir: Path,
    mock_launch: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Storage-required session fails before launch when no storage name is configured."""
    from astrid.core.integrations.runpod.storage import ENSURE_STORAGE_HINT
    from astrid.packs.runpod.actions.provision.run import cmd_session

    class Args:
        gpu_type = "NVIDIA GeForce RTX 4090"
        storage_name = None
        require_storage = True
        max_runtime_seconds = None
        name_prefix = None
        image = None
        container_disk_gb = None
        datacenter_id = None
        ports = None
        remote_root = None
        remote_script = None
        local_root = None
        timeout = None
        upload_mode = None
        excludes = None
        produces_dir = produces_dir

    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-rpa_0000000000000000000000000000000000000000000000")

    with patch("runpod_lifecycle.launch", mock_launch), \
         patch("runpod_lifecycle.Pod.create_storage", AsyncMock()) as create_storage:
        with pytest.raises(AstridError) as raised:
            cmd_session(Args(), produces_dir)

    assert ENSURE_STORAGE_HINT in raised.value.cause
    mock_launch.assert_not_awaited()
    create_storage.assert_not_called()


# ---------------------------------------------------------------------------
# Exec executor
# ---------------------------------------------------------------------------


def test_exec_reads_handle_and_writes_result(
    produces_dir: Path,
    mock_get_pod: MagicMock,
    mock_ship_and_run_detached: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """Exec executor reattaches, runs, and writes exec_result.json + cost.json."""
    # Pre-create a valid pod_handle.json
    handle = {
        "pod_id": "pod-abc123",
        "ssh": "root@1.2.3.4 -p 2222",
        "name": "astrid-test-pod-1700000000",
        "name_prefix": "astrid-test",
        "terminate_at": "2099-01-01T00:00:00Z",
        "gpu_type": "NVIDIA GeForce RTX 4090",
        "hourly_rate": 0.34,
        "provisioned_at": "2024-01-01T00:00:00Z",
        "config_snapshot": {
            "api_key_ref": "RUNPOD_API_KEY",
            "datacenter_id": "US-GA-1",
            "image": "runpod/pytorch:latest",
            "container_disk_in_gb": 200,
            "volume_in_gb": 0,
            "network_volume_id": None,
            "ports": "8888/http,22/tcp",
        },
    }
    handle_path = produces_dir / "pod_handle.json"
    handle_path.write_text(json.dumps(handle))

    import os
    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"

    try:
        with patch("runpod_lifecycle.get_pod", mock_get_pod), \
             patch("runpod_lifecycle.ship_and_run_detached", mock_ship_and_run_detached), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_exec

            class Args:
                pod_handle = str(handle_path)
                local_root = None
                remote_root = None
                remote_script = "echo hello"
                timeout = None
                upload_mode = None
                excludes = None
                produces_dir = produces_dir

            exit_code = cmd_exec(Args(), produces_dir)
            assert exit_code == 0
            _assert_detached_call_uses_v03_contract(mock_ship_and_run_detached)

            # Verify exec_result.json
            result_path = produces_dir / "exec_result.json"
            assert result_path.is_file(), "exec_result.json not written"
            result = json.loads(result_path.read_text())
            assert "returncode" in result
            assert "stdout" in result

            # Verify cost.json
            cost_path = produces_dir / "cost.json"
            assert cost_path.is_file(), "cost.json not written"
            cost = json.loads(cost_path.read_text())
            _assert_cost_shape(cost)
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


def test_exec_nonzero_remote_exit_keeps_artifacts_diagnostics_result_and_cost(
    produces_dir: Path,
    tmp_path: Path,
    mock_get_pod: MagicMock,
) -> None:
    """Non-zero remote exits still materialize the v0.3 artifact root and diagnostics."""
    handle = {
        "pod_id": "pod-abc123",
        "ssh": "root@1.2.3.4 -p 2222",
        "name": "astrid-test-pod-1700000000",
        "name_prefix": "astrid-test",
        "terminate_at": "2099-01-01T00:00:00Z",
        "gpu_type": "NVIDIA GeForce RTX 4090",
        "hourly_rate": 0.34,
        "provisioned_at": "2024-01-01T00:00:00Z",
        "config_snapshot": {
            "api_key_ref": "RUNPOD_API_KEY",
            "datacenter_id": "US-GA-1",
            "image": "runpod/pytorch:latest",
            "container_disk_in_gb": 200,
            "volume_in_gb": 0,
            "network_volume_id": None,
            "ports": "8888/http,22/tcp",
        },
    }
    handle_path = produces_dir / "pod_handle.json"
    handle_path.write_text(json.dumps(handle))
    artifact_root = tmp_path / "substrate-artifacts"
    artifact_root.mkdir()
    (artifact_root / "remote.txt").write_text("remote artifact", encoding="utf-8")
    local_root = tmp_path / "local-root"
    local_root.mkdir()
    (local_root / "must-not-copy.txt").write_text("local", encoding="utf-8")
    local_root_arg = str(local_root)
    local_root_arg = str(local_root)

    result = MagicMock()
    result.returncode = 7
    result.stdout = "partial stdout"
    result.stderr = "remote failed"
    result.terminated = False
    result.artifact_root = artifact_root
    result.breach_log = []
    ship = AsyncMock(return_value=result)

    import os
    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"

    try:
        with patch("runpod_lifecycle.get_pod", mock_get_pod), \
             patch("runpod_lifecycle.ship_and_run_detached", ship), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_exec

            class Args:
                pod_handle = str(handle_path)
                local_root = local_root_arg
                remote_root = "/workspace"
                remote_script = "exit 7"
                timeout = None
                upload_mode = None
                excludes = None
                produces_dir = produces_dir

            exit_code = cmd_exec(Args(), produces_dir)

        assert exit_code == 7
        _assert_detached_call_uses_v03_contract(ship)
        artifact_dir = produces_dir / "artifact_dir"
        assert (artifact_dir / "remote.txt").read_text(encoding="utf-8") == "remote artifact"
        assert not (artifact_dir / "must-not-copy.txt").exists()
        payload = json.loads((produces_dir / "exec_result.json").read_text(encoding="utf-8"))
        assert payload["returncode"] == 7
        assert payload["termination_status"] == "remote_failed"
        assert payload["artifact_paths"] == [{"path": "remote.txt", "size_bytes": len("remote artifact")}]
        assert payload["breadcrumbs"]["pod_id"] == "pod-abc123"
        assert "diagnostics_path" in payload
        assert Path(payload["diagnostics_path"]).is_file()
        assert (produces_dir / "cost.json").is_file()
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


def test_exec_success_copies_only_substrate_returned_artifact_root(
    produces_dir: Path,
    tmp_path: Path,
    mock_get_pod: MagicMock,
) -> None:
    """Detached exec artifact_dir is populated only from result.artifact_root."""
    handle = {
        "pod_id": "pod-abc123",
        "ssh": "root@1.2.3.4 -p 2222",
        "name": "astrid-test-pod-1700000000",
        "name_prefix": "astrid-test",
        "terminate_at": "2099-01-01T00:00:00Z",
        "gpu_type": "NVIDIA GeForce RTX 4090",
        "hourly_rate": 0.34,
        "provisioned_at": "2024-01-01T00:00:00Z",
        "config_snapshot": {
            "api_key_ref": "RUNPOD_API_KEY",
            "datacenter_id": "US-GA-1",
            "image": "runpod/pytorch:latest",
            "container_disk_in_gb": 200,
            "volume_in_gb": 0,
            "network_volume_id": None,
            "ports": "8888/http,22/tcp",
        },
    }
    handle_path = produces_dir / "pod_handle.json"
    handle_path.write_text(json.dumps(handle))

    artifact_root = tmp_path / "substrate-artifacts"
    artifact_root.mkdir()
    (artifact_root / "remote.txt").write_text("remote artifact", encoding="utf-8")
    (artifact_root / "nested").mkdir()
    (artifact_root / "nested" / "manifest.json").write_text("{}", encoding="utf-8")
    local_root = tmp_path / "local-root"
    local_root.mkdir()
    (local_root / "must-not-copy.txt").write_text("local", encoding="utf-8")
    local_root_arg = str(local_root)

    result = MagicMock()
    result.returncode = 0
    result.stdout = "ok"
    result.stderr = ""
    result.terminated = False
    result.artifact_root = artifact_root
    result.breach_log = []
    ship = AsyncMock(return_value=result)

    import os

    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"
    try:
        with patch("runpod_lifecycle.get_pod", mock_get_pod), \
             patch("runpod_lifecycle.ship_and_run_detached", ship), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_exec

            args = MagicMock()
            args.pod_handle = str(handle_path)
            args.local_root = local_root_arg
            args.remote_root = "/workspace"
            args.remote_script = "echo ok"
            args.timeout = None
            args.upload_mode = None
            args.excludes = None
            args.produces_dir = produces_dir

            exit_code = cmd_exec(args, produces_dir)

        assert exit_code == 0
        _assert_detached_call_uses_v03_contract(ship)
        artifact_dir = produces_dir / "artifact_dir"
        assert (artifact_dir / "remote.txt").read_text(encoding="utf-8") == "remote artifact"
        assert (artifact_dir / "nested" / "manifest.json").read_text(encoding="utf-8") == "{}"
        assert not (artifact_dir / "must-not-copy.txt").exists()
        payload = json.loads((produces_dir / "exec_result.json").read_text(encoding="utf-8"))
        assert payload["termination_status"] == "completed"
        assert payload["artifact_paths"] == [
            {"path": "nested/manifest.json", "size_bytes": 2},
            {"path": "remote.txt", "size_bytes": len("remote artifact")},
        ]
        assert "diagnostics_path" not in payload
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


# ---------------------------------------------------------------------------
# Teardown executor
# ---------------------------------------------------------------------------


def test_teardown_terminates_and_writes_receipt(
    produces_dir: Path,
    mock_get_pod: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """Teardown executor terminates the pod and writes teardown_receipt.json."""
    handle = {
        "pod_id": "pod-abc123",
        "ssh": "root@1.2.3.4 -p 2222",
        "name": "astrid-test-pod-1700000000",
        "name_prefix": "astrid-test",
        "terminate_at": "2099-01-01T00:00:00Z",
        "gpu_type": "NVIDIA GeForce RTX 4090",
        "hourly_rate": 0.34,
        "provisioned_at": "2024-01-01T00:00:00Z",
        "config_snapshot": {
            "api_key_ref": "RUNPOD_API_KEY",
            "datacenter_id": "US-GA-1",
            "image": "runpod/pytorch:latest",
            "container_disk_in_gb": 200,
            "volume_in_gb": 0,
            "network_volume_id": None,
            "ports": "8888/http,22/tcp",
        },
    }
    handle_path = produces_dir / "pod_handle.json"
    handle_path.write_text(json.dumps(handle))

    import os
    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"

    try:
        with patch("runpod_lifecycle.get_pod", mock_get_pod), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_teardown

            class Args:
                pod_handle = str(handle_path)
                produces_dir = produces_dir

            exit_code = cmd_teardown(Args(), produces_dir)
            assert exit_code == 0

            # Verify teardown_receipt.json
            receipt_path = produces_dir / "teardown_receipt.json"
            assert receipt_path.is_file(), "teardown_receipt.json not written"
            receipt = json.loads(receipt_path.read_text())
            assert receipt["pod_id"] == "pod-abc123"
            assert receipt["status"] in ("terminated", "already_gone")

            # Verify cost.json
            cost_path = produces_dir / "cost.json"
            assert cost_path.is_file(), "cost.json not written"
            cost = json.loads(cost_path.read_text())
            _assert_cost_shape(cost)
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


def test_teardown_idempotent_pod_not_found(
    produces_dir: Path,
    mock_pod: MagicMock,
) -> None:
    """Teardown is idempotent — 'not found' produces already_gone receipt."""
    handle = {
        "pod_id": "pod-gone123",
        "ssh": "root@1.2.3.4 -p 2222",
        "name": "astrid-test-gone",
        "name_prefix": "astrid-test",
        "terminate_at": "2099-01-01T00:00:00Z",
        "gpu_type": "NVIDIA GeForce RTX 4090",
        "hourly_rate": 0.34,
        "provisioned_at": "2024-01-01T00:00:00Z",
        "config_snapshot": {
            "api_key_ref": "RUNPOD_API_KEY",
            "datacenter_id": "US-GA-1",
            "image": "runpod/pytorch:latest",
            "container_disk_in_gb": 200,
            "volume_in_gb": 0,
            "network_volume_id": None,
            "ports": "8888/http,22/tcp",
        },
    }
    handle_path = produces_dir / "pod_handle.json"
    handle_path.write_text(json.dumps(handle))

    # Mock get_pod to raise a "not found" error
    not_found_pod = MagicMock()
    not_found_pod.terminate = AsyncMock(side_effect=Exception("pod not found or 404"))
    mock_get_pod_not_found = AsyncMock(return_value=not_found_pod)

    import os
    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"

    try:
        with patch("runpod_lifecycle.get_pod", mock_get_pod_not_found), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_teardown

            class Args:
                pod_handle = str(handle_path)
                produces_dir = produces_dir

            exit_code = cmd_teardown(Args(), produces_dir)
            # Should succeed because not-found is a no-op
            assert exit_code == 0

            receipt_path = produces_dir / "teardown_receipt.json"
            receipt = json.loads(receipt_path.read_text())
            assert receipt["status"] == "already_gone"
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


# ---------------------------------------------------------------------------
# Session executor
# ---------------------------------------------------------------------------


def test_session_writes_breadcrumb_and_deletes_on_teardown(
    produces_dir: Path,
    mock_launch: MagicMock,
    mock_ship_and_run_detached: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """Session writes pod_handle.json immediately and deletes it on graceful teardown."""
    import os
    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"

    try:
        with patch("runpod_lifecycle.launch", mock_launch), \
             patch("runpod_lifecycle.get_pod", AsyncMock(return_value=mock_pod)), \
             patch("runpod_lifecycle.ship_and_run_detached", mock_ship_and_run_detached), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_session

            class Args:
                gpu_type = None
                storage_name = None
                max_runtime_seconds = None
                name_prefix = None
                image = None
                container_disk_gb = None
                datacenter_id = None
                local_root = None
                remote_root = None
                remote_script = "echo ok"
                timeout = None
                upload_mode = None
                excludes = None
                produces_dir = produces_dir

            exit_code = cmd_session(Args(), produces_dir)
            assert exit_code == 0
            _assert_detached_call_uses_v03_contract(mock_ship_and_run_detached)

            # pod_handle.json should be deleted after graceful teardown
            handle_path = produces_dir / "pod_handle.json"
            assert not handle_path.exists(), (
                "pod_handle.json should be deleted after graceful session teardown"
            )

            # exec_result.json should exist
            result_path = produces_dir / "exec_result.json"
            assert result_path.is_file(), "exec_result.json not written"

            # cost.json should exist
            cost_path = produces_dir / "cost.json"
            assert cost_path.is_file(), "cost.json not written"
            cost = json.loads(cost_path.read_text())
            _assert_cost_shape(cost)
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


def test_session_transient_handle_exists_during_detached_exec_and_is_removed_after(
    produces_dir: Path,
    mock_launch: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """Session writes the canonical sweeper breadcrumb before detached exec starts."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "ok"
    result.stderr = ""
    result.terminated = False
    result.artifact_root = None
    result.breach_log = []

    async def ship_and_assert_handle(*args, **kwargs):
        handle_path = produces_dir / "pod_handle.json"
        assert handle_path.is_file()
        handle = json.loads(handle_path.read_text(encoding="utf-8"))
        _assert_pod_handle_shape(handle)
        assert handle["config_snapshot"]["storage_name"] == "astrid-storage"
        assert handle["config_snapshot"]["network_volume_id"] == "vol-astrid-storage"
        return result

    ship = AsyncMock(side_effect=ship_and_assert_handle)

    import os

    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"
    mock_pod._storage_volume = "vol-astrid-storage"
    try:
        with patch("runpod_lifecycle.launch", mock_launch), \
             patch("runpod_lifecycle.get_pod", AsyncMock(return_value=mock_pod)), \
             patch("runpod_lifecycle.api.get_network_volumes", return_value=[{"id": "vol-astrid-storage", "name": "astrid-storage"}]), \
             patch("runpod_lifecycle.ship_and_run_detached", ship), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_session

            class Args:
                gpu_type = None
                storage_name = "astrid-storage"
                require_storage = False
                max_runtime_seconds = None
                name_prefix = None
                image = None
                container_disk_gb = None
                datacenter_id = None
                local_root = None
                remote_root = None
                remote_script = "echo ok"
                timeout = None
                upload_mode = None
                excludes = None
                produces_dir = produces_dir

            exit_code = cmd_session(Args(), produces_dir)

        assert exit_code == 0
        _assert_detached_call_uses_v03_contract(ship)
        assert not (produces_dir / "pod_handle.json").exists()
        assert (produces_dir / "exec_result.json").is_file()
        assert (produces_dir / "cost.json").is_file()
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


def test_session_captures_pod_id_before_readiness_failure_and_terminates(
    produces_dir: Path,
    mock_launch: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """A post-allocation readiness failure still has provider cleanup custody."""
    import os

    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"
    mock_pod.wait_ready = AsyncMock(side_effect=TimeoutError("readiness timeout"))
    try:
        with patch("runpod_lifecycle.launch", mock_launch), \
             patch("runpod_lifecycle.get_pod", AsyncMock(return_value=mock_pod)), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()), \
             patch("astrid.packs.runpod.shared.common._terminate_pod_id", AsyncMock(return_value=False)) as cleanup:
            from astrid.packs.runpod.actions.provision.run import cmd_session

            class Args:
                gpu_type = None
                storage_name = None
                require_storage = False
                max_runtime_seconds = None
                name_prefix = None
                image = None
                container_disk_gb = None
                datacenter_id = None
                local_root = None
                remote_root = None
                remote_script = "echo never"
                timeout = None
                upload_mode = None
                excludes = None
                produces_dir = produces_dir

            with pytest.raises(AstridError, match="readiness timeout"):
                cmd_session(Args(), produces_dir)

            provisional = json.loads((produces_dir / "pod_handle.json").read_text(encoding="utf-8"))
            assert provisional["pod_id"] == "pod-abc123"
            assert provisional["state"] == "cleanup_pending"
            assert provisional["cleanup_pending"] is True
            assert provisional["cleanup_phase"] == "session_teardown"
            cleanup.assert_awaited_once_with("pod-abc123", ANY, name="astrid-test-pod-1700000000")
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


def test_session_mocked_artifact_smoke_uses_substrate_output_directory(
    produces_dir: Path,
    tmp_path: Path,
    mock_launch: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """Mocked GPU smoke writes to output/, which v0.3 collects as artifact_root."""
    artifact_root = tmp_path / "substrate-smoke-artifacts"
    output_dir = artifact_root / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "smoke.txt").write_text("smoke\n", encoding="utf-8")

    result = MagicMock()
    result.returncode = 0
    result.stdout = "ok\n"
    result.stderr = ""
    result.terminated = False
    result.artifact_root = artifact_root
    result.breach_log = []

    async def ship_and_assert_smoke_contract(*args, **kwargs):
        assert kwargs["remote_script"] == _SESSION_SMOKE_SCRIPT
        assert "nvidia-smi -L" in kwargs["remote_script"]
        assert "echo ok" in kwargs["remote_script"]
        assert "mkdir -p output" in kwargs["remote_script"]
        assert "output/smoke.txt" in kwargs["remote_script"]
        assert "artifact_paths" not in kwargs
        assert produces_dir.joinpath("pod_handle.json").is_file()
        return result

    ship = AsyncMock(side_effect=ship_and_assert_smoke_contract)

    import os

    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"
    try:
        with patch("runpod_lifecycle.launch", mock_launch), \
             patch("runpod_lifecycle.get_pod", AsyncMock(return_value=mock_pod)), \
             patch("runpod_lifecycle.ship_and_run_detached", ship), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_session

            args = MagicMock()
            args.gpu_type = None
            args.storage_name = None
            args.require_storage = False
            args.max_runtime_seconds = None
            args.name_prefix = None
            args.image = None
            args.container_disk_gb = None
            args.datacenter_id = None
            args.ports = None
            args.local_root = None
            args.remote_root = None
            args.remote_script = _SESSION_SMOKE_SCRIPT
            args.timeout = None
            args.upload_mode = None
            args.excludes = None
            args.produces_dir = produces_dir

            exit_code = cmd_session(args, produces_dir)

        assert exit_code == 0
        _assert_detached_call_uses_v03_contract(ship)
        assert not (produces_dir / "pod_handle.json").exists()
        assert (produces_dir / "artifact_dir" / "output" / "smoke.txt").read_text(encoding="utf-8") == "smoke\n"
        assert any((produces_dir / "artifact_dir").rglob("*"))

        payload = json.loads((produces_dir / "exec_result.json").read_text(encoding="utf-8"))
        assert payload["returncode"] == 0
        assert payload["stdout"] == "ok\n"
        assert payload["artifact_paths"] == [
            {"path": "output/smoke.txt", "size_bytes": len("smoke\n")}
        ]
        _assert_cost_shape(json.loads((produces_dir / "cost.json").read_text(encoding="utf-8")))
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


def test_session_breadcrumb_survives_on_crash(
    produces_dir: Path,
    mock_launch: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """When exec raises a Python exception, the finally block still runs
    (so handle is deleted). The breadcrumb survives only when the Python
    process itself crashes (SIGKILL, OOM-kill, machine reboot) — those
    are tested in test_session_oom_breadcrumb.py."""

    import os
    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"

    # Make ship_and_run_detached raise to simulate a crash during exec
    crash_mock = AsyncMock(side_effect=RuntimeError("simulated exec crash"))

    try:
        with patch("runpod_lifecycle.launch", mock_launch), \
             patch("runpod_lifecycle.get_pod", AsyncMock(return_value=mock_pod)), \
             patch("runpod_lifecycle.ship_and_run_detached", crash_mock), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_session

            class Args:
                gpu_type = None
                storage_name = None
                max_runtime_seconds = None
                name_prefix = None
                image = None
                container_disk_gb = None
                datacenter_id = None
                local_root = None
                remote_root = None
                remote_script = "echo ok"
                timeout = None
                upload_mode = None
                excludes = None
                produces_dir = produces_dir

            with pytest.raises(AstridError, match="simulated exec crash"):
                cmd_session(Args(), produces_dir)

            # When a Python exception is raised and caught by except,
            # the finally block still runs → handle is deleted.
            # This is the graceful path. The sweeper breadcrumb is for
            # process-level crashes (SIGKILL/OOM) where finally never runs.
            handle_path = produces_dir / "pod_handle.json"
            assert not handle_path.exists(), (
                "pod_handle.json is deleted in finally when Python exception "
                "is caught — this is expected. Process-level crashes (SIGKILL) "
                "are tested in test_session_oom_breadcrumb.py"
            )
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


def test_session_keeps_breadcrumb_when_teardown_fails(
    produces_dir: Path,
    mock_launch: MagicMock,
    mock_ship_and_run_detached: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """Session reports cleanup_pending instead of returning success on teardown failure."""
    import os
    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"
    mock_pod.terminate = AsyncMock(side_effect=RuntimeError("teardown transport failed"))

    try:
        with patch("runpod_lifecycle.launch", mock_launch), \
             patch("runpod_lifecycle.get_pod", AsyncMock(return_value=mock_pod)), \
             patch("runpod_lifecycle.ship_and_run_detached", mock_ship_and_run_detached), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_session

            class Args:
                gpu_type = None
                storage_name = None
                max_runtime_seconds = None
                name_prefix = None
                image = None
                container_disk_gb = None
                datacenter_id = None
                local_root = None
                remote_root = None
                remote_script = "echo ok"
                timeout = None
                upload_mode = None
                excludes = None
                produces_dir = produces_dir

            with pytest.raises(AstridError, match="cleanup is pending"):
                cmd_session(Args(), produces_dir)

            handle_path = produces_dir / "pod_handle.json"
            assert handle_path.is_file(), "pod_handle.json must remain when teardown fails"
            handle = json.loads(handle_path.read_text())
            _assert_pod_handle_shape(handle)
            assert handle["state"] == "cleanup_pending"
            assert handle["cleanup_pending"] is True
            assert handle["cleanup_phase"] == "session_teardown"
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


def test_session_nonzero_remote_exit_writes_diagnostics_artifacts_result_and_cost(
    produces_dir: Path,
    tmp_path: Path,
    mock_launch: MagicMock,
    mock_pod: MagicMock,
) -> None:
    """Composite session preserves failed remote-exec evidence before teardown."""
    artifact_root = tmp_path / "session-artifacts"
    artifact_root.mkdir()
    (artifact_root / "log.txt").write_text("failure log", encoding="utf-8")

    result = MagicMock()
    result.returncode = 9
    result.stdout = "started"
    result.stderr = "failed"
    result.terminated = False
    result.artifact_root = artifact_root
    result.breach_log = []
    ship = AsyncMock(return_value=result)

    import os
    os.environ["RUNPOD_API_KEY"] = "test-key-rpa_0000000000000000000000000000000000000000000000"

    try:
        with patch("runpod_lifecycle.launch", mock_launch), \
             patch("runpod_lifecycle.get_pod", AsyncMock(return_value=mock_pod)), \
             patch("runpod_lifecycle.ship_and_run_detached", ship), \
             patch("runpod_lifecycle.RunPodConfig", MagicMock()):
            from astrid.packs.runpod.actions.provision.run import cmd_session

            class Args:
                gpu_type = None
                storage_name = None
                require_storage = False
                max_runtime_seconds = None
                name_prefix = None
                image = None
                container_disk_gb = None
                datacenter_id = None
                ports = None
                local_root = None
                remote_root = None
                remote_script = "exit 9"
                timeout = None
                upload_mode = None
                excludes = None
                produces_dir = produces_dir

            exit_code = cmd_session(Args(), produces_dir)

        assert exit_code == 9
        _assert_detached_call_uses_v03_contract(ship)
        assert not (produces_dir / "pod_handle.json").exists()
        assert (produces_dir / "artifact_dir" / "log.txt").read_text(encoding="utf-8") == "failure log"
        payload = json.loads((produces_dir / "exec_result.json").read_text(encoding="utf-8"))
        assert payload["returncode"] == 9
        assert payload["termination_status"] == "remote_failed"
        assert Path(payload["diagnostics_path"]).is_file()
        assert (produces_dir / "cost.json").is_file()
    finally:
        if os.environ.get("RUNPOD_API_KEY") == "test-key-rpa_0000000000000000000000000000000000000000000000":
            del os.environ["RUNPOD_API_KEY"]


# ---------------------------------------------------------------------------
# Cost summation invariant
# ---------------------------------------------------------------------------


def test_cost_summation_invariant() -> None:
    """provision.cost + exec.cost + teardown.cost ≈ session.cost (same hourly_rate)."""
    hourly_rate = 0.34

    # Simulate the three partial costs (using the _cost_amount + _cost_entry helpers)
    from astrid.packs.runpod.shared.common import _cost_amount, _cost_entry

    prov_duration = 45.0
    exec_duration = 120.0
    tear_duration = 15.0
    session_duration = prov_duration + exec_duration + tear_duration

    prov_cost = _cost_entry(_cost_amount(prov_duration, hourly_rate), "runpod", "provision")
    exec_cost = _cost_entry(_cost_amount(exec_duration, hourly_rate), "runpod", "exec")
    tear_cost = _cost_entry(_cost_amount(tear_duration, hourly_rate), "runpod", "teardown")
    sess_cost = _cost_entry(_cost_amount(session_duration, hourly_rate), "runpod", "session")

    trio_sum = prov_cost["amount"] + exec_cost["amount"] + tear_cost["amount"]
    session_amount = sess_cost["amount"]

    # Allow tiny floating-point difference
    assert abs(trio_sum - session_amount) < 0.001, (
        f"Cost summation invariant broken: "
        f"provision={prov_cost['amount']} + exec={exec_cost['amount']} + "
        f"teardown={tear_cost['amount']} = {trio_sum} != "
        f"session={session_amount}"
    )

    # Verify all cost shapes
    for cost in (prov_cost, exec_cost, tear_cost, sess_cost):
        _assert_cost_shape(cost)
