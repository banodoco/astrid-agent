import os
import subprocess
from pathlib import Path

import pytest
from scripts import ci_cached_checkout as cached


def command(*args):
    return subprocess.check_output(["git", *map(str, args)], text=True, stderr=subprocess.DEVNULL).strip()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cached, "UID", os.getuid())
    parent = tmp_path / "private-cache"
    parent.mkdir(mode=0o700)
    root = parent / "bundle-a-v1"
    root.mkdir(mode=0o700)
    (root / "git-home").mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    command("init", source)
    (source / "proof.txt").write_text("immutable\n")
    command("-C", source, "add", "proof.txt")
    command("-C", source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    sha = command("-C", source, "rev-parse", "HEAD")
    repo = root / "agent.git"
    command("init", "--bare", repo)
    repo.chmod(0o700)
    command("-C", repo, "remote", "add", "origin", cached.REPOS["agent"])
    command("-C", repo, "fetch", "--depth=1", source.as_uri(), sha)
    command("-C", repo, "update-ref", "refs/heads/bundle-a-selected", sha)
    command("-C", repo, "symbolic-ref", "HEAD", "refs/heads/bundle-a-selected")
    return root, workspace, repo, sha


def test_cache_hit_checks_out_exact_commit_without_alternates(cache):
    root, workspace, _, sha = cache
    assert cached.checkout("agent", sha, root, workspace) is True
    dest = workspace / "candidate-agent"
    assert command("-C", dest, "rev-parse", "HEAD") == sha
    assert (dest / "proof.txt").read_text() == "immutable\n"
    assert not (dest / ".git/objects/info/alternates").exists()
    assert (dest / ".git/shallow").is_file()
    assert command("-C", dest, "remote", "get-url", "origin") == cached.REPOS["agent"]


def test_cache_miss_falls_back_without_creating_destination(cache):
    root, workspace, _, _ = cache
    assert cached.checkout("agent", "f" * 40, root, workspace) is False
    assert not (workspace / "candidate-agent").exists()
    assert cached.checkout("runtime", "f" * 40, root, workspace) is False


def test_cache_rejects_local_redirect_and_symlink(cache):
    root, workspace, repo, sha = cache
    command("-C", repo, "config", "http.followRedirects", "true")
    with pytest.raises(RuntimeError, match="unsafe"):
        cached.checkout("agent", sha, root, workspace)
    command("-C", repo, "config", "--unset", "http.followRedirects")
    (repo / "evil").symlink_to(workspace)
    with pytest.raises(RuntimeError, match="symlink"):
        cached.checkout("agent", sha, root, workspace)
    assert not (workspace / "candidate-agent").exists()


def test_workflow_cache_is_smoke_only_and_hosted_keeps_checkout():
    workflow = (Path(__file__).parents[1] / ".github/workflows/journey.yml").read_text()
    job = workflow.split("  journey:\n", 1)[1].split("\n  release2:", 1)[0]
    for name in ("agent", "runtime", "pinokio"):
        assert f"id: cache_{name}\n        if: matrix.runner == 'astrid-mac' && matrix.job_id == 'direct-macos'" in job
        assert f"if: matrix.runner != 'astrid-mac' || steps.cache_{name}.outputs.hit != 'true'" in job


def test_existing_checkout_falls_back_without_mutating_it(cache):
    root, workspace, _, sha = cache
    dest = workspace / "candidate-agent"
    dest.mkdir()
    (dest / "preserve").write_text("existing")
    assert cached.checkout("agent", sha, root, workspace) is False
    assert (dest / "preserve").read_text() == "existing"


@pytest.mark.parametrize("relative", ["objects/info/alternates", "objects/info/http-alternates", "info/grafts"])
def test_cache_rejects_external_object_and_history_references(cache, relative):
    root, workspace, repo, sha = cache
    unsafe = repo / relative
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    unsafe.write_text("/untrusted/object/source\n")
    with pytest.raises(RuntimeError, match="external object or history"):
        cached.checkout("agent", sha, root, workspace)
    assert not (workspace / "candidate-agent").exists()


def test_cache_git_disables_hooks_and_replacement_objects(cache, monkeypatch):
    root, workspace, _, sha = cache
    original = cached.subprocess.run
    calls = []
    def capture(argv, **kwargs):
        calls.append((argv, kwargs.get("env", {})))
        return original(argv, **kwargs)
    monkeypatch.setattr(cached.subprocess, "run", capture)
    assert cached.checkout("agent", sha, root, workspace)
    assert calls
    assert all("core.hooksPath=/dev/null" in args for args, _ in calls)
    assert all(env.get("GIT_NO_REPLACE_OBJECTS") == "1" for _, env in calls)
