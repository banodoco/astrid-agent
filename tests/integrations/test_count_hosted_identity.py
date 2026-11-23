"""Hosted identity must account for every nested source repository."""
from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest

from runtime_protocol import source_identity
from runtime_protocol.source_identity import SourceIdentityError, capture_source_identity
from tests.integrations._count_hosted_identity import NESTED_REPOSITORIES, hosted_identity_scope

pytestmark = pytest.mark.integration


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _repo(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    _git(root, "config", "user.name", "Count Fixture")
    _git(root, "config", "user.email", "count-fixture@example.invalid")
    (root / "tracked.txt").write_text(name + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "--quiet", "-m", "fixture"], check=True)
    return root.resolve()


def _layout(tmp_path: Path, *, nested: bool = True):
    agent = _repo(tmp_path / "astrid-agent", "Agent")
    runtime = _repo(tmp_path / "astrid-runtime", "Runtime")
    components = {}
    if nested:
        for name in NESTED_REPOSITORIES:
            components[name] = _repo(agent / name, name)
    return agent, runtime, components


def _capture(monkeypatch, agent, runtime, *, hosted=True):
    with hosted_identity_scope(monkeypatch, source_identity, agent, hosted=hosted) as heads:
        identity = capture_source_identity(runtime, agent)
    return identity, heads


def test_clean_hosted_layout_captures_agent_and_all_nested_heads(tmp_path, monkeypatch):
    agent, runtime, components = _layout(tmp_path)
    identity, heads = _capture(monkeypatch, agent, runtime)
    assert identity["agent_head"] == heads["astrid-agent"] == _git(agent, "rev-parse", "HEAD")
    assert set(heads) == {"astrid-agent", *NESTED_REPOSITORIES}
    for name, root in components.items():
        assert heads[name] == _git(root, "rev-parse", "HEAD")
    assert identity["dirty"] is False


@pytest.mark.parametrize("damage", ["agent_tracked", "component_tracked", "agent_untracked",
                                     "component_untracked", "non_git_nested"])
def test_hosted_identity_rejects_dirty_or_invalid_source_roots(tmp_path, monkeypatch, damage):
    agent, runtime, components = _layout(tmp_path)
    if damage == "agent_tracked":
        (agent / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    elif damage == "component_tracked":
        (components[NESTED_REPOSITORIES[0]] / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    elif damage == "agent_untracked":
        (agent / "stray.txt").write_text("stray\n", encoding="utf-8")
    elif damage == "component_untracked":
        (components[NESTED_REPOSITORIES[0]] / "stray.txt").write_text("stray\n", encoding="utf-8")
    else:
        broken = components[NESTED_REPOSITORIES[0]]
        shutil.rmtree(broken)
        broken.mkdir()
        (broken / "plain.txt").write_text("not a repository\n", encoding="utf-8")
    with pytest.raises(SourceIdentityError):
        _capture(monkeypatch, agent, runtime)


def test_hosted_identity_rejects_nested_head_change_during_capture(tmp_path, monkeypatch):
    agent, runtime, components = _layout(tmp_path)
    original = source_identity._untracked
    changed = False

    def move_nested_head(root):
        nonlocal changed
        result = original(root)
        if Path(root).resolve() == agent and not changed:
            nested = components[NESTED_REPOSITORIES[0]]
            (nested / "tracked.txt").write_text("next commit\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(nested), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(nested), "commit", "--quiet", "-m", "next"], check=True)
            changed = True
        return result

    monkeypatch.setattr(source_identity, "_untracked", move_nested_head)
    with pytest.raises(SourceIdentityError, match="changed during"):
        _capture(monkeypatch, agent, runtime)


def test_non_hosted_local_layout_may_omit_nested_repositories(tmp_path, monkeypatch):
    agent, runtime, _ = _layout(tmp_path, nested=False)
    identity, heads = _capture(monkeypatch, agent, runtime, hosted=False)
    assert identity["dirty"] is False
    assert set(heads) == {"astrid-agent"}
