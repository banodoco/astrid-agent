"""CPU-only checks of the setup guide's decision branches and command surfaces."""
from __future__ import annotations

import json
import csv
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from astrid.core.generation import engine_readiness
from astrid.packs.vibecomfy.local import scan
from tests.packs.runpod.test_machine import machine_case as machine_case  # noqa: F401
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401
from tests.packs.runpod.test_warm_session import (  # noqa: F401
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)
from tests.packs.test_vibecomfy_local import _ComfyHandler, _server


_SETUP = Path(__file__).resolve().parents[1] / "docs" / "setup" / "SKILL.md"


def _fenced(command_fragment: str) -> str:
    text = _SETUP.read_text()
    return next(block.strip() for block in re.findall(r"```sh\s*\n(.*?)\n```", text, re.S)
                if command_fragment in block)


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


def test_setup_engine_fenced_command_reaches_real_parser_through_fake_launcher(tmp_path):
    data = tmp_path / "data"
    launcher = data / "astrid" / "run-astrid"
    launcher.parent.mkdir(parents=True)
    call_log = tmp_path / "launcher-argv"
    launcher.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(call_log))}\n"
        f"exec {shlex.quote(sys.executable)} -c "
        "'import sys; from astrid.core.gateway import main; "
        "raise SystemExit(main(sys.argv[1:]))' \"$@\"\n"
    )
    launcher.chmod(0o755)
    root = tmp_path / "engine"
    command = _fenced("engines install comfyui --dry-run")
    env = {**os.environ, "XDG_DATA_HOME": str(data), "ASTRID_ENGINE_ROOT": str(root),
           "PYTHONPATH": str(_SETUP.parents[2])}
    result = subprocess.run(command, shell=True, cwd=_SETUP.parents[2], env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert call_log.read_text().splitlines() == ["engines", "install", "comfyui", "--dry-run"]
    output = json.loads(result.stdout)
    assert output["recipe"]["engine"] == "comfyui"
    assert output["plan"]["steps"]
    assert output["disk_check"]["status"] == "ok"
    assert not root.exists()


@pytest.mark.parametrize("memory_mib, capable", [(11445, True), (11444, False)])
def test_nvidia_fenced_command_and_csv_cross_the_12_decimal_gb_boundary(tmp_path, memory_mib, capable):
    smi = tmp_path / "nvidia-smi"
    argv_log = tmp_path / "nvidia-argv"
    smi.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_log))}\n"
        f"echo 'Synthetic GPU, {memory_mib} MiB'\n"
    )
    smi.chmod(0o755)
    command = _fenced("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader")
    result = subprocess.run(command, shell=True, env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0
    assert argv_log.read_text().splitlines() == ["--query-gpu=name,memory.total", "--format=csv,noheader"]
    name, memory = next(csv.reader([result.stdout.strip()], skipinitialspace=True))
    parsed_mib = int(memory.removesuffix(" MiB"))
    assert name == "Synthetic GPU"
    assert parsed_mib == memory_mib
    # The guide says decimal GB; nvidia-smi reports MiB. Convert the actual
    # MiB count to bytes before applying 12,000,000,000 bytes.
    observed_bytes = parsed_mib * 1024 * 1024
    assert (observed_bytes >= 12_000_000_000) is capable


def test_engine_readiness_parser_uses_its_distinct_query(tmp_path):
    argv_log = tmp_path / "readiness-argv"
    smi = tmp_path / "nvidia-smi"
    smi.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_log))}\n"
        "echo '550.54.15, 12288'\n"
    )
    smi.chmod(0o755)
    observed = engine_readiness.probe_hardware(time.monotonic() + 5, executable=str(smi))
    assert argv_log.read_text().splitlines() == [
        "--query-gpu=driver_version,memory.total", "--format=csv,noheader,nounits",
    ]
    assert observed["vram_bytes"] == 12 * 1024**3


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
