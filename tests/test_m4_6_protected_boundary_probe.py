"""Operational refusals. The real principal test belongs on the hosted VM."""

import pytest

from scripts import m4_6_protected_boundary_probe as probe


def test_non_linux_host_is_refused_before_output_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--candidate-uid", "65534", "--candidate-gid", "65534", "--output", str(output)])
    with pytest.raises(RuntimeError, match="disposable Linux host"):
        probe.main()
    assert not output.exists()


def test_root_candidate_is_refused_before_publication(monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setattr(probe.os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(ValueError, match="nonzero"):
        probe.run_probe(0, 0, [])


def test_executor_uid_zero_cannot_be_mistaken_for_host_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setattr(probe.os, "geteuid", lambda: 0, raising=False)
    # Exercise the actual capability parser against a restricted-executor status.
    from pathlib import Path
    real_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **kw: "CapEff:\t0000000000000000\n" if str(path) == "/proc/self/status" else real_read(path, *a, **kw))
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--candidate-uid", "65534", "--candidate-gid", "65534", "--output", str(output)])
    with pytest.raises(RuntimeError, match="executor UID 0 is insufficient"):
        probe.main()
    assert not output.exists()
