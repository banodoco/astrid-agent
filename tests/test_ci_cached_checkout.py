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


def test_main_uses_private_runner_temp_without_mutating_existing_workspace(tmp_path, monkeypatch):
    home = tmp_path / 'runner'
    temp = home / 'actions-runner/_work/_temp'
    temp.mkdir(parents=True)
    workspace = home / 'actions-runner/_work/astrid-agent/astrid-agent'
    old = workspace / 'candidate-agent'
    old.mkdir(parents=True)
    (old / 'preserve').write_text('existing data')
    output = tmp_path / 'output'
    monkeypatch.setattr(cached, 'UID', os.getuid())
    monkeypatch.setenv('RUNNER_NAME', 'hannah-mac')
    monkeypatch.setenv('RUNNER_TEMP', str(temp))
    monkeypatch.setenv('GITHUB_WORKSPACE', str(workspace))
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(cached.sys, 'argv', ['script', 'agent', 'a' * 40])
    def checkout(name, sha, root, target):
        assert target == temp / 'astrid-cached-checkouts'
        assert target.stat().st_mode & 0o777 == 0o700
        return True
    monkeypatch.setattr(cached, 'checkout', checkout)
    cached.main()
    assert (old / 'preserve').read_text() == 'existing data'
    assert f'path={temp}/astrid-cached-checkouts/candidate-agent' in output.read_text()


def test_workflow_uses_cache_path_only_when_cache_hit():
    workflow = (Path(__file__).parents[1] / '.github/workflows/journey.yml').read_text()
    job = workflow.split('  journey:\n', 1)[1].split('\n  release2:', 1)[0]
    for name in ('agent', 'runtime', 'pinokio'):
        assert f'steps.cache_{name}.outputs.path || format(' in job
    assert '--candidate-agent-checkout "$env:AGENT_CHECKOUT"' in job
    assert '--candidate-runtime-checkout "$env:RUNTIME_CHECKOUT"' in job
    assert '--pinokio-checkout "$env:PINOKIO_CHECKOUT"' in job


def test_shallow_cache_fetches_exact_commit_after_selected_ref_moves(cache):
    root, workspace, repo, previous_sha = cache
    source = root.parent.parent / 'source'
    (source / 'proof.txt').write_text('new revision\n')
    command('-C', source, 'add', 'proof.txt')
    command('-C', source, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'new')
    newer_sha = command('-C', source, 'rev-parse', 'HEAD')
    command('-C', repo, 'fetch', '--depth=1', source.as_uri(), newer_sha)
    command('-C', repo, 'update-ref', 'refs/heads/bundle-a-selected', newer_sha)
    assert command('-C', repo, 'cat-file', '-t', previous_sha) == 'commit'
    assert cached.checkout('agent', previous_sha, root, workspace)
    dest = workspace / 'candidate-agent'
    assert command('-C', dest, 'rev-parse', 'HEAD') == previous_sha
    assert (dest / 'proof.txt').read_text() == 'immutable\n'
    assert not (dest / '.git/objects/info/alternates').exists()


def test_git_error_reports_only_operation_and_exit_code(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(cached.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=128, stderr='secret/path/token'))
    with pytest.raises(RuntimeError, match=r'cache fetch failed \(exit 128\)') as exc:
        cached.run(['fetch', '/private/path'], {})
    assert 'secret' not in str(exc.value) and '/private' not in str(exc.value)
