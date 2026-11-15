"""One runpod install route: the Agent lock's PyPI pin, on the pod too; installed-layout pins."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from astrid.packs.runpod import managed_sources, pod_bootstrap, pod_source_identity, remote_placement

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "requirements" / "runtime.lock"
FORBIDDEN = ("runpod-python", "runpod_python", "1.12.0+astrid", "SETUPTOOLS_SCM", "RUNPOD_PRETEND_VERSION")


@pytest.mark.parametrize(
    ("lifecycle", "source", "expected"),
    [(0, 0, "skip"), (0, 1, "fresh"), (1, 0, "repair"), (1, 1, "repair")],
)
def test_pod_bootstrap_package_check_requires_import_and_source(tmp_path, lifecycle, source, expected):
    """Execute the shipped package check and install dispatch with controlled probes."""
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nexit {lifecycle}\n")
    python.chmod(0o755)
    script = pod_bootstrap.SCRIPT
    start = script.index("have_all() {")
    end = script.index("\n# pip in the venv.", start)
    fixture = f'''set -euo pipefail
VENV="{tmp_path / 'venv'}"
PACKAGES=(runpod astrid-runtime runpod-lifecycle astrid vibecomfy)
SRC_DIRS=(fixture)
uv() {{ printf '%s\\n' runpod==1 astrid-runtime==1 runpod-lifecycle==1 astrid==1 vibecomfy==1; }}
source_install_current() {{ return {source}; }}
install_runpod_from_lock() {{ :; }}
staged_uv_install() {{ echo "selected:$1"; }}
''' + script[start:end]
    result = subprocess.run(["bash", "-c", fixture], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    if expected == "skip":
        assert "skipping install" in result.stdout
    else:
        assert f"selected:{expected}" in result.stdout
        assert "skipping install" not in result.stdout


def _installed_layout(tmp_path: Path, monkeypatch, *, manifest: dict | None) -> Path:
    """versions/<id>/{py,manifest.json}: the layout `Installer.stage()` writes."""
    version = tmp_path / "versions" / "v1"
    (version / "py").mkdir(parents=True)
    if manifest is not None:
        (version / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(managed_sources.sys, "prefix", str(version / "py"))
    return version


def _manifest(runtime_sha: str) -> dict:
    """What a real install writes: the lifecycle and VibeComfy pins are the Agent's canonical ones."""
    from astrid.core.generation.canonical_pins import canonical_pin

    lifecycle, vibecomfy = canonical_pin("lifecycle"), canonical_pin("vibecomfy")
    return {
        "agent": {"sha": "a" * 40},
        "runtime": {"sha": runtime_sha},
        "lifecycle": {"repo": lifecycle["repository"], "sha": lifecycle["revision"], "version": lifecycle["version"]},
        "vibecomfy": {"sha": vibecomfy["revision"]},
    }


def test_pins_resolve_without_a_checkout(tmp_path, monkeypatch):
    from astrid.core.rendering import _local_schema

    # An installed product root: the lock sits where the installer put it, no checkout around it.
    product = tmp_path / "site-packages"
    product.mkdir()
    (product / "runtime.lock.json").write_text(json.dumps({"source_commit": "c" * 40}))
    seen = []
    real = _local_schema.runtime_lock_path
    monkeypatch.setattr(_local_schema, "runtime_lock_path", lambda root: seen.append(root) or real(root))
    monkeypatch.setattr(managed_sources, "AGENT_ROOT", product)
    _installed_layout(tmp_path, monkeypatch, manifest=_manifest("c" * 40))  # no "runpod" entry

    pins = managed_sources._pins()

    assert seen == [product]
    assert set(pins) == {"src/astrid-agent", "src/astrid-runtime", "src/runpod-lifecycle", "src/VibeComfy"}
    assert pins["src/astrid-runtime"][1] == "c" * 40
    assert pins["src/astrid-agent"][1] == "a" * 40
    assert set(pins) == set(remote_placement.SOURCE_DIRS)
    from astrid.core.generation.canonical_pins import canonical_pin

    assert pins["src/VibeComfy"][1] == canonical_pin("vibecomfy")["revision"]
    assert pins["src/runpod-lifecycle"][1] == canonical_pin("lifecycle")["revision"]


def test_pins_report_a_missing_installed_manifest(tmp_path, monkeypatch):
    product = tmp_path / "site-packages"
    product.mkdir()
    (product / "runtime.lock.json").write_text(json.dumps({"source_commit": "c" * 40}))
    monkeypatch.setattr(managed_sources, "AGENT_ROOT", product)
    version = _installed_layout(tmp_path, monkeypatch, manifest=None)
    with pytest.raises(managed_sources.ManagedSourcesError, match=re.escape(str(version / "manifest.json"))):
        managed_sources._pins()


def test_managed_sources_assume_no_checkout_and_copy_nothing():
    text = Path(managed_sources.__file__).read_text()
    assert "AGENT_ROOT.parent" not in text
    assert '"runtime.lock.json"' not in text
    assert "shutil.copy" not in text


def test_runpod_python_source_dir_is_gone():
    assert not any("runpod-python" in d for d in remote_placement.SOURCE_DIRS)
    assert len(remote_placement.SOURCE_DIRS) == 4
    assert "runpod-python" not in Path(pod_source_identity.__file__).read_text()
    script = pod_bootstrap.SCRIPT
    for needle in FORBIDDEN:
        assert needle not in script, needle
    assert "src/runpod-python" not in re.search(r"SRC_DIRS=\(([^)]*)\)", script).group(1)


