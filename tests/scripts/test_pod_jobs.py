"""Focused job-list runner acceptance over real lifecycle loopback transport."""
from __future__ import annotations

import hashlib
import base64
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
    second = dict(cmd, id="later", command=[sys.executable, "-c",
        "import json,pathlib; pathlib.Path('later.png').write_bytes(pathlib.Path('data/reference.png').read_bytes()); pathlib.Path('result.json').write_text(json.dumps({'ok': True}))"],
        **{"expected-outputs":[{"path":"later.png", "kind":"png", "width":32, "height":24}, "result.json"]})
    document = {"version":1, "jobs":[first, cmd] if mixed else [cmd, second]}
    path = root / "jobs.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path, document


def run_jobs(tmp_path, monkeypatch, *, mixed=True, failure=None, budget=None, release_status=None,
             missing_storage=False, fail_row_transport=False, single=False, hold=False,
             configure=None, after_run=None, process_boundary=False):
    pod = LoopbackPod(tmp_path / "pod", ps, monkeypatch, sanity_fail=failure == "sanity",
                      runtime_fail=failure == "runtime",
                      corrupt_download="generated.png" if failure == "hash" else None)
    try:
        # Model the production Runtime probe's source checks for all transported
        # checkouts and emit stable identity fields across separate sessions.
        (pod.agent_root / "scripts/pod_runtime_ready.py").write_text(
            'import base64,json,os,pathlib,subprocess,sys\n'
            'request = json.loads(base64.b64decode(sys.argv[1]))\n'
            'root = pathlib.Path(__file__).resolve().parents[2]\n'
            'manifest = json.loads((root / ".pod-source.json").read_text())\n'
            'for name, expected in manifest["heads"].items():\n'
            '    checkout = root / name\n'
            '    head = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"]).decode().strip()\n'
            '    dirty = subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"])\n'
            '    if head != expected or dirty: sys.exit(3)\n'
            'with pathlib.Path(os.environ["POD_JOBS_SETUP_LOG"]).open("a") as log: log.write("runtime-ready\\n")\n'
            'identity = {"agent_head": manifest["heads"]["astrid-agent"], "runtime_head": manifest["heads"]["astrid-runtime"], "managed_by": "source", "dirty": False}\n'
            + f'print(json.dumps({{**request, "ready": {failure != "runtime"!r}, "stopped": True, "identity": identity}}))\n')
        # Synthetic committed sources exercise the production exact-Git bundler.
        for root in [*(pod.sources / name for name in ps.BUNDLE_TREES[1:]), pod.agent_root]:
            if root == pod.agent_root:
                (root / "runtime.lock.json").write_text(json.dumps({"source_commit":
                    ps._bundle_git(pod.sources / "astrid-runtime", "rev-parse", "HEAD").decode().strip()}))
            ps._bundle_git(root, "init", "-q")
            ps._bundle_git(root, "add", ".")
            ps._bundle_git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                           "commit", "-qm", "synthetic source")
        pod.launches = []
        def cli(argv, env):
            if missing_storage and argv[0] == "list":
                # No pod exists after failed attachment; preserve scoped cleanup evidence.
                pod.events.append("list")
                return 0, "[]", ""
            if argv[0] == "launch":
                pod.launches.append(argv)
                assert argv[argv.index("--datacenter-id") + 1] == ps.DATACENTER
                assert argv[argv.index("--storage-name") + 1] == ps.STORAGE_NAME
                assert env["RUNPOD_STORAGE_NAME"] == ps.STORAGE_NAME
                assert env["RUNPOD_ATTACH_ONLY"] == "true" and env["RUNPOD_STORAGE_VOLUMES"] == ""
                assert "--storage-volumes" not in argv
                if missing_storage:
                    return 1, "", "error: existing storage not found (attach-only)"
            if argv[0] == "list" and "--name-prefix" in argv and not pod.terminated:
                pod.events.append("list")
                return 0, json.dumps([{"id":"fakepod134", "name":session_holder["session"].name}]), ""
            return pod.cli(argv, env)
        manifest, document = make_manifest(tmp_path / "manifest", mixed=mixed, failure=failure)
        if single:
            document["jobs"] = [document["jobs"][0]]
            manifest.write_text(yaml.safe_dump(document, sort_keys=False))
        args = ps.build_parser().parse_args([
            "--datacenters", ps.DATACENTER, "--storage-name", ps.STORAGE_NAME, "--sources-dir", str(pod.sources), "--jobs", str(manifest),
            "--workdir", str(tmp_path / "session"), "--token-dir", str(tmp_path / "tokens"), "--poll-interval", ".01"]
            + (["--hold-on-row-failure"] if hold else []))
        messages = []
        session_holder = {}
        def transport_factory(pod_id, token):
            assert pod_id == "fakepod134"
            transport = pod.transport if pod.transport is not None else pod.factory(pod_id, token)
            if fail_row_transport:
                start = transport.detached_start
                def fail_row_start(command, *, root, job_id=None, launch_timeout=60):
                    session = session_holder["session"]
                    if f"{ps.CLIP_DIR}/jobs/{session.sid}-image" in command:
                        raise RuntimeError("fixture transport disconnected")
                    return start(command, root=root, job_id=job_id, launch_timeout=launch_timeout)
                transport.detached_start = fail_row_start
            return transport
        session = ps.JobListSession(args, cli=cli, transport_factory=transport_factory,
                                   http_status=pod.http_status if release_status is None else lambda _pod: release_status,
                                   sleep=lambda _seconds: None, say=messages.append, budget=budget,
                                   agent_root=pod.agent_root)
        session.test_messages = messages
        session_holder["session"] = session
        if configure:
            configure(session, pod)
        if process_boundary:
            # Keep the loopback service in the parent; only the controller exits.
            # This fixture-only value never appears in an argument or report.
            fixture_token = "T" * 64
            pod.factory("fakepod134", fixture_token)
            monkeypatch.setattr(ps.secrets, "token_urlsafe", lambda _n: fixture_token)
            reader, writer = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(reader)
                try:
                    child_code = session.run()
                    summary = {"code": child_code, "events": pod.events,
                               "launches": len(pod.launches), "pid": os.getpid()}
                    with os.fdopen(writer, "w") as output:
                        json.dump(summary, output)
                    os._exit(0)
                except BaseException:
                    os._exit(2)
            os.close(writer)
            with os.fdopen(reader) as stream:
                summary = json.load(stream)
            exited_pid, status = os.waitpid(pid, 0)
            assert exited_pid == summary["pid"] == pid and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
            assert pid != os.getpid()
            pod.events.extend(summary["events"])
            pod.launches = [None] * summary["launches"]
            code = summary["code"]
            session.pod_id = json.loads((session.workdir / "held-session.json").read_text())["pod_id"]
        else:
            code = session.run()
        if after_run:
            after_run(session, pod, code, cli, transport_factory)
        (session.workdir / "http-trace.json").write_text(json.dumps(pod.requests, indent=2) + "\n")
        if pod.setup_log.exists():
            (session.workdir / "setup-trace.txt").write_text(pod.setup_log.read_text())
        (session.workdir / "harness-boundaries.json").write_text(json.dumps({
            "boundary":"fixture pod allocation, mounted storage/Runtime readiness, bootstrap/install/GPU and H3 model/action",
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


def held_control(session, pod, cli, transport_factory, *, command=None):
    mode = ["--release-held"] if command is None else ["--resume-held", command]
    args = ps.build_parser().parse_args([
        "--datacenters", ps.DATACENTER, "--sources-dir", str(pod.sources),
        "--workdir", str(session.workdir), "--token-dir", str(session.args.token_dir),
        "--poll-interval", ".01", *mode])
    messages = []
    control = ps.Session(args, cli=cli, transport_factory=transport_factory,
                         http_status=pod.http_status, sleep=lambda _seconds: None,
                         say=messages.append, agent_root=pod.agent_root)
    control.test_messages = messages
    return control


def failure_diagnostics(session):
    return "\n".join(message for message in session.test_messages
                     if message.startswith(("STOPPED:", "RELEASE FAILED", "RELEASE UNVERIFIED")))


@pytest.mark.parametrize("process_boundary", [False, True])
def test_single_row_hold_resume_and_verified_release_across_sessions(tmp_path, monkeypatch, process_boundary):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and len(pod.launches) == 1 and not pod.terminated, failure_diagnostics(session)
        held = session.workdir / "held-session.json"
        assert held.is_file() and held.stat().st_mode & 0o777 == 0o600
        initial = json.loads((session.workdir / "pod-session-progress.json").read_text())
        primary = next(r for r in initial["steps"] if r["step"] == "job:image" and r["result"] == "failed")
        assert "exited 7" in primary["detail"]["error"]
        assert json.loads((session.workdir / "packets/image/machine-check.json").read_text())["status"] == "failed"
        marker = tmp_path / "followup-ran"
        command = f"printf done > {shlex.quote(str(marker))}"
        resumed = held_control(session, pod, cli, transport_factory, command=command)
        assert resumed.run_held(command=command) == 1
        assert marker.read_text() == "done"
        assert len(pod.launches) == 1 and not pod.terminated, failure_diagnostics(resumed)
        assert resumed.sid == session.sid and resumed.pod_id == session.pod_id
        progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
        assert primary in progress["steps"]
        release = held_control(session, pod, cli, transport_factory)
        assert release.run_held(command=None) == 0
        assert pod.events.count("terminate") == 1 and pod.events[-1] == "list"
        assert "release-get" in pod.events and release.released and pod.terminated
        assert len(pod.launches) == 1
        assert pod.events.count("launch") == 1
        assert not list((tmp_path / "tokens").glob("*.token"))
        assert primary in json.loads((session.workdir / "pod-session-progress.json").read_text())["steps"]

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run, process_boundary=process_boundary)


def test_failed_followup_releases_and_retains_primary_failure(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated, failure_diagnostics(session)
        resumed = held_control(session, pod, cli, transport_factory, command="exit 9")
        assert resumed.run_held(command="exit 9") == 1
        assert_release_verified(resumed, pod)
        progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
        assert any(r["step"] == "job:image" and r["result"] == "failed"
                   and "exited 7" in r["detail"]["error"] for r in progress["steps"])
        assert len(pod.launches) == 1 and not (session.workdir / "held-session.json").exists()

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_success_in_hold_mode_still_releases(tmp_path, monkeypatch):
    session, pod, code, _messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, single=True, hold=True)
    assert code == 0, failure_diagnostics(session)
    assert_release_verified(session, pod)
    assert not (session.workdir / "held-session.json").exists()


def test_progress_status_replacement_transport_failure_releases(tmp_path, monkeypatch):
    from runpod_lifecycle.pod_agent import Handler

    replaced, rejected = [], []
    def configure(session, _pod):
        original_info, original_reply = Handler.download_info, Handler.reply
        def download_info(handler, stream):
            info = original_info(handler, stream)
            status = Path(stream.name)
            if (not replaced and status.name == "status" and status.parent.parent == Path(ps.JOB_ROOT)
                    and any(r["step"] == "install" and r["result"] == "ok" for r in session.records)):
                # Force the supervisor's atomic-publication race between the
                # handler's two fstats, even after the fixture barrier completes.
                staged = status.with_name("status.fixture-tmp")
                staged.write_bytes(status.read_bytes())
                staged.replace(status)
                replaced.append(status.parent.name)
            return info
        def reply(handler, status, data=None, **kwargs):
            if status == 412:
                rejected.append(data)
            return original_reply(handler, status, data, **kwargs)
        monkeypatch.setattr(Handler, "download_info", download_info)
        monkeypatch.setattr(Handler, "reply", reply)

    session, pod, code, _messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, single=True, hold=True, configure=configure)
    assert code == 1 and len(replaced) == 1
    assert rejected == [{"error": "file changed"}]
    failed = next(r for r in session.records if r["result"] == "failed")
    assert failed["step"] == "progress"
    assert failed["detail"]["error"] == f"job {replaced[0]}: poll failed (PodTransportError)"
    assert not any(r["step"] == "job:image" for r in session.records)
    assert_release_verified(session, pod)
    assert len(pod.launches) == 1 and not (session.workdir / "held-session.json").exists()


def test_held_explicit_release_requires_verified_termination(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        release = held_control(session, pod, cli, transport_factory)
        release.http_status = lambda _pod: 200
        assert release.run_held(command=None) == 1
        assert pod.terminated and not release.released
        assert (session.workdir / "held-session.json").exists()
        assert len(pod.launches) == 1

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


@pytest.mark.parametrize("fault", ["missing", "corrupt", "stale", "token", "progress", "source", "remote-source", "remote-manifest",
                                  "identity", "storage", "readiness", "transport"])
def test_held_resume_refuses_unusable_state_or_binding(tmp_path, monkeypatch, fault):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated, failure_diagnostics(session)
        held = session.workdir / "held-session.json"
        if fault == "missing":
            held.unlink()
        elif fault == "corrupt":
            held.write_text("{broken")
        elif fault == "stale":
            document = json.loads(held.read_text())
            document["expires_at"] = 1
            held.write_text(json.dumps(document))
        elif fault == "token":
            session.token_path.unlink()
        elif fault == "progress":
            (session.workdir / "pod-session-progress.json").write_text("{}")
        elif fault == "source":
            (session.workdir / "source-identity.json").write_text("{}")
        elif fault == "remote-source":
            (Path(ps.SRC_DIR) / "VibeComfy/fixture.txt").write_text("changed after hold")
        elif fault == "remote-manifest":
            remote_manifest = Path(ps.SRC_DIR) / ".pod-source.json"
            document = json.loads(remote_manifest.read_text())
            document["heads"]["VibeComfy"] = "0" * 40
            remote_manifest.write_text(json.dumps(document))

        def changed_cli(argv, env):
            if fault == "identity" and argv[0] == "list" and "--name-prefix" in argv:
                pod.events.append("list")
                return 0, json.dumps([{"id":"otherpod", "name":session.name}]), ""
            if fault == "storage" and argv[:2] == ["volumes", "ls"]:
                pod.events.append("volumes")
                return 0, json.dumps([{"id":"other-volume", "name":ps.STORAGE_NAME,
                                       "dataCenterId":ps.DATACENTER}]), ""
            return cli(argv, env)

        def changed_transport(pod_id, token):
            transport = transport_factory(pod_id, token)
            if fault == "transport":
                monkeypatch.setattr(transport, "wait_ready", lambda **_kw: (_ for _ in ()).throw(RuntimeError("disconnected")))
            return transport

        marker = tmp_path / "forbidden-followup"
        command = f"printf bad > {shlex.quote(str(marker))}"
        resumed = held_control(session, pod, changed_cli, changed_transport, command=command)
        if fault == "readiness":
            original = resumed.must
            def must(label, *args, **kwargs):
                output, handle = original(label, *args, **kwargs)
                if label == "Runtime readiness":
                    receipt = json.loads(output)
                    receipt["runtime_head"] = "0" * 40
                    return json.dumps(receipt), handle
                return output, handle
            monkeypatch.setattr(resumed, "must", must)
        assert resumed.run_held(command=command) == 1
        assert not marker.exists() and len(pod.launches) == 1
        if fault in ("missing", "corrupt", "identity"):
            assert not pod.terminated  # No verified identity to terminate safely.
        else:
            assert_release_verified(resumed, pod)

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_job_list_constructs_install_sanity_hook_without_preinstall_probe(tmp_path, monkeypatch):
    manifest, _document = make_manifest(tmp_path / "manifest")
    session = ps.JobListSession(Namespace(jobs=str(manifest), workdir=str(tmp_path / "session")),
                                cli=lambda *_args: pytest.fail("unexpected provider call"),
                                transport_factory=lambda *_args: pytest.fail("unexpected transport"),
                                http_status=lambda _pod: pytest.fail("unexpected provider call"))
    commands = []
    session.pod_id = "fixturepod"
    session.state["storage"] = {"volume_id":"fixture-volume"}
    session._cache_binding = {"cache_device":47, "cache_mount_id":102}
    manifest_identity = {"heads":{"astrid-runtime":"a" * 40}, "runtime_lock_sha256":"b" * 64}
    (session.workdir / "source-identity.json").write_text(json.dumps(manifest_identity))

    def capture(label, command, timeout, **kwargs):
        commands.append((label, command))
        if label == "Runtime readiness":
            request = json.loads(base64.b64decode(shlex.split(command)[-1]))
            assert request["runtime_head"] == manifest_identity["heads"]["astrid-runtime"]
            assert request["runtime_lock_sha256"] == manifest_identity["runtime_lock_sha256"]
            assert request["pod_id"] == session.pod_id and request["volume_id"] == "fixture-volume"
            return json.dumps({**request, "ready":True, "stopped":True}), {"id":label}
        return "", {"id": label}

    monkeypatch.setattr(session, "must", capture)
    steps = session.run_steps()
    assert [name for name, _operation in steps][:5] == ["claim", "bundle", "bootstrap", "install", "rerun"]
    assert "sanity" not in dict(steps)
    dict(steps)["bootstrap"]()
    detail = dict(steps)["install"]()
    assert [label for label, _command in commands] == ["bootstrap", "Runtime readiness", "install"]
    assert "gpu_sanity.py" not in commands[0][1]
    assert session.state["runtime_ready"]["ready"] is True
    install = commands[2][1].splitlines()
    assert install == ["set -eu", "export PATH=/opt/astrid/bin:$PATH",
                       f"export ASTRID_ENGINE_SANITY_CMD={shlex.quote(ps.SANITY_CMD)}",
                       f"astrid engines install {ps.ENGINE} --placement local --root {ps.ENGINE_ROOT} --deadline 14400 --model-cache-root {shlex.quote(ps.MODEL_CACHE_ROOT)} --model-cache-device 47 --model-cache-mount-id 102"]
    assert detail["sanity_cmd"] == ps.SANITY_CMD


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
    steps = ["claim", "bundle", "bootstrap", "install", "rerun", "h3:clip:s0",
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
    assert setup.index("storage") < setup.index("bootstrap") < setup.index("runtime-ready") < setup.index("install") < setup.index("sanity") < setup.index("models") < setup.index("h3:1") < setup.index("h3:9")
    assert session.state["storage"]["volume_id"] == session.state["runtime_ready"]["volume_id"] == "fixture-volume"
    assert setup.count("sanity") == 2  # Initial install and its idempotency rerun both keep the hook.
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


@pytest.mark.parametrize("failure", ["png", "json", "hash", "command", "sanity", "runtime"])
def test_failed_two_job_rehearsal_retains_diagnostic_and_releases(tmp_path, monkeypatch, failure):
    session, pod, code, messages, _manifest, _document = run_jobs(tmp_path, monkeypatch, mixed=False, failure=failure)
    assert code == 1, "\n".join(messages)
    assert_release_verified(session, pod)
    assert session.budget.count == pod.record_count < 40
    failed = [r for r in session.records if r["result"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["step"] == ("bootstrap" if failure == "runtime" else "install" if failure == "sanity" else "job:image" if failure == "command" else "download:image")
    progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
    assert progress["steps"][-1]["step"] == "release"
    assert any("POD SESSION FAILED" in message for message in messages)
    if failure == "sanity":
        assert pod.setup_log.read_text().splitlines() == ["storage", "bootstrap", "runtime-ready", "install", "sanity"]
        assert [r["step"] for r in session.records] == ["claim", "bundle", "bootstrap", "install", "release"]
        assert "exited 1" in failed[0]["detail"]["error"]
        assert not pod.comfy_prompts
    elif failure == "runtime":
        assert pod.setup_log.read_text().splitlines() == ["storage", "bootstrap", "runtime-ready"]
        assert [record["step"] for record in session.records] == ["claim", "bundle", "bootstrap", "release"]
        assert "identity/readiness" in failed[0]["detail"]["error"]
        assert not pod.comfy_prompts and "runtime_ready" not in session.state
    else:
        check = json.loads((session.workdir / "packets/image/machine-check.json").read_text())
        assert check["status"] == "failed"
        assert "failed" in (session.workdir / "packets/image/README.md").read_text().lower()
        assert [r["step"] for r in session.records if r["step"].endswith(":later")] == [
            "job:later", "download:later", "packet:later"]
        assert json.loads((session.workdir / "packets/later/machine-check.json").read_text())["status"] == "passed"


def test_recoverable_row_failure_keeps_pod_for_next_row_and_records_failure(tmp_path, monkeypatch):
    session, pod, code, messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, failure="command")
    assert code == 1, "\n".join(messages)
    assert pod.launches and len(pod.launches) == 1
    assert pod.events.count("terminate") == 1
    steps = [record["step"] for record in session.records]
    assert steps.index("job:image") < steps.index("job:later") < steps.index("release")
    failed = next(record for record in session.records if record["step"] == "job:image")
    assert failed["result"] == "failed" and "exited 7" in failed["detail"]["error"]
    progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
    retained = next(record for record in progress["steps"] if record["step"] == "job:image")
    assert retained["result"] == "failed" and "exited 7" in retained["detail"]["error"]
    assert json.loads((session.workdir / "packets/image/machine-check.json").read_text())["status"] == "failed"
    assert json.loads((session.workdir / "packets/later/machine-check.json").read_text())["status"] == "passed"


def test_unusable_setup_still_releases_and_preserves_primary_failure(tmp_path, monkeypatch):
    session, pod, code, _messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, failure="runtime")
    assert code == 1
    assert_release_verified(session, pod)
    failed = next(record for record in session.records if record["result"] == "failed")
    assert failed["step"] == "bootstrap" and "identity/readiness" in failed["detail"]["error"]
    assert not any(record["step"] in ("job:image", "job:later") for record in session.records)


def test_row_transport_failure_stops_before_later_job_and_releases(tmp_path, monkeypatch):
    session, pod, code, _messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, fail_row_transport=True)
    assert code == 1
    assert_release_verified(session, pod)
    failed = next(record for record in session.records if record["result"] == "failed")
    assert failed["step"] == "job:image" and "transport disconnected" in failed["detail"]["error"]
    assert not any(record["step"] in ("job:later", "download:later", "packet:later")
                   for record in session.records)


@pytest.mark.parametrize("fault", ["progress", "packet", "local-output", "held-state", "transport", "runtime"])
def test_hold_mode_fails_closed_on_nonrecoverable_failure(tmp_path, monkeypatch, fault):
    def configure(session, _pod):
        if fault == "progress":
            original = session.write_progress
            def write_progress(*, strict):
                if strict and any(r["step"] == "job:image" and r["result"] == "failed" for r in session.records):
                    raise OSError("fixture progress write failed")
                return original(strict=strict)
            monkeypatch.setattr(session, "write_progress", write_progress)
        elif fault == "packet":
            original = session.packet_check
            def packet_check(job, status, **detail):
                if status == "failed":
                    raise OSError("fixture packet write failed")
                return original(job, status, **detail)
            monkeypatch.setattr(session, "packet_check", packet_check)
        elif fault == "local-output":
            original = Path.write_text
            def write_text(path, *args, **kwargs):
                if path.name == "job.stdout":
                    raise OSError("fixture local output write failed")
                return original(path, *args, **kwargs)
            monkeypatch.setattr(Path, "write_text", write_text)
        elif fault == "held-state":
            def fail_state(*_args, **_kwargs):
                raise OSError("fixture held state write failed")
            monkeypatch.setattr(session, "_write_held", fail_state)

    session, pod, code, _messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, single=True, hold=True,
        failure="runtime" if fault == "runtime" else "command",
        fail_row_transport=fault == "transport", configure=configure)
    assert code == 1
    assert_release_verified(session, pod)
    assert not (session.workdir / "held-session.json").exists()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_transient_row_log_fetch_failure_cannot_hold(tmp_path, monkeypatch, stream):
    failed_fetches = []
    def configure(session, _pod):
        original_run, original_fetch = session.run_job, session.fetch
        in_row = False
        def run_job(label, *args, **kwargs):
            nonlocal in_row
            in_row = label == "job:image"
            try:
                return original_run(label, *args, **kwargs)
            finally:
                in_row = False
        def fetch(remote):
            if in_row and remote.endswith("/" + stream) and not failed_fetches:
                failed_fetches.append(remote)
                raise RuntimeError("fixture transient log transport failure")
            return original_fetch(remote)
        monkeypatch.setattr(session, "run_job", run_job)
        monkeypatch.setattr(session, "fetch", fetch)

    session, pod, code, _messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, single=True, hold=True,
        failure="command", configure=configure)
    assert len(failed_fetches) == 1 and code == 1
    assert_release_verified(session, pod)
    assert not (session.workdir / "held-session.json").exists()


