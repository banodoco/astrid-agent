"""Resident declarations stay bound to source data and detached from jobs."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest


def test_editable_catalogue_content_invalidates_compiler_generation(tmp_path, monkeypatch):
    import vibecomfy
    from vibecomfy import resources

    from astrid.packs.vibecomfy.resident_source import compiler_content_identity

    package = tmp_path / "vibecomfy"
    package.mkdir()
    module = package / "__init__.py"
    module.write_text("VERSION = 'one'\n")
    catalogue = tmp_path / "ready_templates" / "coverage.json"
    catalogue.parent.mkdir()
    catalogue.write_text('{"workflows": [{"id": "one"}]}')
    monkeypatch.setattr(vibecomfy, "__file__", str(module))
    monkeypatch.setattr(resources, "_resource_location", lambda name: catalogue)
    before = compiler_content_identity()
    identity = catalogue.stat()
    catalogue.write_text('{"workflows": [{"id": "two"}]}')
    os.utime(catalogue, ns=(identity.st_atime_ns, identity.st_mtime_ns))
    assert catalogue.stat().st_size == identity.st_size
    assert compiler_content_identity() != before


def test_cached_source_declarations_cannot_retain_another_jobs_observations(tmp_path, monkeypatch):
    from astrid.core.generation.engine_readiness import ComfyProvenance
    from astrid.packs.vibecomfy import resident

    source = tmp_path / "workflow.json"
    source.write_text('{"nodes": []}')
    calls = []

    def observe(self, path):
        calls.append(Path(path).read_bytes())
        self.recorded["pack"]["declared"] = "one"

    monkeypatch.setattr(ComfyProvenance, "source", observe)
    session = resident.VibeComfySession.__new__(resident.VibeComfySession)
    session.lock, session.source_declarations = threading.RLock(), {}
    first = session.source_provenance(source)
    first.recorded["pack"]["declared"] = "mutated"
    first.observed["engine"] = "job-one-target"
    first.models.append({"observed_sha256": "job-one-model"})
    first.add_readiness_issues([{"code": "job-one-warning"}])
    second = session.source_provenance(source)
    assert second.recorded["pack"] == {"declared": "one"}
    assert second.observed["engine"] is None and not second.models
    assert not second._readiness_issues
    assert len(calls) == 1
    source.write_text('{"nodes": [1]}')
    session.source_provenance(source)
    assert len(calls) == 2


def test_source_declaration_capacity_refuses_without_reusing_other_source(tmp_path, monkeypatch):
    from astrid.core.generation.engine_readiness import ComfyProvenance
    from astrid.packs.vibecomfy import resident

    monkeypatch.setattr(ComfyProvenance, "source", lambda *_: None)
    session = resident.VibeComfySession.__new__(resident.VibeComfySession)
    session.lock, session.source_declarations = threading.RLock(), {}
    source = tmp_path / "workflow.json"
    for index in range(resident._LIMIT):
        source.write_text('{"nodes": [%d]}' % index)
        session.source_provenance(source)
    source.write_text('{"nodes": [99]}')
    with pytest.raises(ValueError, match="capacity"):
        session.source_provenance(source)


@pytest.mark.parametrize("layout", ["loose", "packed", "detached", "worktree"])
def test_advisory_ambient_revision_matches_git_without_starting_a_process(tmp_path, monkeypatch, layout):
    from astrid.packs.vibecomfy.resident_source import advisory_repository_revision

    root = tmp_path / "repository"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()

    git("init")
    git("-c", "user.name=LS15 fixture", "-c", "user.email=ls15@example.invalid", "commit", "--allow-empty", "-m", "fixture")
    expected = git("rev-parse", "HEAD")
    if layout == "packed":
        git("pack-refs", "--all")
    elif layout == "detached":
        git("checkout", "--detach", expected)
    elif layout == "worktree":
        git("worktree", "add", "-b", "ls15-linked", str(tmp_path / "worktree"), expected)
        root = tmp_path / "worktree"
    private = root / "private" / "worker"
    private.mkdir(parents=True)
    monkeypatch.chdir(private)

    def no_process(*args, **kwargs):
        raise AssertionError("resident advisory metadata must not start a native command")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    assert advisory_repository_revision() == expected


def test_advisory_git_discovery_overrides_and_malformed_refs_stay_unknown(tmp_path, monkeypatch):
    from astrid.packs.vibecomfy.resident_source import advisory_repository_revision

    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("1" * 40)
    monkeypatch.chdir(tmp_path)
    assert advisory_repository_revision() == "1" * 40
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "another-repository"))
    assert advisory_repository_revision() is None
    monkeypatch.delenv("GIT_DIR")
    (git / "HEAD").write_text("ref: refs/../../outside")
    assert advisory_repository_revision() is None
