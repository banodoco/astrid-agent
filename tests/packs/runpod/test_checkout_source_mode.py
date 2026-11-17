"""RunPod source mode 'checkout': the pins a worker fetches are derived from a clean, pushed source checkout."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from astrid.packs.runpod import managed_sources, remote_placement

REMOTE_URL = "https://github.com/example/astrid-agent.git"
RUNTIME_SHA = "c" * 40


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A fake checkout whose https origin is rewritten (repo-local insteadOf) to a local bare repository."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "runtime.lock.json").write_text(json.dumps({"source_commit": RUNTIME_SHA}))
    (root / "tracked.txt").write_text("one")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "first")
    _git(root, "remote", "add", "origin", REMOTE_URL)
    _git(root, "config", f"url.{bare}.insteadOf", REMOTE_URL)
    _git(root, "push", "-q", "origin", "main")
    monkeypatch.setattr(managed_sources, "AGENT_ROOT", root)
    monkeypatch.setenv(managed_sources.SOURCE_MODE_ENV, "checkout")
    return root


def _commit(root: Path, text: str = "two") -> str:
    (root / "tracked.txt").write_text(text)
    _git(root, "commit", "-qam", text)
    return _git(root, "rev-parse", "HEAD")


def test_clean_pushed_checkout_derives_the_installed_schema(checkout):
    head = _git(checkout, "rev-parse", "HEAD")
    manifest, provenance = managed_sources.derive_checkout_manifest(checkout)
    from astrid.core.generation.canonical_pins import canonical_pin

    lifecycle, vibecomfy = canonical_pin("lifecycle"), canonical_pin("vibecomfy")
    assert manifest == {
        "agent": {"repo": REMOTE_URL, "sha": head},
        "runtime": {"repo": "https://github.com/banodoco/astrid-runtime", "sha": RUNTIME_SHA},
        "lifecycle": {"repo": lifecycle["repository"], "sha": lifecycle["revision"], "version": lifecycle["version"]},
        "vibecomfy": {"repo": vibecomfy["repository"], "sha": vibecomfy["revision"]},
    }
    assert set(provenance) == {"agent", "runtime", "lifecycle", "vibecomfy"}
    assert "astrid/packs/runpod/pins.json" in provenance["lifecycle"]


def test_checkout_pins_pass_the_same_validation(checkout):
    pins = managed_sources._pins()
    assert set(pins) == set(remote_placement.SOURCE_DIRS)
    assert pins["src/astrid-agent"] == (REMOTE_URL, _git(checkout, "rev-parse", "HEAD"))
    assert pins["src/astrid-runtime"][1] == RUNTIME_SHA


def test_head_behind_a_pushed_branch_is_on_the_remote(checkout):
    _commit(checkout)
    _git(checkout, "push", "-q", "origin", "main")
    _git(checkout, "checkout", "-q", "--detach", "HEAD~1")
    assert managed_sources.derive_checkout_manifest(checkout)[0]["agent"]["sha"] == _git(checkout, "rev-parse", "HEAD")


def test_dirty_tracked_file_is_refused_naming_the_fix(checkout):
    (checkout / "tracked.txt").write_text("edited")
    with pytest.raises(managed_sources.ManagedSourcesError, match=r"uncommitted changes \(tracked.txt\).*Commit and push"):
        managed_sources.derive_checkout_manifest(checkout)


def test_untracked_files_do_not_block(checkout):
    (checkout / "scratch.txt").write_text("x")
    managed_sources.derive_checkout_manifest(checkout)


def test_unpushed_head_is_refused_naming_the_fix(checkout):
    _commit(checkout)
    with pytest.raises(managed_sources.ManagedSourcesError, match=r"not on remote 'origin'.*git push origin HEAD"):
        managed_sources.derive_checkout_manifest(checkout)


def test_unreachable_remote_is_refused(checkout):
    _git(checkout, "config", "--unset", f"url.{checkout.parent / 'remote.git'}.insteadOf")
    _git(checkout, "remote", "set-url", "origin", str(checkout.parent / "missing.git"))
    with pytest.raises(managed_sources.ManagedSourcesError, match="cannot reach remote"):
        managed_sources.derive_checkout_manifest(checkout)


def test_missing_remote_is_refused(checkout):
    _git(checkout, "remote", "remove", "origin")
    with pytest.raises(managed_sources.ManagedSourcesError, match="no remote named 'origin'"):
        managed_sources.derive_checkout_manifest(checkout)


@pytest.mark.parametrize("content", [None, "{", json.dumps({}), json.dumps({"source_commit": "nope"})])
def test_missing_runtime_pin_is_refused(checkout, content):
    lock = checkout / "runtime.lock.json"
    if content is None:
        _git(checkout, "rm", "-q", "runtime.lock.json")
    else:
        lock.write_text(content)
    _git(checkout, "commit", "-qam", "pin")
    _git(checkout, "push", "-q", "origin", "main")
    with pytest.raises(managed_sources.ManagedSourcesError, match="Runtime pin|source_commit"):
        managed_sources.derive_checkout_manifest(checkout)


def test_missing_canonical_pin_is_refused(checkout, monkeypatch):
    from astrid.core.generation import canonical_pins

    def broken(name):
        raise KeyError(name)

    monkeypatch.setattr(canonical_pins, "canonical_pin", broken)
    with pytest.raises(managed_sources.ManagedSourcesError, match="lifecycle pin cannot be determined from astrid/packs/runpod/pins.json"):
        managed_sources.derive_checkout_manifest(checkout)


def test_not_a_checkout_is_refused(tmp_path):
    with pytest.raises(managed_sources.ManagedSourcesError, match="not a git checkout"):
        managed_sources.derive_checkout_manifest(tmp_path)


def test_dry_run_report_shows_every_pin_and_where_it_came_from(checkout):
    report = managed_sources.preflight_source_mode()
    assert report["mode"] == "checkout"
    assert set(report["pins"]) == {"agent", "runtime", "lifecycle", "vibecomfy"}
    assert report["pins"]["agent"]["revision"] == _git(checkout, "rev-parse", "HEAD")
    assert "confirmed on remote 'origin'" in report["pins"]["agent"]["from"]
    assert "runtime.lock.json" in report["pins"]["runtime"]["from"]
    assert all(len(pin["revision"]) == 40 and pin["repository"] for pin in report["pins"].values())


def test_cli_refuses_a_dirty_checkout_before_the_dry_run_and_names_the_flag_choice(checkout, monkeypatch, capsys):
    from astrid.core.generation import cli

    monkeypatch.delenv(managed_sources.SOURCE_MODE_ENV)
    (checkout / "tracked.txt").write_text("edited")
    argv = ["image", "--prompt", "x", "--placement", "runpod", "--dry-run", "--json", "--sources", "checkout"]
    try:
        cli.main(argv)
    except SystemExit:
        pass
    captured = capsys.readouterr()
    assert "uncommitted changes" in captured.out + captured.err
    assert managed_sources.source_mode() == "checkout"  # the flag, not silence, selected it


def test_machines_claim_refuses_before_any_claim(checkout, capsys):
    from astrid.core import machines_cli

    (checkout / "tracked.txt").write_text("edited")
    calls = []
    code = machines_cli.main(["claim", "--max-usd", "1", "--consent", "paid", "--sources", "checkout"],
                             claim=lambda **kw: calls.append(kw), describe=lambda **kw: calls.append(kw))
    assert code == 2 and not calls
    assert "uncommitted changes" in capsys.readouterr().out


def test_unknown_mode_is_refused(monkeypatch):
    monkeypatch.setenv(managed_sources.SOURCE_MODE_ENV, "magic")
    with pytest.raises(managed_sources.ManagedSourcesError, match="not a RunPod source mode"):
        managed_sources.source_mode()


def test_installed_mode_is_the_default_and_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(managed_sources.SOURCE_MODE_ENV, raising=False)
    assert managed_sources.source_mode() == "installed"
    assert managed_sources.preflight_source_mode() is None
    version = tmp_path / "versions" / "v1"
    (version / "py").mkdir(parents=True)
    monkeypatch.setattr(managed_sources.sys, "prefix", str(version / "py"))
    with pytest.raises(managed_sources.ManagedSourcesError, match="installed release manifest"):
        managed_sources._pins()  # no silent fall back to the checkout


def test_cli_dry_run_json_carries_the_derived_pins(checkout, monkeypatch, capsys):
    from astrid.core.generation import cli

    monkeypatch.delenv(managed_sources.SOURCE_MODE_ENV)
    argv = ["image", "--prompt", "x", "--placement", "runpod", "--dry-run", "--json", "--sources", "checkout"]
    try:
        cli.main(argv)
    except SystemExit:
        pass
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    pins = payload["runpod_sources"]["pins"]
    assert pins["agent"]["revision"] == _git(checkout, "rev-parse", "HEAD")
    assert pins["agent"]["from"] and pins["runtime"]["revision"] == RUNTIME_SHA
