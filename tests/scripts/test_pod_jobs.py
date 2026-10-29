"""Focused job-list runner acceptance over real lifecycle loopback transport."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import sys
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from tests.helpers.pod_jobs_runtime import LoopbackPod, png_bytes


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/pod_session.py"
spec = importlib.util.spec_from_file_location("pod_jobs_under_test", SCRIPT)
ps = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ps
spec.loader.exec_module(ps)

REQUEST = {"version":1, "operation":"generate", "source":None, "output":{"duration":9},
           "content":{"prompt":"A blue room with a soft light."}, "changes":{"video":[], "audio":[]},
           "references":[{"asset":"reference", "purpose":"appearance"}], "overrides":{}}


def make_manifest(root, *, mixed=True, failure=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "reference.png").write_bytes(png_bytes())
    (root / "request.json").write_text(json.dumps(REQUEST))
    (root / "notes.txt").write_text("Preserve this exact input.\n")
    (root / "generate.py").write_text(
        "import json,pathlib,sys\n"
        "assert pathlib.Path('data/notes.txt').read_text() == 'Preserve this exact input.\\n'\n"
        "output=pathlib.Path('generated.png')\n"
        + ("output.write_text('invalid PNG')\n" if failure == "png" else "output.write_bytes(pathlib.Path('data/reference.png').read_bytes())\n")
        + ("pathlib.Path('result.json').write_text('{invalid json')\n" if failure == "json" else "pathlib.Path('result.json').write_text(json.dumps({'ok':True, 'input':'data/notes.txt'}))\n")
        + ("sys.exit(7)\n" if failure == "command" else ""))
    cmd = {"id":"image", "type":"cmd", "goal":"Generate a reviewable PNG and result receipt.",
           "prompt":"A small blue rectangle.", "command":[sys.executable, "generate.py"],
           "input-files":{"generate.py":"generate.py", "data/reference.png":"reference.png", "data/notes.txt":"notes.txt"},
           "expected-outputs":[{"path":"generated.png", "kind":"png", "width":32, "height":24}, "result.json"],
           "timeout":20}
    first = {"id":"clip", "type":"h3", "goal":"Produce a nine-second audiovisual clip.",
             "request":"request.json", "assets":{"reference":"reference.png"}, "timeout":40}
    second = dict(cmd, id="later", command=[sys.executable, "-c", "raise SystemExit('later must not execute')"])
    document = {"version":1, "jobs":[first, cmd] if mixed else [cmd, second]}
    path = root / "jobs.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path, document


def run_jobs(tmp_path, monkeypatch, *, mixed=True, failure=None, budget=None, release_status=None):
    pod = LoopbackPod(tmp_path / "pod", ps, monkeypatch, sanity_fail=failure == "sanity",
                      corrupt_download="generated.png" if failure == "hash" else None)
    try:
        manifest, document = make_manifest(tmp_path / "manifest", mixed=mixed, failure=failure)
        args = ps.build_parser().parse_args([
            "--datacenters", "CPU-FIXTURE", "--sources-dir", str(pod.sources), "--jobs", str(manifest),
            "--workdir", str(tmp_path / "session"), "--token-dir", str(tmp_path / "tokens"), "--poll-interval", ".01"])
        messages = []
        session = ps.JobListSession(args, cli=pod.cli, transport_factory=pod.factory,
                                   http_status=pod.http_status if release_status is None else lambda _pod: release_status,
                                   sleep=lambda _seconds: None, say=messages.append, budget=budget,
                                   agent_root=pod.agent_root)
        code = session.run()
        (session.workdir / "http-trace.json").write_text(json.dumps(pod.requests, indent=2) + "\n")
        if pod.setup_log.exists():
            (session.workdir / "setup-trace.txt").write_text(pod.setup_log.read_text())
        (session.workdir / "harness-boundaries.json").write_text(json.dumps({
            "boundary":"fixture pod allocation, bootstrap/install/GPU and H3 model/action",
            "production":"AgentServer, HTTPS transfer, detached jobs, hash/probe and packet runner",
            "agent_job_records":pod.record_count, "runner_budget":session.budget.count,
            "comfy_prompt_count":len(pod.comfy_prompts), "terminated":pod.terminated,
        }, indent=2) + "\n")
        return session, pod, code, messages, manifest, document
    finally:
        pod.close()


def assert_release_verified(session, pod):
    assert pod.events.count("terminate") == 1
    assert "release-get" in pod.events and pod.events[-1] == "list"
    assert session.released and pod.terminated
    assert session.records[-1]["step"] == "release"
    assert session.records[-1]["result"] == "ok"
    assert session.records[-1]["detail"] == {"pods":["fakepod134"], "get":404, "list":[]}


def test_two_job_cpu_loopback_packets_progress_budget_and_release(tmp_path, monkeypatch):
    # Model pyenv's PATH rewrite in the detached python3 launcher. The fixture
    # must bypass both this shim and the unrelated astrid it would prioritize.
    host_bin = tmp_path / "host bin"
    host_bin.mkdir()
    host_log = tmp_path / "host-cli-invocations.txt"
    python = host_bin / "python3"
    python.write_text(
        f'#!/bin/sh\nprintf "python3\\n" >> {shlex.quote(str(host_log))}\n'
        f'export PATH={shlex.quote(str(host_bin))}:$PATH\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    astrid = host_bin / "astrid"
    astrid.write_text(f'#!/bin/sh\nprintf "astrid\\n" >> {shlex.quote(str(host_log))}\nexit 97\n')
    astrid.chmod(0o755)
    monkeypatch.setenv("PATH", str(host_bin) + os.pathsep + os.environ["PATH"])
    session, pod, code, messages, manifest, document = run_jobs(tmp_path, monkeypatch)
    assert code == 0, "\n".join(messages)
    assert not host_log.exists(), "fake pod invoked a host Python/astrid shim"
    assert_release_verified(session, pod)
    assert session.budget.count == pod.record_count < 40
    steps = ["claim", "bundle", "bootstrap", "sanity", "install", "rerun", "h3:clip:s0",
             "job:clip", "download:clip", "packet:clip", "job:image", "download:image", "packet:image", "release"]
    assert [record["step"] for record in session.records] == steps
    local_progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
    remote_progress = json.loads((session.workdir / "pod-session-progress.remote.json").read_text())
    assert [record["step"] for record in local_progress["steps"]] == steps
    assert [record["step"] for record in remote_progress["steps"]] == steps[:-1]
    assert local_progress["jobs"] == remote_progress["jobs"] == session.budget.count
    assert all(record["result"] == "ok" for record in local_progress["steps"])
    assert ("GET", "/health") in pod.requests
    assert ("POST", "/jobs") in pod.requests and ("POST", "/uploads") in pod.requests
    assert any(path == "/download-chunk" for _method, path in pod.requests)
    setup = pod.setup_log.read_text().splitlines()
    assert setup.index("bootstrap") < setup.index("sanity") < setup.index("install") < setup.index("h3:1") < setup.index("h3:9")
    assert [p["prompt"]["1"]["inputs"]["seconds"] for p in pod.comfy_prompts] == [1, 9]

    packets = session.workdir / "packets"
    assert {p.name for p in packets.iterdir()} == {"clip", "image"}
    clip, image = packets / "clip", packets / "image"
    for packet, job in zip((clip, image), document["jobs"]):
        readme = (packet / "README.md").read_text()
        check = json.loads((packet / "machine-check.json").read_text())
        assert job["goal"] in readme
        assert job.get("prompt", REQUEST["content"]["prompt"]) in readme
        assert "passed" in readme.lower() and check["status"] == "passed"
        for relative, evidence in check["outputs"].items():
            output = packet / "outputs" / relative
            assert output.is_file()
            assert evidence["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
            assert evidence["bytes"] == output.stat().st_size
    assert (clip / "inputs/request.json").read_bytes() == (manifest.parent / "request.json").read_bytes()
    assert json.loads((clip / "inputs/smoke-request.json").read_text())["output"]["duration"] == 1
    assert (clip / "inputs/assets/reference.png").read_bytes() == (manifest.parent / "reference.png").read_bytes()
    assert (image / "inputs/data/notes.txt").read_bytes() == (manifest.parent / "notes.txt").read_bytes()
    assert (image / "inputs/generate.py").read_bytes() == (manifest.parent / "generate.py").read_bytes()
    assert json.loads((image / "outputs/result.json").read_text()) == {"ok":True, "input":"data/notes.txt"}
    assert (image / "outputs/generated.png").read_bytes() == png_bytes()
    ps.check_video(ps.probe_video(clip / "outputs/final.mp4"))
    assert json.loads((clip / "outputs/receipt.json").read_text())["states"]["final_composition_publication"]["status"] == "verified"
    assert json.loads((clip / "outputs/composition-manifest.json").read_text())["duration"] == 9
    assert (clip / "outputs/side-by-side.mp4").stat().st_size > 0
    comparison = ps.probe_video(clip / "outputs/side-by-side.mp4")
    assert comparison["frames"] == 216 and comparison["width"] > comparison["height"]
    assert not list((tmp_path / "tokens").glob("*.token"))


@pytest.mark.parametrize("failure", ["png", "json", "hash", "command", "sanity"])
def test_failed_two_job_rehearsal_stops_and_releases(tmp_path, monkeypatch, failure):
    session, pod, code, messages, _manifest, _document = run_jobs(tmp_path, monkeypatch, mixed=False, failure=failure)
    assert code == 1, "\n".join(messages)
    assert_release_verified(session, pod)
    assert session.budget.count == pod.record_count < 40
    failed = [r for r in session.records if r["result"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["step"] == ("sanity" if failure == "sanity" else "job:image" if failure == "command" else "download:image")
    assert not any(record["step"].endswith(":later") for record in session.records)
    progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
    assert progress["steps"][-1]["step"] == "release"
    assert any("POD SESSION FAILED" in message for message in messages)
    if failure == "sanity":
        assert "install" not in pod.setup_log.read_text().splitlines()
    else:
        check = json.loads((session.workdir / "packets/image/machine-check.json").read_text())
        assert check["status"] == "failed"
        assert "failed" in (session.workdir / "packets/image/README.md").read_text().lower()


def test_budget_exhaustion_still_releases_without_allocating_a_record(tmp_path, monkeypatch):
    session, pod, code, messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, budget=ps.JobBudget(limit=8))
    assert code == 1 and session.budget.count < 8
    assert_release_verified(session, pod)
    assert session.budget.count == pod.record_count
    assert any("job budget" in message for message in messages)


def test_release_verification_failure_keeps_session_failed(tmp_path, monkeypatch):
    session, pod, code, messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, failure="command", release_status=200)
    assert code == 1 and pod.terminated and not session.released
    assert pod.events.count("terminate") == 1 and pod.events.count("list") == 12
    assert session.records[-1]["step"] == "release" and session.records[-1]["result"] == "failed"
    assert "not gone" in session.records[-1]["detail"]["error"]
    assert any("RELEASE FAILED" in message for message in messages)


def test_timeout_cancels_the_exact_detached_job(tmp_path, monkeypatch):
    class SlowTransport:
        def __init__(self): self.cancelled = []

        def detached_start(self, command, *, root):
            assert command == "sleep 3600"
            return {"id":"slow-job", "dir":root + "/slow-job"}

        def detached_poll(self, job_id, *, root):
            assert job_id == "slow-job"
            return {"state":"running"}

        def detached_cancel(self, job_id, *, root):
            self.cancelled.append((job_id, root))

    session = ps.Session(Namespace(workdir=str(tmp_path / "session"), poll_interval=0),
                         cli=lambda *_args: None, transport_factory=lambda *_args: None,
                         http_status=lambda _pod: 404, say=lambda _message: None)
    session.transport = SlowTransport()
    times = iter([10.0, 12.0])
    monkeypatch.setattr(ps, "time", Namespace(monotonic=lambda: next(times)))
    with pytest.raises(ps.SessionError, match="did not exit within 1 s"):
        session.run_job("slow", "sleep 3600", 1)
    assert session.transport.cancelled == [("slow-job", ps.JOB_ROOT)]
    assert session.budget.count == 1


@pytest.mark.parametrize("change", [
    {"id":"../escape"}, {"type":"unknown"}, {"command":[]},
    {"input-files":{"../escape":"notes.txt"}}, {"expected-outputs":["../escape.png"]},
    {"expected-outputs":["/absolute.png"]}, {"expected-outputs":["same.png", "same.png"]},
    {"expected-outputs":[{"path":"output.bin", "kind":"unknown"}]}, {"timeout":0}, {"timeout":True},
])
def test_manifest_rejects_unsafe_or_incomplete_jobs_before_claim(tmp_path, change):
    manifest, document = make_manifest(tmp_path, mixed=False)
    document["jobs"][0].update(change)
    manifest.write_text(yaml.safe_dump(document))
    with pytest.raises(ps.SessionError):
        ps.load_jobs(manifest)


def test_manifest_resolves_assets_and_inputs_relative_to_manifest(tmp_path, monkeypatch):
    manifest, _document = make_manifest(tmp_path / "manifest")
    monkeypatch.chdir(tmp_path)
    h3, cmd = ps.load_jobs(manifest)
    assert h3["request"] == manifest.parent / "request.json"
    assert h3["assets"] == {"reference":manifest.parent / "reference.png"}
    assert cmd["input-files"]["data/notes.txt"] == manifest.parent / "notes.txt"
    assert cmd["expected-outputs"][1] == {"path":"result.json", "kind":"json"}


def test_h3_manifest_rejects_missing_references_and_duplicate_ids(tmp_path):
    manifest, document = make_manifest(tmp_path)
    document["jobs"][0]["assets"] = {"different":"reference.png"}
    manifest.write_text(yaml.safe_dump(document))
    with pytest.raises(ps.SessionError, match="missing assets"):
        ps.load_jobs(manifest)
    document["jobs"][0]["assets"] = {"reference":"reference.png"}
    document["jobs"][1]["id"] = "clip"
    manifest.write_text(yaml.safe_dump(document))
    with pytest.raises(ps.SessionError, match="unique"):
        ps.load_jobs(manifest)