@pytest.mark.parametrize("phase", ["row", "followup"])
def test_transient_detached_poll_failure_releases_session(tmp_path, monkeypatch, phase):
    injected = []
    def inject(session, label_to_fail):
        original_run = session.run_job
        def run_job(label, *args, **kwargs):
            if label != label_to_fail:
                return original_run(label, *args, **kwargs)
            original_poll = session.transport.detached_poll
            def poll(job_id, *, root):
                if not injected:
                    injected.append(job_id)
                    raise RuntimeError("fixture transient polling failure")
                return original_poll(job_id, root=root)
            monkeypatch.setattr(session.transport, "detached_poll", poll)
            try:
                return original_run(label, *args, **kwargs)
            finally:
                monkeypatch.setattr(session.transport, "detached_poll", original_poll)
        monkeypatch.setattr(session, "run_job", run_job)

    def configure(session, _pod):
        if phase == "row":
            inject(session, "job:image")

    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1
        if phase == "row":
            assert_release_verified(session, pod)
        else:
            assert not pod.terminated
            resumed = held_control(session, pod, cli, transport_factory, command="printf followup")
            inject(resumed, "follow-up")
            assert resumed.run_held(command="printf followup") == 1
            assert_release_verified(resumed, pod)
            progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
            assert any(r["step"] == "job:image" and r["result"] == "failed" for r in progress["steps"])
        assert len(injected) == 1 and len(pod.launches) == 1
        assert not (session.workdir / "held-session.json").exists()

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", configure=configure, after_run=after_run)


