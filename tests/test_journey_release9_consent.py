"""The Release 9 bootstrap in the release0-to-release1 leg must reach the consent probe.

Release 9's plain install stops for setup consent with no terminal unless --yes is
passed. bootstrap_release9 must hand its Agent checkout to the bootstrap helper, so
the pinned Agent's CLI is read and --yes is added.
"""
from pathlib import Path

from scripts import ci_journey as journey
from scripts import journey_version_jumps as jumps


def test_bootstrap_release9_passes_agent_checkout_to_consent_probe(tmp_path, monkeypatch):
    calls = {}

    def fake_bootstrap(app, manifest_path, shell, mode, evidence, root, data, agent=None):
        calls.update(mode=mode, agent=agent)

    monkeypatch.setattr(jumps.journey, "bootstrap", fake_bootstrap)
    agent = tmp_path / "agent"
    jumps.bootstrap_release9(tmp_path, tmp_path / "manifest.json", "sh", tmp_path / "ev",
                             tmp_path / "root", tmp_path / "data", agent=agent)
    assert calls == {"mode": "install", "agent": agent}


def test_fresh_install_with_consent_gate_adds_yes(tmp_path):
    manifest = tmp_path / "manifest.json"
    flags = journey.bootstrap_flags(manifest, "install", tmp_path / "root", tmp_path / "data",
                                    consent_gate=True)
    assert "--yes" in flags


def test_no_agent_checkout_means_no_consent_gate(tmp_path):
    assert journey.installer_has_consent_gate(None, tmp_path / "manifest.json") is False
