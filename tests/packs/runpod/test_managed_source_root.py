"""An installed Agent derives its RunPod bootstrap source root; nothing sets it."""
from __future__ import annotations

import subprocess
import json
from pathlib import Path

import pytest

from astrid.packs.runpod import managed_sources, remote_placement


def test_pin_refusal_redacts_unvalidated_repository_values():
    canonical = {"repository": "https://github.com/banodoco/runpod-lifecycle.git",
                 "revision": "a" * 40, "version": "1.2.3"}
    for repo in ("//user:FAKE_SECRET@example.invalid/l.git",
                 "https://example.invalid/l.git?token=FAKE_SECRET",
                 "https://user:FAKE_SECRET@example.invalid/l.git"):
        text = managed_sources.pin_mismatch_message(
            "lifecycle", {"repository": repo, "revision": "b" * 40, "version": "1.2.4"}, canonical)
        assert "FAKE_SECRET" not in text
        assert "repository <invalid>" in text
    normal = managed_sources.pin_mismatch_message(
        "lifecycle", {"repository": canonical["repository"], "revision": "b" * 40,
                      "version": "1.2.4"}, canonical)
    assert "revision " + "b" * 40 in normal
    assert "version 1.2.4" in normal


@pytest.mark.parametrize("repo", (
    "https://user%3AFAKE_SECRET%40example.invalid/l.git", "https://[::1]/l.git",
    "https://127.0.0.1/l.git", "https://git\nhub.com/l.git", "https://github.com/l.git//",
    " https://github.com/l.git", "https://github.com/l.git ", "HTTPS://github.com/l.git",
    "https://github.com/l.git?", "https://github.com/l.git#", "https://github.com:/l.git",
    "https://github.com/a\\b", pytest.param("https://github.com/" + "a" * 10000, id="oversized-10000-repository"),
))
def test_managed_resolution_refusal_handles_malformed_repository(repo):
    canonical = {"repository": "https://github.com/banodoco/runpod-lifecycle.git",
                 "revision": "a" * 40, "version": "1.2.3"}
    text = managed_sources.pin_mismatch_message(
        "lifecycle", {"repository": repo, "revision": "b" * 40, "version": "1.2.4"}, canonical)
    assert "repository <invalid>" in text
    assert "FAKE_SECRET" not in text


def test_legacy_manifest_is_refused_during_runpod_preparation(tmp_path, monkeypatch):
    version_root = tmp_path / "version"
    version_root.mkdir()
    monkeypatch.setattr(managed_sources.sys, "prefix", str(version_root / "py"))
    from astrid.core.rendering._local_schema import runtime_lock_path

    lock = json.loads(runtime_lock_path(managed_sources.AGENT_ROOT).read_text())
    (version_root / "manifest.json").write_text(json.dumps({
        "agent": {"sha": "a" * 40}, "runtime": {"sha": lock["source_commit"]},
    }))
    with pytest.raises(managed_sources.ManagedSourcesError, match="has no lifecycle pin"):
        managed_sources._pins()


def _repo(path: Path) -> tuple[str, str]:
    # Local repositories stand in for the GitHub remotes: the network is
    # unavailable in tests, but the real git init/fetch-by-SHA/checkout runs.
    path.mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (path / "f.txt").write_text(path.name)
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return str(path), git("rev-parse", "HEAD")


def test_installed_agent_uses_managed_root_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRID_RUNPOD_SOURCE_ROOT", raising=False)
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    pins = {name: _repo(tmp_path / "remotes" / name.split("/")[1]) for name in remote_placement.SOURCE_DIRS}
    monkeypatch.setattr(managed_sources, "_pins", lambda: pins)

    root = managed_sources.ensure_managed_sources()
    assert root == tmp_path / "data" / "runpod" / "sources"
    for directory, (_, sha) in pins.items():
        head = subprocess.run(["git", "-C", str(root / directory), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        assert head == sha
    # The source digest (the validate_sources input) accepts the managed root.
    assert remote_placement.source_digest(root)
    # A second call reuses the clean checkouts.
    assert managed_sources.ensure_managed_sources() == root
