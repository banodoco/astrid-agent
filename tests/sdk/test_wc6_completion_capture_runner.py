"""Offline controls for the owner-installed WC6 capture attachment."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers import run_wc6_completion_capture as runner


def test_wc6_runner_guards_argv_helper_sink_budgets_and_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = Path(runner.__file__).with_name(runner.HELPER_NAME)
    assert runner.verify_helper(helper, helper.parent) == helper

    altered_dir = tmp_path / "altered"
    altered_dir.mkdir()
    altered = altered_dir / runner.HELPER_NAME
    altered.write_bytes(helper.read_bytes() + b"\n")
    with pytest.raises(runner.AttachmentRefused, match="hash mismatch"):
        runner.verify_helper(altered, altered_dir)
    with pytest.raises(runner.AttachmentRefused, match="origin mismatch"):
        runner.verify_helper(helper, tmp_path)

    monkeypatch.setattr(runner, "_git", lambda *args: "wrong-source")
    with pytest.raises(runner.AttachmentRefused, match="Agent source guard mismatch"):
        runner.verify_pinned_sources(tmp_path, tmp_path)

    sink = runner.verify_sink(tmp_path / "packet.jsonl", tmp_path)
    outside = tmp_path.parent / "packet.jsonl"
    with pytest.raises(runner.AttachmentRefused, match="outside TMPDIR"):
        runner.verify_sink(outside, tmp_path)

    command = runner.pytest_command(sink)
    assert command[0] == runner.sys.executable
    assert command[1:4] == ["-P", "-m", "pytest"]
    assert "--timeout=45" in command and "-m" in command and "integration" in command
    assert command[-1] == runner.ORIGINAL
    assert runner.MAX_PACKET_BYTES == 4 * 1024 * 1024
    with pytest.raises(runner.AttachmentRefused, match="exact WC6 selector"):
        runner.main(["other-test.py::test_not_wc6"])

    missing_output = io.BytesIO()
    assert runner.retain_packet(tmp_path / "missing.jsonl", 1, missing_output) == (1, "missing")
    assert b"WC6_COMPLETION_PACKET_MISSING" in missing_output.getvalue()
    missing_success = io.BytesIO()
    assert runner.retain_packet(tmp_path / "missing-success.jsonl", 0, missing_success) == (
        runner.MISSING_PACKET_EXIT,
        "missing",
    )

    partial_sink = tmp_path / "partial.jsonl"
    partial_sink.write_bytes(b'{"phase":"capture.start"}')
    partial_output = io.BytesIO()
    assert runner.retain_packet(partial_sink, 9, partial_output) == (9, "partial")
    assert b'{"phase":"capture.start"}' in partial_output.getvalue()

    oversize_sink = tmp_path / "oversize.jsonl"
    oversize_sink.write_bytes(b"x" * (runner.MAX_PACKET_BYTES + 1))
    oversize_output = io.BytesIO()
    assert runner.retain_packet(oversize_sink, 0, oversize_output) == (
        runner.MISSING_PACKET_EXIT,
        "oversize",
    )
    assert b"WC6_COMPLETION_PACKET_OVERSIZE" in oversize_output.getvalue()

    packet = b'{"phase":"capture.end","selected":true}\n'
    sink.write_bytes(packet)
    retained = io.BytesIO()
    assert runner.retain_packet(sink, 7, retained) == (7, "complete")
    log = retained.getvalue()
    start = log.index(b"\n") + 1
    end = log.index(b"\nWC6_COMPLETION_PACKET_END", start)
    assert log[start : end + 1] == packet
    assert hashlib.sha256(packet).hexdigest().encode() in log


def test_wc6_candidate_invocation_preserves_child_exit_and_packet_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("ASTRID_RUNTIME_CHECKOUT", str(tmp_path))
    monkeypatch.setattr(runner, "verify_pinned_sources", lambda *_: None)
    monkeypatch.setattr(runner, "verify_helper", lambda helper, runner_dir: helper)
    monkeypatch.setattr(runner, "verify_sink", lambda sink, tmpdir: sink)

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed["command"] = command
        observed["cwd"] = kwargs["cwd"]
        return SimpleNamespace(returncode=13)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    retained: dict[str, object] = {}

    def fake_retain(sink: Path, child_exit: int, output: io.BufferedWriter) -> tuple[int, str]:
        retained["sink"] = sink
        retained["child_exit"] = child_exit
        output.write(b"WC6_COMPLETION_PACKET_BEGIN state=partial\n")
        return child_exit, "partial"

    monkeypatch.setattr(runner, "retain_packet", fake_retain)

    assert runner.main([runner.ORIGINAL]) == 13
    command = observed["command"]
    assert isinstance(command, list)
    assert command[-1] == runner.ORIGINAL
    assert command[-4:-2] == ["-p", "wc6_completion_capture"]
    assert observed["cwd"] == tmp_path
    assert retained["child_exit"] == 13
    assert retained["sink"] == tmp_path / f"wc6-completion-{runner.os.getpid()}.jsonl"
