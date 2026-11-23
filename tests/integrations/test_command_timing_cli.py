"""Fresh CLI process through admission against the local Runtime and host."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401
from tests.integrations.test_runpod_slot_path_cpu import _drive
from tests.integrations.test_workflow_file_placements_cpu import INPUTS, _workflow_file


@pytest.mark.integration
@pytest.mark.timeout(600)
def test_fresh_cli_reports_elapsed_time_through_admission(tmp_path, monkeypatch, real_runtime, tuple_sources):
    connection = tmp_path / "prepared-connection.json"

    def capture_runtime(*args, **kwargs):
        daemon = real_runtime(*args, **kwargs)
        connection.write_text(json.dumps({
            "endpoint": daemon.endpoint,
            "credential_file": str(daemon.credential_path),
            "realm_id": daemon.service.realm["id"],
            "actor_id": "owner",
        }))
        return daemon

    def call(_facade, controls, _tmp):
        command = [str(Path(sys.executable).with_name("astrid")), "generate", "workflow", str(_workflow_file()),
                   "--prompt", INPUTS["prompt"], "--placement", controls["placement"],
                   "--project", controls["project"], "--json", "--timings", "--no-wait"]
        start = time.monotonic()
        result = subprocess.run(command, env=dict(os.environ, ASTRID_PREPARED_CONNECTION=str(connection)),
                                capture_output=True, text=True, timeout=180)
        return result, time.monotonic() - start

    with _drive(("image", "t2i"), tmp_path, monkeypatch, capture_runtime, tuple_sources,
                call=call, target_kind="attached") as (client, _project, _provider, measured):
        result, wall = measured
        assert result.returncode == 0, result.stderr[-4000:] + result.stdout[-4000:]
        payload = json.loads(result.stdout)
        task_id = payload.get("kernel_task_id") or payload.get("task_id")
        assert task_id, payload
        assert client.tasks.show(task_id).ok
        rows = payload["timings"]
        assert rows and all(row["seconds"] >= 0 for row in rows)
        assert 0.9 * wall <= sum(row["seconds"] for row in rows) <= 1.1 * wall
