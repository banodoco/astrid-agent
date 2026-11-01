"""Regression coverage for retrying an exhausted function by pending id."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.sdk.test_timeline_editing_acceptance_runtime import _media_file

pytest_plugins = ("tests.sdk.test_timeline_editing_acceptance_runtime",)


def test_retry_pending_id_overwrite_rebases_same_timeline_after_function_clash(
    editing_case, tmp_path,
):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    source = _media_file(tmp_path / "retry-overwrite-seed.png")
    timeline.commit(
        lambda t: [t.add(source, at=index * 5, hold=4, track="V1", id=f"clip-{index}")
                   for index in range(1, 4)],
        label="Seed retry clips",
    )
    _, credential = daemon.credentials.provision("retry-overwrite-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer = Timeline.open(
        project,
        "film",
        client=open_client(daemon, actor="retry-overwrite-peer", credential=credential),
        outbox_path=tmp_path / "retry-overwrite-peer.json",
    )
    peer.clip("clip-3").set_duration(3.0)
    peer_edit = peer.commit(label="Peer clip duration", key="retry-overwrite-peer-edit")
    assert peer_edit["receipt"]["receipt_id"]

    def set_all_durations(t):
        for index in range(1, 4):
            t.clip(f"clip-{index}").set_duration(2.0)

    with pytest.raises(CommitConflict) as caught:
        timeline.commit(set_all_durations, label="Retry overwrite durations", key="retry-overwrite")
    pending_id = caught.value.pending_id
    assert pending_id

    rebased = timeline.retry(pending_id, overwrite=True)

    assert rebased["status"] == "rebased"
    assert "Rebased" in rebased["summary"]
    assert timeline.pending_show(pending_id)["state"] == "landed"
    assert json.loads(timeline.session.outbox.path.read_text())["flight"] is None

    subsequent = timeline.commit(
        lambda t: t.bundle["parent"].update({"custom": {"after_retry": True}}),
        label="Commit after retry",
        key="retry-overwrite-followup",
    )
    assert subsequent["receipt"]["receipt_id"]
    assert timeline.bundle["parent"]["custom"] == {"after_retry": True}


@pytest.mark.parametrize("overlap", [False, True], ids=["disjoint", "overlap"])
def test_function_rerun_against_second_process_timelines_edit(editing_case, tmp_path, overlap):
    daemon, _client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitConflict

    source = _media_file(tmp_path / "cli-rerun-seed.png")
    timeline.commit(
        lambda t: [
            t.add(source, at=index * 5, hold=4, track="V1", id=f"cli-clip-{index}")
            for index in range(2)
        ],
        label="Seed CLI rerun clips",
        key=f"cli-rerun-seed-{overlap}",
    )
    _, credential = daemon.credentials.provision("recheck-cli-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    cli_env = {
        **os.environ,
        "ASTRID_TIMELINE_EVAL_ENDPOINT": daemon.endpoint,
        "ASTRID_TIMELINE_EVAL_CREDENTIAL": str(credential),
        "ASTRID_TIMELINE_EVAL_REALM_ID": daemon.service.realm["id"],
        "ASTRID_TIMELINE_EVAL_ACTOR_ID": "recheck-cli-peer",
    }
    repo_root = str(Path(__file__).parents[2])
    cli_env["PYTHONPATH"] = os.pathsep.join(
        [repo_root, cli_env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    target = "parent/clip:cli-clip-0" if overlap else "parent/clip:cli-clip-1"
    evaluations = []

    def edit(t):
        evaluations.append(t.clip("cli-clip-0").at)
        if len(evaluations) == 1:
            result = subprocess.run(
                [sys.executable, "-m", "astrid", "timelines", "edit", "film",
                 "--project", project, "--clip", target, "--on", "8",
                 "--idempotency-key", f"cli-rerun-peer-{overlap}", "--json"],
                cwd=repo_root,
                env=cli_env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert json.loads(result.stdout)["ok"], result.stdout
        t.move("parent/clip:cli-clip-0", 1.0)

    if overlap:
        with pytest.raises(CommitConflict) as caught:
            timeline.commit(edit, label="Function overlaps CLI edit", key="cli-overlap-function")
        assert caught.value.pending_id
        saved = timeline.pending_show(caught.value.pending_id)
        assert saved["state"] == "pending"
        assert saved["origin"] == "function"
        assert saved["clash"]["schema"] == "astrid.clash/2"
        assert any(
            conflict["path"][-1] == "at"
            and any(part.endswith("cli-clip-0") for part in conflict["path"])
            for conflict in saved["clash"]["conflicts"]
        ), saved["clash"]["conflicts"]
        assert len(evaluations) == 2
    else:
        result = timeline.commit(
            edit, label="Function reruns after CLI edit", key="cli-disjoint-function"
        )
        assert result["receipt"]["receipt_id"]
        assert len(evaluations) == 2
        assert evaluations == [0.0, 0.0]
        assert timeline.clip("cli-clip-0").at == pytest.approx(1.0)
        assert timeline.clip("cli-clip-1").at == pytest.approx(8.0)
        outbox = json.loads(timeline.session.outbox.path.read_text(encoding="utf-8"))
        assert outbox["pending"] == []
        assert outbox["flight"] is None
