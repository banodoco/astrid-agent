"""Focused controls for the bounded authentic release-0 -> release-1 leg."""
import argparse
from pathlib import Path

import pytest

from scripts import journey_version_jumps as jumps


def test_release0_fixture_is_the_approved_hashed_input_set():
    root, manifest, receipt = jumps.verified_release0_fixture(jumps.RELEASE0_FIXTURE)
    assert root == jumps.RELEASE0_FIXTURE.resolve()
    assert manifest == root / "served" / "install-manifest.json"
    assert receipt["fixture_sums_sha256"] == jumps.RELEASE0_SUMS_SHA256
    assert receipt["sources"] == jumps.RELEASE0_REFS
    assert set(receipt["bootstrap_sha256"]) == {"install", "install.ps1"}


def test_release0_bootstrap_uses_the_frozen_served_script_without_a_url_fallback(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jumps.sys, "platform", "linux")
    monkeypatch.setattr(jumps.journey, "run", lambda argv, log, **kwargs: calls.append((argv, log, kwargs)))
    root, manifest, _receipt = jumps.verified_release0_fixture(jumps.RELEASE0_FIXTURE)
    program, data = tmp_path / "program", tmp_path / "data"
    jumps.bootstrap_release0(root, manifest, "sh", tmp_path / "evidence", program, data)
    argv, log, _kwargs = calls[0]
    assert argv == ["sh", root / "served" / "install", "--manifest", manifest,
                    "--root", program, "--data-root", data]
    assert log.name == "release0-bootstrap.log"


@pytest.mark.parametrize("changes", (
    {"mode": "pinokio-backend"},
    {"allow_release0_to_release1": False},
    {"source_override": True},
    {"release0_fixture": None},
))
def test_release0_admission_is_narrow_and_rejects_general_bypasses(changes):
    values = {"mode": "direct", "job_id": "direct-linux", "candidate": Path("candidate.json"),
              "allow_release0_to_release1": True, "release0_fixture": jumps.RELEASE0_FIXTURE,
              "source_override": False}
    values.update(changes)
    with pytest.raises(ValueError, match="release0-to-release1"):
        jumps.release0_admission(argparse.Namespace(**values))


def test_special_leg_lifts_only_its_windows_historical_refusal(monkeypatch, tmp_path):
    args = argparse.Namespace(leg="release0-to-release1")
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    monkeypatch.setattr(jumps, "execute_release0_to_release1", lambda *unused: {"status": "special-windows-admitted"})
    assert jumps.execute_owned(args, tmp_path / "program", tmp_path / "data", tmp_path / "evidence",
                               argparse.Namespace(pid=1), "birth") == {"status": "special-windows-admitted"}
