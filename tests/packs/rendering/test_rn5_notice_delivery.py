"""RN-5: bounded wire notice, advisory fallback, and full verified warnings."""

from __future__ import annotations

import hashlib
import json
import threading

import pytest

from astrid.core.rendering.revision_notice import (
    NOTICE_ACK_NAME,
    pending_notice_phase,
    revision_notice_phase,
    revision_notice_text,
    verified_revision_warnings,
)
from astrid.packs.rendering.rendering.renderers.remotion import progress as relay_module


def _mismatch(editor=None, render=None):
    return {
        "pack_id": "astrid_motion",
        "comparison": "mismatch",
        "editor_revision": editor or "a" * 40,
        "upstream_revision": render or "b" * 40,
        "resolved_revision": "c" * 40,
    }


def _provenance(root, rows):
    path = root / "out.provenance.json"
    raw = json.dumps({"backend_fragments": {
        "rendering.remotion": {"pack_revisions": rows},
    }}).encode()
    path.write_bytes(raw)
    return {
        "name": "provenance", "role": "auxiliary", "path": str(path),
        "size": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def test_phase_names_both_full_git_revisions_within_runtime_limit():
    row = _mismatch()
    phase = revision_notice_phase(row)
    assert "astrid_motion" in phase and "mismatch" in phase
    assert row["editor_revision"] in phase
    assert row["upstream_revision"] in phase
    assert 0 < len(phase) <= 128
    assert pending_notice_phase({"phase": phase, "percent": 10}) == phase
    assert pending_notice_phase({"phase": phase, "percent": 50}) is None
    assert pending_notice_phase({"phase": phase + "x" * 128, "percent": 10}) is None


def test_long_revisions_are_explicitly_shortened_only_in_wire_phase():
    row = _mismatch("sha256:" + "a" * 64, "sha256:" + "b" * 64)
    phase = revision_notice_phase(row)
    assert len(phase) <= 128
    assert "editor sha256:" in phase and "render sha256:" in phase
    assert phase.count("...") == 2
    full = revision_notice_text(row)
    assert row["editor_revision"] in full and row["upstream_revision"] in full


def test_verified_warning_projection_excludes_match_unknown_and_tampered_bytes(tmp_path):
    row = _mismatch()
    output = _provenance(tmp_path, [row, {**row, "comparison": "match"},
                                    {**row, "comparison": "unknown"}, row])
    assert verified_revision_warnings([output], tmp_path) == [revision_notice_text(row)]
    assert verified_revision_warnings([output], tmp_path / "other") == []
    assert verified_revision_warnings([{**output, "role": "result"}], tmp_path) == []
    assert verified_revision_warnings([{**output, "digest": "sha256:" + "0" * 64}], tmp_path) == []
    from pathlib import Path
    Path(output["path"]).write_text("{}")
    assert verified_revision_warnings([output], tmp_path) == []


def test_relay_requires_exact_ack_before_enter_returns(tmp_path, monkeypatch):
    phase = revision_notice_phase(_mismatch())
    progress = tmp_path / ".astrid-progress.json"
    ack = tmp_path / NOTICE_ACK_NAME
    ack.write_text(json.dumps({"phase": "some other notice"}))
    log = tmp_path / "frames.log"
    log.write_text("")
    entered = threading.Event()
    monkeypatch.setattr(relay_module, "_NOTICE_ACK_TIMEOUT_SECONDS", 3.0)
    # Observe that the relay cannot enter the drawing scope on a stale ACK.
    def draw():
        with relay_module.RemotionProgressRelay(log, progress_path=progress, notices=[phase]):
            entered.set()

    thread = threading.Thread(target=draw)
    thread.start()
    try:
        import time
        deadline = time.monotonic() + 2
        while not progress.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert json.loads(progress.read_text()) == {"phase": phase, "percent": 10}
        assert not entered.wait(0.1)
        temporary = ack.with_suffix(".tmp")
        temporary.write_text(json.dumps({"phase": phase}))
        temporary.replace(ack)
        assert entered.wait(1)
    finally:
        thread.join(timeout=4)
    assert not thread.is_alive()


@pytest.mark.parametrize("managed", [False, True])
def test_notice_does_not_refuse_when_no_runtime_ack(tmp_path, monkeypatch, capsys, managed):
    phase = revision_notice_phase(_mismatch())
    progress = tmp_path / ".astrid-progress.json"
    if managed:
        (tmp_path / NOTICE_ACK_NAME).write_text('{"phase": null}')
    log = tmp_path / "frames.log"
    log.write_text("")
    monkeypatch.setattr(relay_module, "_NOTICE_ACK_TIMEOUT_SECONDS", 0.0)
    with relay_module.RemotionProgressRelay(log, progress_path=progress, notices=[phase]):
        assert json.loads(progress.read_text()) == {"phase": phase, "percent": 10}
        log.write_text("Rendered 1/2\n")
    checkpoint = json.loads(progress.read_text())
    assert checkpoint["phase"] == phase
    assert checkpoint["current"] == 1 and checkpoint["total"] == 2
    assert len(checkpoint["phase"]) <= 128
    assert ("acknowledgement unavailable" in capsys.readouterr().err) is managed


def test_no_notice_keeps_existing_frame_progress(tmp_path):
    progress = tmp_path / ".astrid-progress.json"
    log = tmp_path / "frames.log"
    log.write_text("Rendered 1/2\n")
    with relay_module.RemotionProgressRelay(log, progress_path=progress):
        pass
    assert json.loads(progress.read_text()) == relay_module.parse_progress_line("Rendered 1/2")
