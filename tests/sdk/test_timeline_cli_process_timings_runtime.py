"""Fresh CLI process acceptance for timeline edit timings."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.sdk.test_timeline_editing_acceptance_runtime import _media_file


@pytest.mark.integration
def test_fresh_cli_edit_reports_accepted_commit_and_covers_process_wall_time(tmp_path, real_runtime):
    daemon = real_runtime(authoring_profile="astrid")
    project = daemon.service.create_project({"slug": "cli-timing", "name": "CLI timing"})["id"]
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    client = open_client(daemon)
    created = client.timelines.create_empty(
        project=project, timeline_id="film", name="CLI timing film", idempotency_key="cli-timing-create",
    )
    assert created.ok, created.as_dict()
    from astrid.sdk.timeline import Timeline

    timeline = Timeline.open(project, "film", client=client, outbox_path=tmp_path / "seed-outbox.json")
    source = _media_file(tmp_path / "cli-timing-seed.png")
    seeded = timeline.commit(
        lambda t: t.add(source, at=0, hold=4, track="V1", id="timed-clip"),
        label="Seed CLI timing clip",
        key="cli-timing-seed",
    )
    assert seeded["receipt"]["receipt_id"]

    _, credential = daemon.credentials.provision("cli-timing-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    connection = tmp_path / "prepared-connection.json"
    connection.write_text(json.dumps({
        "endpoint": daemon.endpoint,
        "credential_file": str(credential),
        "realm_id": daemon.service.realm["id"],
        "actor_id": "cli-timing-peer",
    }), encoding="utf-8")
    env = dict(os.environ, ASTRID_PREPARED_CONNECTION=str(connection))
    repo_root = str(Path(__file__).parents[2])
    executable = Path(sys.executable).with_name("astrid")
    assert executable.is_file(), f"Astrid console executable is missing beside {sys.executable}"
    command = [
        str(executable), "timelines", "edit", "film",
        "--project", project, "--clip", "parent/clip:timed-clip", "--on", "1",
        "--idempotency-key", "cli-timing-edit", "--json", "--timings",
    ]
    started = time.monotonic()
    result = subprocess.run(command, cwd=repo_root, env=env, capture_output=True, text=True, check=False)
    wall = time.monotonic() - started
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["ok"] is True, result.stdout
    assert payload["data"]["accepted_revision_id"]
    assert payload["timings"]
    assert all(row["seconds"] >= 0 for row in payload["timings"])
    timed = sum(row["seconds"] for row in payload["timings"])
    assert 0.9 * wall <= timed <= 1.1 * wall, {
        "wall": wall, "timed": timed, "timings": payload["timings"], "stderr": result.stderr,
    }

    history = client.timelines.history(project, "film", limit=10)
    assert history.ok, history.as_dict()
    assert any(row.get("revision_id") == payload["data"]["accepted_revision_id"] for row in history.data["items"])
