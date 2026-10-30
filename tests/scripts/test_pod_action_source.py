"""CPU source-bound actions: real Git/lock/identity checks, no provider or media."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from argparse import Namespace

import pytest

from scripts import pod_action_source as source
from tests.helpers.runtime import runtime_checkout

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/pod_session.py"
spec = importlib.util.spec_from_file_location("source_bound_session", SCRIPT)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()


def commit(root):
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "source")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def candidate(tmp_path_factory):
    root = tmp_path_factory.mktemp("source-selection").resolve()
    # Quotes, spaces and shell syntax must stay literal in both roots and argv.
    agent, runtime = root / "agent ' ; $literal", root / "runtime ' ; $literal"
    agent.mkdir()
    runtime.mkdir()
    real = runtime_checkout()
    for name in ("astrid_runtime", "runtime_protocol"):
        shutil.copytree(real / name, runtime / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (runtime / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (runtime / "contract").mkdir()
    artifact = b"locked bytes"
    digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
    (runtime / "contract/item").write_bytes(artifact)
    (runtime / "contract/artifacts.json").write_text(json.dumps({
        "producer": "astrid-runtime", "version": "fixture", "artifacts": {"contract/item": digest}}))
    runtime_head = commit(runtime)
    for name in ("astrid", "astrid_runtime_client"):
        (agent / name).mkdir()
        (agent / name / "__init__.py").write_text("")
    # A synthetic CLI reports imports and a child interpreter's imports. The
    # selection checker and canonical Runtime identity implementation are real.
    (agent / "astrid/__main__.py").write_text(
        "import json,os,pathlib,subprocess,sys,astrid,astrid_runtime\n"
        "profile=json.loads(pathlib.Path(os.environ['ASTRID_RUNTIME_SOURCE_MANIFEST']).read_text())\n"
        "child=subprocess.check_output([sys.executable,'-c',"
        "'import astrid,astrid_runtime,json; print(json.dumps([astrid.__file__,astrid_runtime.__file__]))']).decode()\n"
        "print(json.dumps({'argv':sys.argv[1:],'agent':astrid.__file__,'runtime':astrid_runtime.__file__,"
        "'profile':profile,'child':json.loads(child)}))\n")
    (agent / "scripts").mkdir()
    shutil.copyfile(SCRIPT.with_name("vendor_lock.py"), agent / "scripts/vendor_lock.py")
    (agent / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (agent / "vendored").write_bytes(artifact)
    (agent / "runtime.lock.json").write_text(json.dumps({
        "source_commit": runtime_head, "producer": "astrid-runtime", "producer_version": "fixture",
        "manifest": "contract/artifacts.json", "files": {"vendored": {"from": "contract/item", "sha256": digest}}}))
    return {"agent_root": str(agent), "runtime_root": str(runtime), "agent_head": commit(agent),
            "runtime_head": runtime_head,
            "runtime_lock_sha256": hashlib.sha256((agent / "runtime.lock.json").read_bytes()).hexdigest()}


def execute(tmp_path, monkeypatch, selected, overrides=None):
    clip = tmp_path / "clip ' ; $literal"
    clip.mkdir()
    monkeypatch.setattr(ps, "CLIP_DIR", str(clip))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "default-called"
    default = bin_dir / "astrid"
    default.write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\nprintf default\n")
    default.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASTRID_") and not k.upper().startswith("GIT_")}
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["PYTHONPATH"] = str(bin_dir)  # selected process must ignore this default.
    env.update(overrides or {})
    request = str(clip / "request ' ; $(touch INJECTED).json")
    assets = str(clip / "assets ' ; $(touch INJECTED).json")
    out = str(clip / "output ' ; $(touch INJECTED)")
    command = ps.action_script("fixture", request, assets, out, source=selected)
    command = command.replace("/opt/astrid/bin/python -I", shlex.quote(sys.executable) + " -I", 1)
    done = subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    return done, clip, marker, (request, assets, out)


def test_source_bound_action_selects_exact_pair_and_child_environment(candidate, tmp_path, monkeypatch):
    done, clip, marker, (request, assets, out) = execute(tmp_path, monkeypatch, candidate)
    assert done.returncode == 0, done.stderr
    result = json.loads((clip / "runs/fixture.summary.json").read_text())
    expected = [candidate["agent_root"] + "/astrid/__init__.py", candidate["runtime_root"] + "/astrid_runtime/__init__.py"]
    assert [result["agent"], result["runtime"]] == expected == result["child"]
    assert result["profile"]["source_checkout"] == candidate["agent_root"]
    assert result["profile"]["runtime_checkout"] == candidate["runtime_root"]
    assert result["argv"] == ["action", "run", ps.ACTION, "--request", request, "--asset-map", assets,
                              "--placement", "local", "--out", out]
    assert not marker.exists() and not (clip / "INJECTED").exists() and not (tmp_path / "INJECTED").exists()
    evidence = json.loads((clip / "runs/fixture.summary.json.source.json").read_text())
    assert evidence["identity"]["dirty"] is False
    assert evidence["identity"]["agent_head"] == candidate["agent_head"]
    assert evidence["identity"]["runtime_head"] == candidate["runtime_head"]
    assert candidate["agent_root"] not in json.dumps(evidence)
    assert candidate["runtime_root"] not in json.dumps(evidence)


@pytest.mark.parametrize("fault", ["agent_head", "runtime_head", "runtime_lock_sha256", "missing_root", "symlink", "dirty", "vendor", "lock_pin", "imports"])
def test_invalid_candidate_never_calls_action_or_default(candidate, tmp_path, monkeypatch, fault):
    selected = dict(candidate)
    cleanup = None
    if fault in ("agent_head", "runtime_head", "runtime_lock_sha256"):
        selected[fault] = "0" * len(selected[fault])
    elif fault == "missing_root":
        selected["agent_root"] = str(tmp_path / "absent")
    elif fault == "symlink":
        link = tmp_path / "alias"
        link.symlink_to(candidate["agent_root"], target_is_directory=True)
        selected["agent_root"] = str(link)
    elif fault == "dirty":
        cleanup = Path(candidate["runtime_root"]) / "untracked.py"
        cleanup.write_text("raise RuntimeError('not authorized')")
    elif fault in ("vendor", "lock_pin"):
        replacement = tmp_path / "different-agent"
        shutil.copytree(candidate["agent_root"], replacement)
        if fault == "vendor":
            (replacement / "vendored").write_bytes(b"changed")
        else:
            lock = replacement / "runtime.lock.json"
            value = json.loads(lock.read_text())
            value["source_commit"] = "0" * 40
            lock.write_text(json.dumps(value))
        selected.update(agent_root=str(replacement), agent_head=commit(replacement),
                        runtime_lock_sha256=hashlib.sha256((replacement / "runtime.lock.json").read_bytes()).hexdigest())
    else:
        empty = tmp_path / "empty-agent"
        empty.mkdir()
        (empty / "unrelated.txt").write_text("not Agent")
        selected.update(agent_root=str(empty), agent_head=commit(empty))
    try:
        done, clip, marker, _args = execute(tmp_path, monkeypatch, selected)
        assert done.returncode != 0
        assert done.stderr.strip() == "H3 selected source identity verification failed"
        assert not marker.exists()
        assert not (clip / "runs/fixture.summary.json.source.json").exists()
    finally:
        if cleanup is not None:
            cleanup.unlink()


@pytest.mark.parametrize("key", ["ASTRID_RUNTIME_ENDPOINT", "ASTRID_RUNTIME_CREDENTIAL", "ASTRID_RUNTIME_LAUNCHER",
                                  "ASTRID_INSTALL_RELEASE", "ASTRID_INSTALL_MANAGED_BY", "ASTRID_TIMELINE_EVAL_OVERRIDE"])
def test_ambient_identity_override_refused(candidate, tmp_path, monkeypatch, key):
    done, clip, marker, _args = execute(tmp_path, monkeypatch, candidate, {key: "fixture-private-value"})
    assert done.returncode != 0 and not marker.exists()
    assert "fixture-private-value" not in done.stderr
    assert not (clip / "runs/fixture.summary.json.source.json").exists()


def test_unselected_action_keeps_default_behavior(tmp_path, monkeypatch):
    done, clip, marker, _args = execute(tmp_path, monkeypatch, None)
    assert done.returncode == 0 and marker.exists()
    assert (clip / "runs/fixture.summary.json").read_text() == "default"
    assert not (clip / "runs/fixture.summary.json.source.json").exists()


@pytest.mark.parametrize("fault", ["absent", "json", "missing_field", "extra", "duplicate", "relative", "separator", "control", "same_root"])
def test_bad_selection_refused_before_manifest_or_transport(candidate, tmp_path, monkeypatch, fault):
    selected = dict(candidate)
    if fault == "missing_field": del selected["runtime_head"]
    if fault == "extra": selected["typo"] = "unused"
    if fault == "relative": selected["agent_root"] = "relative"
    if fault == "separator": selected["agent_root"] = "/root/a:/root/b"
    if fault == "control": selected["agent_root"] = "/root/a\nnext"
    if fault == "same_root": selected["runtime_root"] = selected["agent_root"]
    path = tmp_path / "selection.json"
    if fault != "absent":
        payload = json.dumps(selected)
        if fault == "json": payload = "{"
        if fault == "duplicate": payload = payload[:-1] + ',"agent_head":"' + "0" * 40 + '"}'
        path.write_text(payload)
    monkeypatch.setattr(ps, "load_jobs", lambda *_: pytest.fail("manifest accessed before selection validation"))
    with pytest.raises(ps.SessionError, match="selection is missing or invalid"):
        ps.JobListSession(Namespace(h3_source_selection=str(path), jobs="unopened.yaml"))


def test_source_selection_requires_h3_rows(candidate, tmp_path, monkeypatch):
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(candidate))
    monkeypatch.setattr(ps, "load_jobs", lambda *_: [{"type": "cmd"}])
    with pytest.raises(ps.SessionError, match="requires an H3 row"):
        ps.JobListSession(Namespace(h3_source_selection=str(path), jobs="unopened.yaml"))


@pytest.mark.parametrize("smoke,exit_code", [(False, 0), (True, 0), (False, 1)])
def test_row_seam_records_selected_identity_without_new_jobs(candidate, tmp_path, monkeypatch, smoke, exit_code):
    session = object.__new__(ps.JobListSession)
    session.sid, session.h3_source = "fixture", candidate
    job = {"id": "row", "type": "h3", "remote": "/root/jobs/row", "packet": tmp_path, "timeout": 90}
    out = job["remote"] + ("/s0" if smoke else "/out")
    evidence = {"schema": "astrid.pod-action-source.v1", **{k: candidate[k] for k in
                 ("agent_head", "runtime_head", "runtime_lock_sha256")}, "identity": {
                 "release": 0, "managed_by": "source", "dirty": False, "dirty_hash": "a" * 64,
                 **{k: candidate[k] for k in ("agent_head", "runtime_head")}}}
    calls = []
    def run(label, command, timeout, **kwargs):
        calls.append(command)
        assert "/opt/astrid/bin/python -I -c" in command
        return exit_code, "", {"id": "one", "dir": "/root/job"}
    def fetch(path):
        if path.endswith("source.json"): return json.dumps(evidence)
        if path.endswith("summary.json"): return json.dumps({"ok": True, "out": out})
        return ""
    monkeypatch.setattr(session, "run_job", run)
    monkeypatch.setattr(session, "fetch", fetch)
    if exit_code:
        with pytest.raises(ps.JobRowError):
            session.run_manifest_job(job, smoke=smoke)
    else:
        detail = session.run_manifest_job(job, smoke=smoke)
        assert detail["source"] == evidence
    assert len(calls) == 1
    phase = "s0" if smoke else "job"
    assert json.loads((tmp_path / "logs" / f"{phase}.source-identity.json").read_text()) == evidence
