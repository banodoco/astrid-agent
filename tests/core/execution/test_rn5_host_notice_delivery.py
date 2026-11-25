"""Exercise the actual host heartbeat and frozen settlement across the render seam."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from astrid.core.execution import generic_host
from astrid.core.rendering.revision_notice import (
    NOTICE_ACK_NAME, revision_notice_phase, revision_notice_text,
)
from astrid.packs.rendering.rendering.renderers.remotion.progress import RemotionProgressRelay
from tests.packs.rendering.test_rn5_notice_delivery import _mismatch, _provenance
from tests.test_generic_host_result_json import host_case


def test_direct_render_notice_event_is_accepted_before_drawing_and_settlement_keeps_full_warning(
    host_case, monkeypatch,
):
    case = host_case
    task = case.task["task"]
    task.update(capability="rendering.render", status="running", runtime_epoch=1)
    monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 0.02)
    row = _mismatch()
    phase = revision_notice_phase(row)
    full = revision_notice_text(row)
    events = []

    def heartbeat(task_id, lease_token, *, attempt_id, fence, progress=None):
        # Model the pinned Runtime's heartbeat phase contract: the oversized
        # old notice cannot become a task.progress event.
        if progress is not None:
            assert set(progress) <= {"phase", "percent", "current", "total"}
            assert 0 < len(progress.get("phase", "render")) <= 128
            events.append({"event_type": "task.progress", "payload": dict(progress)})
        return SimpleNamespace(
            task_id=task_id, attempt_id=attempt_id, fence=fence, lease_id=lease_token,
            runtime_epoch=1,
            lease_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        )

    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)

    def execute(_task, *, lease_token, _terminal, **_kwargs):
        # Only replace the expensive execution boundary. run_task still owns
        # the real lease pump, ACK and immutable settlement call.
        root = _terminal["root"]
        log = root / "frames.log"
        log.write_text("")
        assert json.loads((root / NOTICE_ACK_NAME).read_text()) == {"phase": None}
        with RemotionProgressRelay(
            log, progress_path=root / ".astrid-progress.json", notices=[phase],
        ):
            assert any(event["payload"] == {"phase": phase, "percent": 10}
                       for event in events)
            events.append({"event_type": "draw"})
            log.write_text("Rendered 1/2\n")
        output_root = root / "outputs"
        output_root.mkdir()
        provenance = _provenance(output_root, [row])
        _terminal["output_root"] = output_root
        _terminal["outputs"] = [provenance]
        payload = {"warnings": ["existing producer warning"]}
        _terminal["payload"] = payload
        settlement = _terminal["completion_call"](
            case.runtime.settle, task["id"], lease_token,
            result=payload, outputs=[], effect=None,
            attempt_id=task["attempt_id"], fence=task["fence"],
        )
        _terminal.update(status="succeeded", settled=True, settlement=settlement)
        # The provenance port has already passed the producer's harvest and
        # upload in the real command. This test isolates host delivery, not
        # the renderer/browser or artifact upload.
        return settlement

    monkeypatch.setattr(case.host, "_run_task_impl", execute)
    result = case.host.run_task(case.task, lease_token="lease-result", keep_attempt=True)

    assert result["task"]["status"] == "completed"
    notice_at = next(i for i, event in enumerate(events)
                     if event.get("payload", {}).get("phase") == phase)
    draw_at = next(i for i, event in enumerate(events) if event["event_type"] == "draw")
    assert notice_at < draw_at
    assert case.runtime.settlements[0][2]["result"]["warnings"] == ["existing producer warning", full]
    assert case.runtime.failures == []
