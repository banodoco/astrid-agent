from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import (
    StorageEnvelopeError,
    _assert_live_storage_envelope,
    _render_media_roots,
    _task_storage_envelope,
    _task_storage_limits,
)
from astrid.core.execution.guards import EvidenceCapError, ExecutionGuardPolicy


@pytest.fixture
def render(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRID_RUNTIME_FREE_DISK_FLOOR_BYTES", raising=False)
    output = tmp_path / "outputs"
    workspace = tmp_path / ".video.mp4.render-service-fixture"
    output.mkdir()
    workspace.mkdir()
    (output / "video.mp4").write_bytes(b"video exceeds its estimate")
    (workspace / "frame.jpeg").write_bytes(b"scratch also exceeds its estimate")
    task = {"capability_id": "rendering.render", "storage_estimate": {"scratch_bytes": 1, "output_bytes": 1}}
    return task, tmp_path, output


@pytest.mark.parametrize("free", [1024**3, 8 * 1024**3])
def test_render_exceeds_both_estimates_and_settles_while_volume_has_room(render, monkeypatch, free):
    task, root, output = render
    monkeypatch.setattr("astrid.core.execution.generic_host.shutil.disk_usage", lambda _: SimpleNamespace(free=free))
    limits = _task_storage_limits(task)
    assert limits is None
    _assert_live_storage_envelope(limits, root, output)
    receipt = _task_storage_envelope(task, root, [{"path": str(output / "video.mp4")}])
    assert receipt["scratch_bytes"] > receipt["estimated_scratch_bytes"]
    assert receipt["output_bytes"] > receipt["estimated_output_bytes"]


def test_render_stops_when_actual_free_volume_crosses_floor(render, monkeypatch):
    task, root, output = render
    free = {"bytes": 1024**3}
    monkeypatch.setattr("astrid.core.execution.generic_host.shutil.disk_usage", lambda _: SimpleNamespace(free=free["bytes"]))
    _assert_live_storage_envelope(_task_storage_limits(task), root, output)
    free["bytes"] -= 1
    with pytest.raises(StorageEnvelopeError, match="Free disk space") as failure:
        _assert_live_storage_envelope(_task_storage_limits(task), root, output)
    assert failure.value.diagnostic["available_bytes"] == 1024**3 - 1
    assert failure.value.diagnostic["floor_bytes"] == 1024**3
    assert failure.value.diagnostic["guard"] == "free_disk_space"


def test_render_settlement_checks_free_space_without_an_estimate(render, monkeypatch):
    task, root, output = render
    task.pop("storage_estimate")
    monkeypatch.setattr("astrid.core.execution.generic_host.shutil.disk_usage", lambda _: SimpleNamespace(free=1024**3 - 1))
    with pytest.raises(StorageEnvelopeError, match="Free disk space"):
        _task_storage_envelope(task, root, [{"path": str(output / "video.mp4")}])


def test_render_media_does_not_consume_diagnostic_evidence_budget(render):
    _, root, _ = render
    policy = ExecutionGuardPolicy(evidence_cap_bytes=2)
    assert policy.assert_evidence_cap(root, excluded_roots=_render_media_roots(root))["observed_bytes"] == 0
    (root / "diagnostic.log").write_bytes(b"log")
    with pytest.raises(EvidenceCapError):
        policy.assert_evidence_cap(root, excluded_roots=_render_media_roots(root))


def test_render_evidence_exclusions_handle_a_symlinked_parent(tmp_path):
    root = tmp_path / "real"
    (root / "outputs").mkdir(parents=True)
    (root / "outputs" / "video.mp4").write_bytes(b"media")
    (root / "diagnostic.log").write_bytes(b"ok")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    receipt = ExecutionGuardPolicy(evidence_cap_bytes=2).assert_evidence_cap(
        alias, excluded_roots=_render_media_roots(alias),
    )
    assert receipt["observed_bytes"] == 2


def test_render_estimate_does_not_disable_output_path_validation(render, monkeypatch):
    task, root, _ = render
    monkeypatch.setattr("astrid.core.execution.generic_host.shutil.disk_usage", lambda _: SimpleNamespace(free=8 * 1024**3))
    outside = root / "outside.mp4"
    outside.write_bytes(b"video")
    with pytest.raises(RuntimeError, match="escapes the output directory"):
        _task_storage_envelope(task, root, [{"path": str(outside)}])


def test_non_render_pack_retains_its_bounded_storage_contract(render, monkeypatch):
    task, root, output = render
    task["capability_id"] = "generation.generate_image"
    monkeypatch.setattr("astrid.core.execution.generic_host.shutil.disk_usage", lambda _: SimpleNamespace(free=8 * 1024**3))
    with pytest.raises(StorageEnvelopeError, match="exceed task output limit"):
        _assert_live_storage_envelope(_task_storage_limits(task), root, output)


@pytest.mark.parametrize("capability", ["runpod.release", "runpod.status"])
def test_machine_control_observation_and_release_ignore_host_disk_floor(render, monkeypatch, capability):
    _, root, _ = render
    monkeypatch.setattr("astrid.core.execution.generic_host.shutil.disk_usage",
                        lambda _: SimpleNamespace(free=99_332_096))
    from pathlib import Path
    from astrid.core.execution.generic_host import GenericPackHost

    host = GenericPackHost(pack_roots=[Path(__file__).resolve().parents[3] / "astrid/packs/runpod"])
    host.discover()
    try:
        assert {key for key, record in host.capabilities.items() if record.storage_floor_exempt} == {
            "runpod.release", "runpod.status"}
        exempt = host.capabilities[capability].storage_floor_exempt
        assert exempt
        assert _task_storage_envelope({"capability_id": capability}, root, [], storage_floor_exempt=exempt) is None
        _assert_live_storage_envelope({"scratch_bytes": 0, "output_bytes": 0}, root, root / "outputs",
                                      storage_floor_exempt=exempt)
    finally:
        host.shutdown()
