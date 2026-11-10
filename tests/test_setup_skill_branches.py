"""CPU-only checks of the setup guide's decision branches and command surfaces."""
from __future__ import annotations

import json
import time
import pytest

from astrid.core.generation import engine_readiness
from astrid.core import engine_cli
from astrid.packs.vibecomfy.local import scan
from tests.packs.runpod.test_machine import machine_case as machine_case  # noqa: F401
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401
from tests.packs.runpod.test_warm_session import (  # noqa: F401
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)
from tests.packs.test_vibecomfy_local import _ComfyHandler, _server


def test_discovery_against_fake_comfyui_is_read_only(tmp_path):
    server, thread = _server(tmp_path)
    try:
        result = scan([server.server_address[1]])
        assert result["errors"] == []
        assert result["servers"][0]["workflows"] == ["portrait_flux.json", "nested/wan_i2v.json"]
        assert result["servers"][0]["url"] == f"http://127.0.0.1:{server.server_address[1]}"
        assert set(_ComfyHandler.requested) == {
            "/system_stats", "/internal/folder_paths", "/userdata?dir=workflows",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_setup_engine_command_is_a_real_read_only_plan(capsys, tmp_path):
    root = tmp_path / "engine"
    code = engine_cli.main(["install", "comfyui", "--platform", "linux-x86_64-cu130",
                            "--root", str(root), "--dry-run"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["recipe"]["engine"] == "comfyui"
    assert output["plan"]["steps"]
    assert output["disk_check"]["status"] == "ok"
    assert not root.exists()


@pytest.mark.parametrize("memory_mib, capable", [(12288, True), (12287, False)])
def test_gpu_probe_handles_both_sides_of_setup_minimum(tmp_path, memory_mib, capable):
    smi = tmp_path / "nvidia-smi"
    smi.write_text(f"#!/bin/sh\necho '550.54.15, {memory_mib}'\n")
    smi.chmod(0o755)
    observed = engine_readiness.probe_hardware(time.monotonic() + 5, executable=str(smi))
    assert observed["driver"] == "550.54.15"
    assert observed["vram_bytes"] == memory_mib * 1024 * 1024
    # The guide's stated capacity boundary is 12 GB; this is the same 12 GiB
    # boundary used by the NVIDIA MiB probe.
    assert (observed["vram_bytes"] >= 12 * 1024**3) is capable


def test_runpod_machine_refuses_claim_without_paid_consent(machine_case):
    from dataclasses import replace
    from astrid.packs.runpod.target_selection import PaidSelectionError

    case = machine_case
    job = case.job()
    job = replace(job, policy={**job.policy, "consent": []})
    before = case.creates
    with pytest.raises(PaidSelectionError):
        case.machine.claim(job)
    assert case.creates == before