def test_pod_bootstrap_installs_runpod_from_the_lock_pin():
    script = pod_bootstrap.SCRIPT
    assert 'AGENT_LOCK="src/astrid-agent/requirements/runtime.lock"' in script
    # The version is never written in the script; it is read from the lock at run time.
    assert not re.search(r"runpod==\d", script)
    assert not re.search(r"\b1\.1[02]\.\d", script)
    assert script.index("install_runpod_from_lock || exit") < script.index('staged_uv_install "$mode"')


def test_pod_bootstrap_extracts_the_lock_pin(tmp_path):
    """Run the real extraction from the shipped script against the real Agent lock."""
    fn = re.search(r"install_runpod_from_lock\(\) \{.*?\n\}\n", pod_bootstrap.SCRIPT, re.S).group(0)
    (tmp_path / "src/astrid-agent/requirements").mkdir(parents=True)
    (tmp_path / "src/astrid-agent/requirements/runtime.lock").write_bytes(LOCK.read_bytes())
    (tmp_path / "boot").mkdir()
    (tmp_path / "bin").mkdir()
    uv = tmp_path / "bin" / "uv"
    # Stub uv: record the requirements file it is given.
    uv.write_text('#!/usr/bin/env bash\nwhile [ $# -gt 0 ]; do [ "$1" = -r ] && cat "$2" > "$CAPTURE"; shift; done\n')
    uv.chmod(0o755)
    script = (f'set -euo pipefail\nVENV=/nonexistent\nBOOTSTRAP_ROOT={tmp_path}/boot\n'
              'AGENT_LOCK="src/astrid-agent/requirements/runtime.lock"\n' + fn + "install_runpod_from_lock\n")
    env = {"PATH": f"{tmp_path}/bin:/usr/bin:/bin", "CAPTURE": str(tmp_path / "captured.txt")}
    done = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    captured = (tmp_path / "captured.txt").read_text()
    lock_pins = re.findall(r"^runpod==\S+", LOCK.read_text(), re.M)
    assert lock_pins and re.findall(r"^runpod==\S+", captured, re.M) == lock_pins
    assert captured.count("--hash=sha256:") == LOCK.read_text().count(
        "--hash=sha256:", LOCK.read_text().index("runpod=="), LOCK.read_text().index("s3transfer=="))
    assert "s3transfer" not in captured


def _refusal(tmp_path, monkeypatch, mutate) -> str:
    from astrid.core.rendering import _local_schema

    product = tmp_path / "site-packages"
    product.mkdir()
    (product / "runtime.lock.json").write_text(json.dumps({"source_commit": "c" * 40}))
    monkeypatch.setattr(managed_sources, "AGENT_ROOT", product)
    monkeypatch.setattr(_local_schema, "runtime_lock_path", lambda root: product / "runtime.lock.json")
    manifest = _manifest("c" * 40)
    mutate(manifest)
    _installed_layout(tmp_path, monkeypatch, manifest=manifest)
    with pytest.raises(managed_sources.ManagedSourcesError) as caught:
        managed_sources._pins()
    return str(caught.value)


def test_a_lifecycle_mismatch_shows_both_pins_and_the_remedy(tmp_path, monkeypatch):
    from astrid.core.generation.canonical_pins import canonical_pin

    text = _refusal(tmp_path, monkeypatch, lambda m: m["lifecycle"].update(sha="d" * 40))
    assert "d" * 40 in text and canonical_pin("lifecycle")["revision"] in text
    assert "release manifest whose pins match this Agent" in text and "astrid/packs/runpod/pins.json" in text


def test_a_vibecomfy_mismatch_shows_both_pins_and_the_remedy(tmp_path, monkeypatch):
    from astrid.core.generation.canonical_pins import canonical_pin

    text = _refusal(tmp_path, monkeypatch, lambda m: m["vibecomfy"].update(sha="e" * 40))
    assert "e" * 40 in text and canonical_pin("vibecomfy")["revision"] in text
    assert "astrid/packs/runpod/pins.json" in text


@pytest.mark.parametrize("repo", ["//user:FAKE_SECRET@example.invalid/l.git",
                                  "https://example.invalid/l.git?token=FAKE_SECRET",
                                  "https://user:FAKE_SECRET@example.invalid/l.git"])
def test_refusals_never_print_url_credentials(tmp_path, monkeypatch, repo):
    text = _refusal(tmp_path, monkeypatch,
                    lambda m: m["lifecycle"].update(repo=repo))
    assert "FAKE_SECRET" not in text and "user:" not in text and "repository <invalid>" in text


def test_a_non_object_vibecomfy_pin_is_refused_cleanly(tmp_path, monkeypatch):
    text = _refusal(tmp_path, monkeypatch, lambda m: m.update(vibecomfy="8d065a8"))
    assert "vibecomfy pin is not an object" in text


def test_a_missing_lifecycle_pin_has_its_own_refusal(tmp_path, monkeypatch):
    text = _refusal(tmp_path, monkeypatch, lambda m: m.pop("lifecycle"))
    assert "has no lifecycle pin" in text and "needs the installed release manifest" not in text
