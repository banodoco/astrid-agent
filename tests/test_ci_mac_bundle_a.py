import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import ci_mac_bundle_a as bundle


def valid_config(name):
    return ("core.repositoryformatversion\n0\0core.bare\ntrue\0"
            f"remote.origin.url\n{bundle.REPOS[name]}\0"
            "remote.origin.fetch\n+refs/heads/*:refs/remotes/origin/*\0")


def test_cache_directory_refuses_symlink_and_public_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "UID", os.getuid())
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError): bundle.owned_directory(link)
    target.chmod(0o755)
    with pytest.raises(RuntimeError): bundle.owned_directory(target)
    assert target.stat().st_mode & 0o777 == 0o755


def test_git_cache_refuses_unknown_existing_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "UID", os.getuid())
    repo = tmp_path / "agent.git"
    repo.mkdir(mode=0o700)
    commands = []
    def fake_git(args, env):
        commands.append(args)
        return "https://example.invalid/foreign.git"
    monkeypatch.setattr(bundle, "git", fake_git)
    with pytest.raises(RuntimeError, match="unexpected local configuration"):
        bundle.cached_repo(tmp_path, "agent", "refs/heads/test", {})
    assert len(commands) == 1


def test_fetch_sanitizes_git_and_pins_reachable_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "UID", os.getuid())
    commands = []
    def fake_git(args, env):
        commands.append(args)
        if args[-1] == "--list": return valid_config("agent")
        if args[-1] == "--is-bare-repository": return "true"
        if args[-1] == "FETCH_HEAD^{commit}": return "a" * 40
        return ""
    monkeypatch.setattr(bundle, "git", fake_git)
    assert bundle.cached_repo(tmp_path, "agent", "refs/heads/test", {}) == "a" * 40
    assert any("--depth=1" in cmd for cmd in commands)
    assert any("refs/heads/bundle-a-selected" in cmd for cmd in commands)


def test_manual_workflow_embeds_tested_script_without_checkout():
    import textwrap
    base = Path(__file__).parents[1]
    workflow = (base / ".github/workflows/journey.yml").read_text()
    embedded = workflow.split("          /opt/homebrew/opt/python@3.11/bin/python3.11 - <<'PY'\n", 1)[1].rsplit("\n          PY\n", 1)[0]
    assert textwrap.dedent(embedded).strip() == (base / "scripts/ci_mac_bundle_a.py").read_text().strip()
    assert "actions/checkout" not in workflow
    assert "workflow_dispatch:" in workflow


def test_cache_refuses_nested_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "UID", os.getuid())
    repo = tmp_path / "agent.git"
    repo.mkdir(mode=0o700)
    (repo / "config").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RuntimeError, match="symlink"):
        bundle.cached_repo(tmp_path, "agent", "refs/heads/test", {})


def test_cache_configuration_rejects_redirects_includes_and_duplicates():
    good = valid_config("agent")
    bundle.validate_cache_config("agent", good)
    for injected in ("include.path\n/tmp/evil\0", "http.followredirects\ntrue\0",
                     "url.https://evil.insteadof\nhttps://github.com/\0",
                     f"remote.origin.url\n{bundle.REPOS['agent']}\0"):
        with pytest.raises(RuntimeError, match="unexpected local configuration"):
            bundle.validate_cache_config("agent", good + injected)
    assert bundle.MIN_FREE == 10_000_000_000
