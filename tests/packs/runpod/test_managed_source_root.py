"""An installed Agent derives its RunPod bootstrap source root; nothing sets it."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from astrid.packs.runpod import managed_sources, remote_placement


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

    seen = {}

    class Capture:
        def __init__(self, provider, **kwargs):
            seen.update(kwargs)

        def run(self, **_):
            return {}

    monkeypatch.setattr(remote_placement, "RemotePlacement", Capture)
    args = SimpleNamespace(out=tmp_path / "out", request=None, asset_map=None)
    remote_placement.run_remote_placement(args, provider=object(), env={})

    root = tmp_path / "data" / "runpod" / "sources"
    assert seen["source_root"] == root
    for directory, (_, sha) in pins.items():
        head = subprocess.run(["git", "-C", str(root / directory), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        assert head == sha
    # The source digest (the validate_sources input) accepts the managed root.
    assert remote_placement.source_digest(root)
    # A second call reuses the clean checkouts.
    assert managed_sources.ensure_managed_sources() == root
