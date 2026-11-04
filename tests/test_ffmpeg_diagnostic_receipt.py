import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import venv

import pytest

from scripts import ci_journey as journey
from scripts import ffmpeg_diagnostic_receipt as receipt


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setattr(receipt.platform, "machine", lambda: "x86_64")
    root = tmp_path / "program"
    version = root / "versions" / "v1"
    py = version / "py"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(py)
    site = next((py / "lib").glob("python*/site-packages"))
    package = site / "astrid/core/gateway"
    package.mkdir(parents=True)
    for path in (site / "astrid", site / "astrid/core", package):
        (path / "__init__.py").write_text("")
    module = package / "doctor_bundle.py"
    tools = version / "tools/ffmpeg"
    tools.mkdir(parents=True)
    selected = {}
    for name in ("ffmpeg", "ffprobe"):
        binary = tools / name
        binary.write_text("executable")
        binary.chmod(0o755)
        selected[name] = {"source": "download", "executable": str(binary), "version": "7.0.2"}
    def set_selection(value):
        module.write_text("def _selected_tools(root):\n    return " + repr(value) + "\n")
    set_selection(selected)
    manifest = {"release": 1, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40},
                "tools": {"ffmpeg": {"assets": {"linux-x64": {"url": "https://example.test/ffmpeg.tar.xz", "sha256": "c" * 64}}}}}
    (version / "manifest.json").write_text(json.dumps(manifest))
    version_path = version / "version.json"
    version_path.write_text("{}")
    version_data = {"root": str(version), "python": str(py / "bin/python")}
    monkeypatch.setattr(journey, "installation", lambda *args: (version_path, tmp_path / "data", version_data))
    monkeypatch.setattr(journey, "selected_tuple", lambda *args: {})
    candidate = {"app": "d" * 40, "agent": "a" * 40, "manifest_sha256": "e" * 64}
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate))
    gate = SimpleNamespace(validate_tuple=lambda *args: manifest)
    args = SimpleNamespace(candidate=candidate_path)
    state = {"app": tmp_path, "candidate_path": tmp_path / "manifest", "manifest": manifest, "updated": True}
    evidence = tmp_path / "run/evidence/fresh-install"
    log = evidence / "release1/install-bootstrap.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"bootstrap contents\n")
    return SimpleNamespace(root=root, version=version, py=py, module=module, selected=selected,
                           set_selection=set_selection, manifest=manifest, gate=gate, args=args,
                           state=state, evidence=evidence, log=log, data=tmp_path / "data")


def capture(i):
    receipt.capture(journey, i.gate, i.args, i.root, i.data, i.state, i.evidence)
    return json.loads((i.evidence / receipt.RECEIPT).read_text())


def test_installed_doctor_is_invoked_and_bound_to_candidate(installed, monkeypatch):
    i = installed
    monkeypatch.setenv("PYTHONPATH", "/invalid/checkout")
    result = capture(i)
    assert result["status"] == "observed-download"
    assert result["classification"] == "NON-PROOF"
    assert result["candidate_app_sha"] == "d" * 40 and result["candidate_manifest_sha256"] == "e" * 64
    assert result["installed_version_root"] == str(i.version)
    assert result["installed_manifest_sha256"] == hashlib.sha256((i.version / "manifest.json").read_bytes()).hexdigest()
    assert result["doctor_bundle_module"] == str(i.module)
    assert result["selected_tools"] == i.selected
    assert result["ffmpeg_asset"] == {"target": "linux-x64", **i.manifest["tools"]["ffmpeg"]["assets"]["linux-x64"]}
    assert result["bootstrap_log"]["sha256"] == hashlib.sha256(i.log.read_bytes()).hexdigest()