def test_transient_upload_failure_cannot_hold(tmp_path, monkeypatch):
    injected = []
    def configure(session, _pod):
        original_put = session.put
        def put(local, remote):
            original_transport_put = session.transport.put
            def transport_put(*args, **kwargs):
                if not injected:
                    injected.append(True)
                    raise RuntimeError("fixture transient upload failure")
                return original_transport_put(*args, **kwargs)
            monkeypatch.setattr(session.transport, "put", transport_put)
            try:
                return original_put(local, remote)
            finally:
                monkeypatch.setattr(session.transport, "put", original_transport_put)
        monkeypatch.setattr(session, "put", put)

    session, pod, code, _messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, single=True, hold=True,
        failure="command", configure=configure)
    assert injected == [True] and code == 1
    assert_release_verified(session, pod)
    assert not (session.workdir / "held-session.json").exists()


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
    assert pod.events.count("terminate") == 1 and pod.events.count("list") == 13
    assert pod.events.index("list") < pod.events.index("terminate")
    assert pod.events[pod.events.index("terminate") + 1:].count("list") == 12
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


def test_job_list_missing_storage_fails_without_fallback(tmp_path, monkeypatch):
    session, pod, code, _messages, _manifest, _document = run_jobs(tmp_path, monkeypatch, missing_storage=True)
    assert code == 1 and len(pod.launches) == 1
    assert session.state["claim"]["state"] == "launch_failed"
    assert [r["step"] for r in session.records] == ["claim", "release"]
    assert session.records[-1]["result"] == "failed"
    assert session.records[-1]["detail"]["status"] == "unresolved"
    assert not session.released
    assert not pod.requests and not pod.terminated and not pod.setup_log.exists()
    assert session.budget.count == 0
