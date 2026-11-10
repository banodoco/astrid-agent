"""The Release 9 bootstrap in the release9-then-install leg must reach the consent probe.

Release 9's plain install stops for setup consent with no terminal unless --yes is
passed. bootstrap_release9 must hand its Agent checkout to the bootstrap helper, so
the pinned Agent's CLI is read and --yes is added.
"""
import os
from pathlib import Path
from types import SimpleNamespace

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


def test_ci_leg_flags_reach_the_release9_consent_probe(tmp_path, monkeypatch):
    """The CI release9-then-install step passes --candidate-agent-checkout and no --agent-checkout."""
    calls = {}

    def fake_bootstrap(app, manifest_path, shell, mode, evidence, root, data, agent=None):
        calls["agent"] = agent

    monkeypatch.setattr(jumps.journey, "bootstrap", fake_bootstrap)
    candidate_agent = tmp_path / "candidate-agent"
    args = SimpleNamespace(candidate_agent_checkout=candidate_agent, agent_checkout=None)
    jumps.bootstrap_release9(tmp_path, tmp_path / "manifest.json", "sh", tmp_path / "ev",
                             tmp_path / "root", tmp_path / "data",
                             agent=jumps.release9_consent_agent(args))
    assert calls["agent"] == candidate_agent


def test_release9_consent_agent_prefers_candidate_checkout(tmp_path):
    both = SimpleNamespace(candidate_agent_checkout=tmp_path / "c", agent_checkout=tmp_path / "a")
    assert jumps.release9_consent_agent(both) == tmp_path / "c"
    only_agent = SimpleNamespace(candidate_agent_checkout=None, agent_checkout=tmp_path / "a")
    assert jumps.release9_consent_agent(only_agent) == tmp_path / "a"


def test_fresh_install_with_consent_gate_adds_yes(tmp_path):
    manifest = tmp_path / "manifest.json"
    flags = journey.bootstrap_flags(manifest, "install", tmp_path / "root", tmp_path / "data",
                                    consent_gate=True)
    assert "--yes" in flags


def test_no_agent_checkout_means_no_consent_gate(tmp_path):
    assert journey.installer_has_consent_gate(None, tmp_path / "manifest.json") is False


def test_linux_run_home_keeps_playwright_browsers_on_the_runner_cache(tmp_path, monkeypatch):
    from scripts import ci_journey

    runner_home = tmp_path / "runner"
    monkeypatch.setenv("HOME", str(runner_home))
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(ci_journey.sys, "platform", "linux")
    monkeypatch.setattr(ci_journey, "hosted_windows_runner", lambda: False)
    run_root = tmp_path / "run"
    with ci_journey.isolated_environment(run_root, tmp_path / "root", tmp_path / "data"):
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(runner_home / ".cache" / "ms-playwright")
        assert os.environ["HOME"] == str(run_root / "home")