@pytest.mark.parametrize("component", ["tools", "ffmpeg"])
def test_tool_directory_symlink_outside_version_is_inconclusive(installed, tmp_path, component):
    i = installed
    tools = i.version / "tools"
    ffmpeg_dir = tools / "ffmpeg"
    external = tmp_path / "external"
    if component == "tools":
        tools.rename(external)
        tools.symlink_to(external, target_is_directory=True)
    else:
        ffmpeg_dir.rename(external)
        ffmpeg_dir.symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError):
        capture(i)
    result = json.loads((i.evidence / receipt.RECEIPT).read_text())
    assert result["status"] == "inconclusive"
    assert result["reason"]


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe"])
@pytest.mark.parametrize("problem", ["outside", "dangling", "loop"])
def test_executable_symlink_errors_are_inconclusive(installed, tmp_path, name, problem):
    i = installed
    binary = Path(i.selected[name]["executable"])
    binary.unlink()
    target = tmp_path / "external"
    if problem == "outside":
        target.write_text("x")
        target.chmod(0o755)
    elif problem == "loop":
        target = binary
    binary.symlink_to(target)
    with pytest.raises(RuntimeError):
        capture(i)
    result = json.loads((i.evidence / receipt.RECEIPT).read_text())
    assert result["status"] == "inconclusive" and result["reason"]


def test_tool_directory_symlink_within_version_is_accepted(installed):
    i = installed
    tools = i.version / "tools"
    ffmpeg_dir = tools / "ffmpeg"
    internal = i.version / "downloaded-ffmpeg"
    ffmpeg_dir.rename(internal)
    ffmpeg_dir.symlink_to(internal, target_is_directory=True)
    assert capture(i)["status"] == "observed-download"


def test_resolution_error_is_inconclusive(installed, monkeypatch):
    i = installed
    expected = i.version / "tools/ffmpeg"
    resolve = Path.resolve

    def fail_resolve(path, *args, **kwargs):
        if path == expected:
            raise OSError("cannot resolve tools directory")
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    with pytest.raises(RuntimeError, match="cannot resolve tools directory"):
        capture(i)
    result = json.loads((i.evidence / receipt.RECEIPT).read_text())
    assert result["status"] == "inconclusive" and "cannot resolve" in result["reason"]


@pytest.mark.parametrize("problem", ["path", "outside", "symlink", "missing", "module", "manifest", "bootstrap", "no-log"])
def test_unavailable_or_wrong_selection_never_counts_as_download(installed, problem, tmp_path):
    i = installed
    if problem == "path":
        i.selected["ffmpeg"]["source"] = "path"
        i.set_selection(i.selected)
    elif problem in ("outside", "symlink"):
        external = tmp_path / "external"
        external.write_text("x")
        external.chmod(0o755)
        if problem == "outside":
            i.selected["ffmpeg"]["executable"] = str(external)
        else:
            binary = Path(i.selected["ffmpeg"]["executable"])
            binary.unlink()
            binary.symlink_to(external)
        i.set_selection(i.selected)
    elif problem == "missing":
        i.set_selection({})
    elif problem == "module":
        i.module.unlink()
    elif problem == "manifest":
        (i.version / "manifest.json").write_text('{}')
    elif problem == "bootstrap":
        i.state.pop("updated")
    elif problem == "no-log":
        i.log.unlink()
    with pytest.raises(RuntimeError):
        capture(i)
    result = json.loads((i.evidence / receipt.RECEIPT).read_text())
    assert result["status"] == "inconclusive" and result["reason"]
    assert result["bootstrap_log"]["status"] == ("unavailable" if problem == "no-log" else "captured")


def test_complete_bootstrap_log_and_receipt_are_staged(installed, monkeypatch):
    i = installed
    i.log.write_bytes(b"beginning\n" + b"x" * (journey.EVIDENCE_LOG_LIMIT + 10) + b"\nerror\n")
    capture(i)
    journey.write_json(i.evidence / "journey-result.json", {"scenario": "fresh-install", "proof_mode": "keep-going-diagnostic"})
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *args: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *args: None)
    journey.snapshot_failure_evidence(i.evidence, i.evidence.parents[1], i.data, "before-cleanup")
    staged = i.evidence.parents[1] / "evidence-upload/before-cleanup"
    assert (staged / receipt.BOOTSTRAP_LOG).read_bytes() == i.log.read_bytes()
    assert json.loads((staged / receipt.RECEIPT).read_text())["status"] == "observed-download"
