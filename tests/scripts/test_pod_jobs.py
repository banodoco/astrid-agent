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
             configure=None, after_run=None, process_boundary=False, expected_storage_volume_id=None,
             observed_storage_volume_id="fixture-volume"):
    pod = LoopbackPod(tmp_path / "pod", ps, monkeypatch, sanity_fail=failure == "sanity",
                      runtime_fail=failure == "runtime",
                      corrupt_download="generated.png" if failure == "hash" else None,
                      volume_id=observed_storage_volume_id)
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
            + (["--storage-volume-id", expected_storage_volume_id] if expected_storage_volume_id is not None else [])
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


def held_control(session, pod, cli, transport_factory, *, command=None, renew=False, local_free_bytes=None,
                 jobs=None):
    mode = (["--renew-held"] if renew else
            ["--resume-held-jobs", str(jobs)] if jobs is not None else
            ["--release-held"] if command is None else ["--resume-held", command])
    args = ps.build_parser().parse_args([
        "--datacenters", ps.DATACENTER, "--sources-dir", str(pod.sources),
        "--workdir", str(session.workdir), "--token-dir", str(session.args.token_dir),
        *(["--storage-volume-id", "fixture-volume"] if renew else []),
        "--poll-interval", ".01", *mode])
    messages = []
    def held_transport(pod_id, token):
        transport = transport_factory(pod_id, token)
        if not getattr(transport, "_held_storage_fixture", False):
            original_start = transport.detached_start
            def detached_start(command, *, root, job_id=None, launch_timeout=60):
                # Held probes now use the same durable detached operation as
                # follow-ups. Preserve the loopback's mount facts at that entry
                # point while retaining real AgentServer execution and counts.
                parts = shlex.split(command)
                if len(parts) == 4 and parts[:2] == ["python3", "-c"] and "def storage_probe(" in parts[2]:
                    request = json.loads(base64.b64decode(parts[3]))
                    assert request["pod_id"] == pod_id and request["volume_id"] == pod.volume_id
                    assert request["mount"] == ps.CACHE_MOUNT
                    assert request["model_cache_root"] == ps.MODEL_CACHE_ROOT
                    assert request["engine_root"] == ps.ENGINE_ROOT
                    pod.events.append("storage-probe")
                    receipt = {**{key:value for key,value in request.items() if key != "provider_mapping"},
                               "cache_device":47, "cache_mount_id":102,
                               "free_bytes":request["required_volume_bytes"] + 1,
                               "total_bytes":request["required_volume_bytes"] + 2,
                               "local_free_bytes":(request["required_local_bytes"] + 1
                                                   if local_free_bytes is None else local_free_bytes)}
                    source = ('import os,pathlib\n'
                              'with pathlib.Path(os.environ["POD_JOBS_SETUP_LOG"]).open("a") as log: log.write("storage\\n")\n'
                              + f'print({json.dumps(receipt)!r})\n')
                    command = "python3 -c " + shlex.quote(source)
                return original_start(command, root=root, job_id=job_id, launch_timeout=launch_timeout)
            transport.detached_start = detached_start
            transport._held_storage_fixture = True
        return transport

    control_type = ps.JobListSession if jobs is not None else ps.Session
    control = control_type(args, cli=cli, transport_factory=held_transport,
                         http_status=pod.http_status, sleep=lambda _seconds: None,
                         say=messages.append, agent_root=pod.agent_root)
    control.test_messages = messages
    return control


def failure_diagnostics(session):
    return "\n".join(message for message in session.test_messages
                     if message.startswith(("STOPPED:", "RELEASE FAILED", "RELEASE UNVERIFIED")))


def _packet_snapshot(root):
    packets = root / "packets"
    return {str(path.relative_to(packets)): path.read_bytes()
            for path in packets.rglob("*") if path.is_file()}


def test_cold_claim_keeps_recipe_storage_reserve(tmp_path, monkeypatch):
    requests = []
    def configure(session, _pod):
        original_execute = session.execute
        def execute(label, command, *args, **kwargs):
            if label == "storage mount verification":
                requests.append(json.loads(base64.b64decode(shlex.split(command)[3])))
            return original_execute(label, command, *args, **kwargs)
        monkeypatch.setattr(session, "execute", execute)

    session, pod, code, messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, single=True, configure=configure)
    assert code == 0, "\n".join(messages)
    assert len(requests) == 1
    assert requests[0]["required_local_bytes"] == (ps.recipe_footprint_gb() + 20) * 10**9 == 84 * 10**9
    assert session.state["storage"]["required_local_bytes"] == requests[0]["required_local_bytes"]
    assert_release_verified(session, pod)


@pytest.mark.parametrize("local_free_bytes", [20 * 10**9, 59_422_531_584, 20 * 10**9 - 1],
                         ids=["exact-20gb", "installed-pod-headroom", "insufficient-20gb"])
def test_held_installed_followup_uses_warm_scratch(tmp_path, monkeypatch, local_free_bytes):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        before = json.loads(held_path.read_text())
        packets_before = _packet_snapshot(session.workdir)
        progress_path = session.workdir / "pod-session-progress.json"
        progress_before = progress_path.read_bytes()
        assert [(r["step"], r["result"]) for r in json.loads(progress_before)["steps"]
                if r["step"] in ("install", "rerun")] == [("install", "ok"), ("rerun", "ok")]
        assert session.state["storage"]["required_local_bytes"] == 84 * 10**9
        setup_before = pod.setup_log.read_text().splitlines()
        records_before = pod.record_count
        marker = tmp_path / "warm-followup"
        command = f"printf done >> {shlex.quote(str(marker))}"
        control = held_control(session, pod, cli, transport_factory, command=command,
                               local_free_bytes=local_free_bytes)
        assert control.run_held(command=command) == 1
        held = json.loads(held_path.read_text())
        if local_free_bytes >= 20 * 10**9:
            assert marker.read_text() == "done"
            assert control.state["storage"]["required_local_bytes"] == 20 * 10**9
            assert held["status"] == "held"
            assert sum(r["step"] == "follow-up" for r in json.loads(progress_path.read_text())["steps"]) == 1
        else:
            assert not marker.exists()
            assert "storage evidence differs" in failure_diagnostics(control)
            assert held["status"] == "in_use"
            assert held["operation"]["label"] == "storage mount verification"
            assert held["operation"]["state"] == "exited" and held["operation"]["exit_code"] == 0
            assert pod.record_count == records_before + 1
            assert progress_path.read_bytes() == progress_before
            charged = held["charged_jobs"]
            reconcile = held_control(session, pod, cli, transport_factory, command=command)
            assert reconcile.run_held(command=command) == 1
            held = json.loads(held_path.read_text())
            assert held["status"] == "held" and held["charged_jobs"] == charged == pod.record_count
            assert held["jobs"] == before["jobs"] and held["progress_sha256"] == before["progress_sha256"]
            assert not marker.exists() and progress_path.read_bytes() == progress_before
        assert held["charged_jobs"] == control.budget.count == pod.record_count
        assert held["charged_jobs"] > before.get("charged_jobs", before["jobs"])
        assert (control.sid, control.pod_id) == (session.sid, session.pod_id)
        setup_after = pod.setup_log.read_text().splitlines()
        assert setup_after.count("install") == setup_before.count("install")
        assert setup_after.count("sanity") == setup_before.count("sanity")
        assert _packet_snapshot(session.workdir) == packets_before
        assert len(pod.launches) == pod.events.count("launch") == 1
        assert not pod.terminated and pod.events.count("terminate") == 0

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


@pytest.mark.parametrize("step", ["install", "rerun"])
@pytest.mark.parametrize("fault", ["missing", "failed", "duplicate", "conflicting", "unbound"])
def test_held_warm_scratch_requires_digest_bound_success(tmp_path, monkeypatch, step, fault):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        held = json.loads(held_path.read_text())
        progress_path = session.workdir / "pod-session-progress.json"
        progress = json.loads(progress_path.read_text())
        row = next(r for r in progress["steps"] if r["step"] == step)
        if fault == "missing":
            progress["steps"].remove(row)
        elif fault in ("failed", "unbound"):
            row["result"] = "failed"
        else:
            progress["steps"].append(dict(row, result="failed" if fault == "conflicting" else "ok"))
        progress_path.write_text(json.dumps(progress))
        if fault != "unbound":
            held["progress_sha256"] = hashlib.sha256(progress_path.read_bytes()).hexdigest()
            session._write_held(held)
        held_before, progress_before = held_path.read_bytes(), progress_path.read_bytes()
        records_before = pod.record_count
        marker = tmp_path / "forbidden-warm-followup"
        command = f"printf bad > {shlex.quote(str(marker))}"
        control = held_control(session, pod, cli, transport_factory, command=command)
        assert control.run_held(command=command) == 1
        assert ("progress differs" if fault == "unbound" else "successful install and rerun evidence") in failure_diagnostics(control)
        assert held_path.read_bytes() == held_before and progress_path.read_bytes() == progress_before
        assert pod.record_count == records_before and not marker.exists()
        assert len(pod.launches) == pod.events.count("launch") == 1
        assert not pod.terminated and pod.events.count("terminate") == 0

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_held_renewal_changes_only_expiry_and_preserves_failure_evidence(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        before = json.loads(held_path.read_text())
        progress_before = (session.workdir / "pod-session-progress.json").read_bytes()
        packets_before = _packet_snapshot(session.workdir)
        launches_before, events_before = list(pod.launches), list(pod.events)
        control = held_control(session, pod, cli, transport_factory, renew=True)
        assert control.renew_held() == 0, failure_diagnostics(control)
        after = json.loads(held_path.read_text())
        assert after["expires_at"] > before["expires_at"]
        assert {**after, "expires_at": before["expires_at"]} == before
        assert (session.workdir / "pod-session-progress.json").read_bytes() == progress_before
        assert _packet_snapshot(session.workdir) == packets_before
        assert pod.launches == launches_before and pod.events == events_before and not pod.terminated
        assert any("original row failure remains" in message for message in control.test_messages)

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


@pytest.mark.parametrize("case", ["expired", "in_use", "lock_busy"])
def test_held_renewal_rejects_expired_in_use_or_concurrent_session(tmp_path, monkeypatch, case):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1
        held_path = session.workdir / "held-session.json"
        before = held_path.read_bytes()
        lockfd = None
        if case == "expired":
            doc = json.loads(before)
            doc["expires_at"] = 1
            session._write_held(doc)
            before = held_path.read_bytes()
        elif case == "in_use":
            doc = json.loads(before)
            doc["status"] = "in_use"
            session._write_held(doc)
            before = held_path.read_bytes()
        else:
            import fcntl
            lockfd = os.open(session.workdir / ".held-session.lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        events_before, launches_before = list(pod.events), list(pod.launches)
        control = held_control(session, pod, cli, transport_factory, renew=True)
        try:
            assert control.renew_held() == 1
        finally:
            if lockfd is not None:
                fcntl.flock(lockfd, fcntl.LOCK_UN)
                os.close(lockfd)
        assert held_path.read_bytes() == before
        assert pod.events == events_before and pod.launches == launches_before and not pod.terminated

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


@pytest.mark.parametrize("case", ["session", "pod", "source", "runtime_lock", "volume", "progress", "token_dir", "token_file"])
def test_held_renewal_rejects_mismatched_identity_without_changing_custody(tmp_path, monkeypatch, case):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1
        held_path = session.workdir / "held-session.json"
        doc = json.loads(held_path.read_text())
        if case == "session":
            doc["session"] = "deadbeef"
            session._write_held(doc)
        elif case == "pod":
            doc["pod_id"] = "fakepod135"
            session._write_held(doc)
        elif case == "source":
            source = json.loads((session.workdir / "source-identity.json").read_text())
            source["heads"]["astrid-runtime"] = "a" * 40
            (session.workdir / "source-identity.json").write_text(json.dumps(source))
        elif case == "runtime_lock":
            (session.agent_root / "runtime.lock.json").write_text('{"source_commit":"wrong"}')
        elif case == "volume":
            doc["volume_id"] = "other-volume"
            session._write_held(doc)
        elif case == "progress":
            (session.workdir / "pod-session-progress.json").write_text("{}")
        elif case == "token_file":
            session.token_path.write_text("invalid-token\n")
        before = held_path.read_bytes()
        progress_before = (session.workdir / "pod-session-progress.json").read_bytes()
        events_before, launches_before = list(pod.events), list(pod.launches)
        control = held_control(session, pod, cli, transport_factory, renew=True)
        if case == "token_dir":
            control.args.token_dir = str(session.workdir / "different-tokens")
        assert control.renew_held() == 1
        assert held_path.read_bytes() == before
        assert (session.workdir / "pod-session-progress.json").read_bytes() == progress_before
        assert pod.events == events_before and pod.launches == launches_before and not pod.terminated

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_held_renewal_atomic_write_failure_keeps_custody_without_release(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1
        held_path = session.workdir / "held-session.json"
        before = held_path.read_bytes()
        events_before, launches_before = list(pod.events), list(pod.launches)
        control = held_control(session, pod, cli, transport_factory, renew=True)
        def fail_write(_doc, *, create=False):
            assert not create
            raise OSError("fixture atomic write interruption")
        monkeypatch.setattr(control, "_write_held", fail_write)
        assert control.renew_held() == 1
        assert held_path.read_bytes() == before
        assert json.loads(held_path.read_text())["status"] == "held"
        assert pod.events == events_before and pod.launches == launches_before and not pod.terminated

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


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
        packet_before = _packet_snapshot(session.workdir)
        marker = tmp_path / "followup-ran"
        command = f"printf done > {shlex.quote(str(marker))}"
        resumed = held_control(session, pod, cli, transport_factory, command=command)
        assert resumed.run_held(command=command) == 1
        assert marker.read_text() == "done"
        assert len(pod.launches) == 1 and not pod.terminated, failure_diagnostics(resumed)
        assert resumed.sid == session.sid and resumed.pod_id == session.pod_id
        assert json.loads(held.read_text())["status"] == "held"
        assert _packet_snapshot(session.workdir) == packet_before
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


def _held_joblist_manifest(root, *, failing=None):
    manifest, document = make_manifest(root, mixed=True)
    cmd = dict(document["jobs"][1])
    cmd["id"] = "next-image"
    if failing == "command":
        cmd["command"] = "exit 9"
    elif failing == "verification":
        (Path(root) / "generate.py").write_text(
            "import pathlib; pathlib.Path('generated.png').write_bytes(pathlib.Path('data/reference.png').read_bytes()); "
            "pathlib.Path('result.json').write_text('{invalid json')\n")
    later = dict(document["jobs"][1])
    later.update(id="later", command=[sys.executable, "-c",
        "import json,pathlib; pathlib.Path('later.png').write_bytes(pathlib.Path('data/reference.png').read_bytes()); "
        "pathlib.Path('result.json').write_text(json.dumps({'ok': True}))"],
        **{"expected-outputs":[{"path":"later.png", "kind":"png", "width":32, "height":24}, "result.json"]})
    document["jobs"] = [document["jobs"][0], cmd, later]
    manifest.write_text(yaml.safe_dump(document, sort_keys=False))
    return manifest


@pytest.mark.parametrize("process_boundary", [False, True])
def test_held_joblist_runs_fresh_mixed_rows_and_keeps_custody(tmp_path, monkeypatch, process_boundary):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated, failure_diagnostics(session)
        original_failure = next(r for r in json.loads(
            (session.workdir / "pod-session-progress.json").read_text())["steps"]
            if r["step"] == "job:image" and r["result"] == "failed")
        held_path = session.workdir / "held-session.json"
        held_before = json.loads(held_path.read_text())
        original_packet = _packet_snapshot(session.workdir)
        original_count = len(json.loads((session.workdir / "pod-session-progress.json").read_text())["steps"])
        setup_before = pod.setup_log.read_text().splitlines()
        manifest = _held_joblist_manifest(tmp_path / "resume-list")
        before_events = list(pod.events)
        before_count = pod.record_count
        control = held_control(session, pod, cli, transport_factory, jobs=manifest)
        assert control.run_held_jobs() == 0, failure_diagnostics(control)
        assert control.sid == session.sid and control.pod_id == session.pod_id
        assert pod.events.count("launch") == 1 and pod.record_count > before_count
        assert not pod.terminated and json.loads((session.workdir / "held-session.json").read_text())["status"] == "held"
        assert not any(event in pod.events[len(before_events):] for event in ("terminate", "launch"))
        progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
        assert original_failure in progress["steps"]
        resumed_steps = [r["step"] for r in progress["steps"][original_count:]]
        assert resumed_steps == ["upload:clip", "h3:clip:s0", "job:clip", "download:clip", "packet:clip",
                                 "upload:next-image", "job:next-image", "download:next-image", "packet:next-image",
                                 "upload:later", "job:later", "download:later", "packet:later"]
        assert not any(r["step"] in ("claim", "bundle", "bootstrap", "install", "rerun", "release")
                       for r in progress["steps"][original_count:])
        remote = json.loads((session.workdir / "pod-session-progress.remote.json").read_text())
        assert remote == progress
        assert json.loads((session.workdir / "held-session-joblist-progress.json").read_text()) == progress
        held_after = json.loads(held_path.read_text())
        assert held_after["jobs"] == held_after["charged_jobs"] == control.budget.count
        assert pod.record_count - before_count == control.budget.count - held_before["jobs"]
        for key in ("session", "pod_id", "volume_id", "volume_gb", "source", "runtime_identity", "expires_at"):
            assert held_after[key] == held_before[key]
        assert held_after["progress_sha256"] == hashlib.sha256(
            (session.workdir / "pod-session-progress.json").read_bytes()).hexdigest()
        packets = session.workdir / "packets"
        assert {p.name for p in packets.iterdir()} == {"image", "clip", "next-image", "later"}
        final_packets = _packet_snapshot(session.workdir)
        assert {k: v for k, v in final_packets.items() if k.startswith("image/")} == {
            k: v for k, v in original_packet.items() if k.startswith("image/")}
        for row in ("clip", "next-image", "later"):
            check = json.loads((packets / row / "machine-check.json").read_text())
            assert check["status"] == "passed"
            for relative, evidence in check["outputs"].items():
                output = packets / row / "outputs" / relative
                assert output.is_file()
                assert evidence["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
        clip_outputs = packets / "clip/outputs"
        assert {"final.mp4", "receipt.json", "composition-manifest.json"} <= {
            path.name for path in clip_outputs.iterdir()}
        assert ps.publication_verified(json.loads((clip_outputs / "receipt.json").read_text()))
        prompts = [int(prompt["prompt"]["1"]["inputs"]["seconds"]) for prompt in pod.comfy_prompts]
        assert prompts == [1, 9]
        setup_after = pod.setup_log.read_text().splitlines()
        assert setup_after.count("install") == setup_before.count("install")
        assert setup_after.count("sanity") == setup_before.count("sanity")

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run, process_boundary=process_boundary)


@pytest.mark.parametrize("failure,failed_step", [("command", "job:next-image"),
                                                  ("verification", "download:next-image")])
def test_held_joblist_terminal_row_failure_keeps_original_failure_and_snapshot(
        tmp_path, monkeypatch, failure, failed_step):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        progress_path = session.workdir / "pod-session-progress.json"
        original = progress_path.read_bytes()
        primary = next(r for r in json.loads(original)["steps"] if r["step"] == "job:image")
        manifest = _held_joblist_manifest(tmp_path / "resume-list-fail", failing=failure)
        control = held_control(session, pod, cli, transport_factory, jobs=manifest)
        assert control.run_held_jobs() == 1
        held = json.loads((session.workdir / "held-session.json").read_text())
        assert held["status"] == "held"
        progress = json.loads(progress_path.read_text())
        assert primary in progress["steps"]
        assert any(r["step"] == failed_step and r["result"] == "failed" for r in progress["steps"])
        assert (session.workdir / "held-session-joblist-progress.json").is_file()
        failures = (session.workdir / "held-session-followup-failures.jsonl").read_text().splitlines()
        assert json.loads(failures[-1])["retryable"] is True
        assert not (session.workdir / "packets/later").exists()
        assert json.loads((session.workdir / "packets/clip/machine-check.json").read_text())["status"] == "passed"
        assert pod.events.count("launch") == 1 and pod.events.count("terminate") == 0

        evidence = {name: (session.workdir / name).read_bytes() for name in (
            "pod-session-progress.json", "pod-session-progress.remote.json", "source-identity.json",
            "held-session-joblist-progress.json", "held-session-followup-failures.jsonl")}
        packets = _packet_snapshot(session.workdir)
        events, records = list(pod.events), pod.record_count
        renewal = held_control(session, pod, cli, transport_factory, renew=True)
        assert renewal.renew_held() == 0, failure_diagnostics(renewal)
        renewed = json.loads((session.workdir / "held-session.json").read_text())
        assert renewed["expires_at"] > held["expires_at"]
        assert dict(renewed, expires_at=held["expires_at"]) == held
        assert all((session.workdir / name).read_bytes() == data for name, data in evidence.items())
        assert _packet_snapshot(session.workdir) == packets
        assert pod.events == events and pod.record_count == records and not pod.terminated

        # A later ordinary command failure keeps its separate durable charge;
        # the earlier list failure remains renewable without replay or refund.
        followup = held_control(session, pod, cli, transport_factory, command="exit 11")
        assert followup.run_held(command="exit 11") == 1
        after_command = json.loads((session.workdir / "held-session.json").read_text())
        assert after_command["charged_jobs"] > renewed["charged_jobs"]
        assert after_command["jobs"] == renewed["jobs"]
        assert (session.workdir / "pod-session-progress.json").read_bytes() == evidence["pod-session-progress.json"]
        renewal = held_control(session, pod, cli, transport_factory, renew=True)
        events, records = list(pod.events), pod.record_count
        assert renewal.renew_held() == 0, failure_diagnostics(renewal)
        after = json.loads((session.workdir / "held-session.json").read_text())
        assert dict(after, expires_at=after_command["expires_at"]) == after_command
        assert pod.events == events and pod.record_count == records and not pod.terminated

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_held_joblist_uncertain_row_keeps_in_use_operation_and_snapshot(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        manifest = _held_joblist_manifest(tmp_path / "resume-list-uncertain")
        original_transport = transport_factory
        def uncertain_transport(pod_id, token):
            transport = original_transport(pod_id, token)
            start = transport.detached_start
            def detached_start(command, *, root, job_id=None, launch_timeout=60):
                if ("cd " in command and f"{ps.CLIP_DIR}/jobs/" in command
                        and "next-image" in command):
                    # The reservation is durable before this start transport
                    # raises, so the accepted/uncertain slot must not be reused.
                    raise TimeoutError("fixture lost row start response")
                return start(command, root=root, job_id=job_id, launch_timeout=launch_timeout)
            transport.detached_start = detached_start
            return transport
        control = held_control(session, pod, cli, uncertain_transport, jobs=manifest)
        assert control.run_held_jobs() == 1
        held = json.loads((session.workdir / "held-session.json").read_text())
        assert held["status"] == "in_use"
        assert held["operation"]["state"] == "starting"
        assert held["operation"]["label"] == "job:next-image"
        assert held["charged_jobs"] == control.budget.count == pod.record_count + 1
        assert (session.workdir / "held-session-joblist-progress.json").is_file()
        assert not (session.workdir / "packets/later").exists()
        assert pod.events.count("launch") == 1 and pod.events.count("terminate") == 0

        held_bytes = (session.workdir / "held-session.json").read_bytes()
        events, records = list(pod.events), pod.record_count
        renewal = held_control(session, pod, cli, transport_factory, renew=True)
        assert renewal.renew_held() == 1
        assert (session.workdir / "held-session.json").read_bytes() == held_bytes
        assert pod.events == events and pod.record_count == records and not pod.terminated

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_held_joblist_uncertain_failure_requires_reconcile_before_renewal(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1
        held_path = session.workdir / "held-session.json"
        original_progress = (session.workdir / "pod-session-progress.json").read_bytes()
        manifest = _held_joblist_manifest(tmp_path / "resume-list-lost-response", failing="command")
        transport = pod.transport
        start, poll = transport.detached_start, transport.detached_poll
        target, uncertain = {}, [True]
        def lost_response(command, *, root, job_id=None, launch_timeout=60):
            job = start(command, root=root, job_id=job_id, launch_timeout=launch_timeout)
            if "next-image" in command and "cd " in command:
                target["id"] = job_id
                raise TimeoutError("fixture lost failed list row response")
            return job
        def delayed_poll(job_id, *, root):
            if uncertain[0] and job_id == target.get("id"):
                return {"id": job_id, "dir": root + "/" + job_id, "state": "running", "exit_code": None}
            return poll(job_id, root=root)
        monkeypatch.setattr(transport, "detached_start", lost_response)
        monkeypatch.setattr(transport, "detached_poll", delayed_poll)
        control = held_control(session, pod, cli, transport_factory, jobs=manifest)
        assert control.run_held_jobs() == 1
        held = json.loads(held_path.read_text())
        assert held["status"] == "in_use" and held["operation"]["state"] == "starting"
        renewal = held_control(session, pod, cli, transport_factory, renew=True)
        assert renewal.renew_held() == 1

        uncertain[0] = False
        records = pod.record_count
        reconcile = held_control(session, pod, cli, transport_factory, command="exit 99")
        assert reconcile.run_held(command="exit 99") == 1
        reconciled = json.loads(held_path.read_text())
        assert reconciled["status"] == "held"
        assert reconciled["operation"] == dict(held["operation"], state="exited", exit_code=9)
        assert reconciled["charged_jobs"] == held["charged_jobs"]
        assert (session.workdir / "pod-session-progress.json").read_bytes() == original_progress
        evidence = {name: (session.workdir / name).read_bytes() for name in (
            "pod-session-progress.json", "held-session-joblist-progress.json", "held-session-followup-failures.jsonl")}
        packets, events = _packet_snapshot(session.workdir), list(pod.events)
        renewal = held_control(session, pod, cli, transport_factory, renew=True)
        assert renewal.renew_held() == 0, failure_diagnostics(renewal)
        renewed = json.loads(held_path.read_text())
        assert dict(renewed, expires_at=reconciled["expires_at"]) == reconciled
        assert all((session.workdir / name).read_bytes() == data for name, data in evidence.items())
        assert _packet_snapshot(session.workdir) == packets
        assert pod.events == events and pod.record_count == records and not pod.terminated

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


@pytest.mark.parametrize("fault", ["stale", "identity", "progress", "packet", "dangling-packet"])
def test_held_joblist_refuses_invalid_custody_before_staging(tmp_path, monkeypatch, fault):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        progress_path = session.workdir / "pod-session-progress.json"
        if fault == "stale":
            held = json.loads(held_path.read_text())
            held["expires_at"] = 1
            held_path.write_text(json.dumps(held))
        elif fault == "progress":
            progress_path.write_text("{}")
        elif fault == "packet":
            (session.workdir / "packets/clip").mkdir()
        elif fault == "dangling-packet":
            (session.workdir / "packets/clip").symlink_to(tmp_path / "missing-target")
        def changed_cli(argv, env):
            if fault == "identity" and argv[0] == "list" and "--name-prefix" in argv:
                pod.events.append("list")
                return 0, json.dumps([{"id":"otherpod", "name":session.name}]), ""
            return cli(argv, env)
        manifest = _held_joblist_manifest(tmp_path / f"resume-list-{fault}")
        records_before = pod.record_count
        control = held_control(session, pod, changed_cli, transport_factory, jobs=manifest)
        assert control.run_held_jobs() == 1
        assert pod.record_count == records_before
        rejected_packet = session.workdir / "packets/clip"
        if fault == "packet":
            assert rejected_packet.is_dir() and not any(rejected_packet.iterdir())
        elif fault == "dangling-packet":
            assert rejected_packet.is_symlink() and not rejected_packet.exists()
        else:
            assert not os.path.lexists(rejected_packet)
        assert not os.path.lexists(session.workdir / "packets/next-image")
        assert pod.events.count("launch") == 1 and pod.events.count("terminate") == 0
        assert not pod.terminated

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_failed_followup_then_retry_keeps_same_held_pod_and_primary_evidence(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated, failure_diagnostics(session)
        packet_before = _packet_snapshot(session.workdir)
        held = session.workdir / "held-session.json"
        pod_id = session.pod_id
        resumed = held_control(session, pod, cli, transport_factory, command="exit 9")
        assert resumed.run_held(command="exit 9") == 1
        failure_log = session.workdir / "held-session-followup-failures.jsonl"
        assert failure_log.is_file()
        failure = json.loads(failure_log.read_text().splitlines()[-1])
        assert failure["retryable"] is True and failure["session"] == session.sid
        assert failure["pod_id"] == pod_id and "exited 9" in failure["error"]
        assert json.loads(held.read_text())["status"] == "held"
        assert _packet_snapshot(session.workdir) == packet_before
        assert pod.events.count("terminate") == 0 and len(pod.launches) == 1

        marker = tmp_path / "retry-succeeded"
        command = f"printf retried > {shlex.quote(str(marker))}"
        retry = held_control(session, pod, cli, transport_factory, command=command)
        assert retry.run_held(command=command) == 1
        assert marker.read_text() == "retried"
        progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
        assert any(r["step"] == "job:image" and r["result"] == "failed"
                   and "exited 7" in r["detail"]["error"] for r in progress["steps"])
        assert json.loads(held.read_text())["status"] == "held"
        assert retry.pod_id == pod_id and pod.events.count("terminate") == 0
        assert pod.events.count("launch") == 1 and len(pod.launches) == 1
        assert _packet_snapshot(session.workdir) == packet_before

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


@pytest.mark.parametrize("fault", ["timeout", "lost-start-response", "cancelled", "cancel-error", "interrupt",
                                   "malformed-terminal"])
def test_held_reconcile_uncertain_operation_blocks_every_new_start(tmp_path, monkeypatch, fault):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        before = json.loads(held_path.read_text())
        progress_path = session.workdir / "pod-session-progress.json"
        progress_before = progress_path.read_bytes()
        packets_before = _packet_snapshot(session.workdir)
        command = "printf uncertain-followup"
        control = held_control(session, pod, cli, transport_factory, command=command)
        transport = pod.transport
        original_start, original_poll = transport.detached_start, transport.detached_poll
        starts, cancellations, target, interrupted = [], [], {}, []

        def start(command_to_start, *, root, job_id=None, launch_timeout=60):
            durable = json.loads(held_path.read_text())
            assert durable["status"] == "in_use"
            assert durable["operation"]["id"] == job_id and isinstance(job_id, str)
            assert durable["operation"]["state"] == "starting"
            assert durable["charged_jobs"] == control.budget.count
            starts.append(job_id)
            if command_to_start == command:
                target["id"] = job_id
            job = original_start(command_to_start, root=root, job_id=job_id,
                                 launch_timeout=launch_timeout)
            if command_to_start == command and fault == "lost-start-response":
                raise TimeoutError("fixture lost response after remote launch")
            return job

        def poll(job_id, *, root):
            if job_id != target.get("id"):
                return original_poll(job_id, root=root)
            if fault == "cancel-error":
                raise RuntimeError("fixture cancellation outcome cannot be confirmed")
            if fault == "interrupt" and not interrupted:
                interrupted.append(job_id)
                raise KeyboardInterrupt()
            return {"id":job_id, "dir":root + "/" + job_id,
                    "state":"cancelled" if fault == "cancelled" else
                            "exited" if fault == "malformed-terminal" else "running",
                    "exit_code":"0" if fault == "malformed-terminal" else None}

        def cancel(job_id, *, root):
            cancellations.append(job_id)
            raise RuntimeError("fixture cancellation response lost")

        monkeypatch.setattr(transport, "detached_start", start)
        monkeypatch.setattr(transport, "detached_poll", poll)
        monkeypatch.setattr(transport, "detached_cancel", cancel)
        original_wait = control.wait_job
        def wait(job, timeout):
            if job["id"] != target.get("id") or fault != "timeout":
                return original_wait(job, timeout)
            ticks = iter((0.0, 2.0))
            with monkeypatch.context() as patch:
                patch.setattr(ps, "time", Namespace(time=ps.time.time,
                                                    monotonic=lambda: next(ticks)))
                return original_wait(job, 1)
        monkeypatch.setattr(control, "wait_job", wait)
        if fault == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                control.run_held(command=command)
        else:
            assert control.run_held(command=command) == 1

        held = json.loads(held_path.read_text())
        assert target.get("id"), control.test_messages
        assert held["status"] == "in_use"
        assert held["operation"] == {"id":target["id"], "label":"follow-up",
                                     "state":"starting", "exit_code":None}
        assert held["charged_jobs"] == control.budget.count == pod.record_count
        assert held["charged_jobs"] > before.get("charged_jobs", before["jobs"])
        assert held["jobs"] == before["jobs"]
        assert held["progress_sha256"] == before["progress_sha256"]
        assert progress_path.read_bytes() == progress_before
        assert _packet_snapshot(session.workdir) == packets_before
        assert not cancellations and not pod.terminated and pod.events.count("terminate") == 0
        assert "RECONCILIATION REQUIRED:" in "\n".join(control.test_messages)
        failure = json.loads((session.workdir / "held-session-followup-failures.jsonl").read_text().splitlines()[-1])
        assert failure["job_id"] == target["id"] and failure["retryable"] is False

        starts_before, records_before = list(starts), pod.record_count
        blocked = held_control(session, pod, cli, transport_factory, command="printf forbidden-second-job")
        assert blocked.run_held(command="printf forbidden-second-job") == 1
        assert starts == starts_before and pod.record_count == records_before
        assert json.loads(held_path.read_text())["status"] == "in_use"
        assert progress_path.read_bytes() == progress_before
        assert _packet_snapshot(session.workdir) == packets_before
        assert not cancellations and pod.events.count("launch") == 1 and len(pod.launches) == 1

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_held_reconcile_terminal_proof_needs_separate_turn_before_same_pod_retry(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        progress_path = session.workdir / "pod-session-progress.json"
        progress_before = progress_path.read_bytes()
        packets_before = _packet_snapshot(session.workdir)
        transport = pod.transport
        original_start, original_poll = transport.detached_start, transport.detached_poll
        target, uncertain = {}, [True]
        command = "exit 9"
        def start(command_to_start, *, root, job_id=None, launch_timeout=60):
            job = original_start(command_to_start, root=root, job_id=job_id,
                                 launch_timeout=launch_timeout)
            if command_to_start == command:
                target["id"] = job_id
                raise TimeoutError("fixture response lost after starting failed follow-up")
            return job
        def poll(job_id, *, root):
            if uncertain[0] and job_id == target.get("id"):
                return {"id":job_id, "dir":root + "/" + job_id, "state":"running", "exit_code":None}
            return original_poll(job_id, root=root)
        monkeypatch.setattr(transport, "detached_start", start)
        monkeypatch.setattr(transport, "detached_poll", poll)
        failed = held_control(session, pod, cli, transport_factory, command=command)
        assert failed.run_held(command=command) == 1
        assert json.loads(held_path.read_text())["status"] == "in_use"
        charged = json.loads(held_path.read_text())["charged_jobs"]
        records_before = pod.record_count

        uncertain[0] = False
        marker = tmp_path / "retry-after-proof"
        retry_command = f"printf retried > {shlex.quote(str(marker))}"
        reconcile = held_control(session, pod, cli, transport_factory, command=retry_command)
        assert reconcile.run_held(command=retry_command) == 1
        reconciled = json.loads(held_path.read_text())
        assert reconciled["status"] == "held" and reconciled["charged_jobs"] == charged
        assert reconciled["operation"] == {"id":target["id"], "label":"follow-up", "state":"exited", "exit_code":9}
        assert pod.record_count == records_before and not marker.exists()
        assert progress_path.read_bytes() == progress_before
        assert _packet_snapshot(session.workdir) == packets_before

        retry = held_control(session, pod, cli, transport_factory, command=retry_command)
        assert retry.run_held(command=retry_command) == 1, failure_diagnostics(retry)
        assert marker.read_text() == "retried"
        held = json.loads(held_path.read_text())
        assert held["status"] == "held" and held["charged_jobs"] == retry.budget.count == pod.record_count
        assert held["charged_jobs"] > charged and retry.pod_id == session.pod_id
        assert pod.events.count("launch") == 1 and len(pod.launches) == 1
        assert not pod.terminated and pod.events.count("terminate") == 0
        assert _packet_snapshot(session.workdir) == packets_before
        assert any(r["step"] == "job:image" and r["result"] == "failed"
                   and "exited 7" in r["detail"]["error"]
                   for r in json.loads(progress_path.read_text())["steps"])

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_held_reconcile_repeated_terminal_failures_exhaust_durable_budget(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        before = json.loads(held_path.read_text())
        progress_path = session.workdir / "pod-session-progress.json"
        progress_before = progress_path.read_bytes()
        packets_before = _packet_snapshot(session.workdir)
        charged = before.get("charged_jobs", before["jobs"])
        limit = charged + 7  # Two probes and one failed command per attempt.
        for attempt in range(2):
            control = held_control(session, pod, cli, transport_factory, command="exit 9")
            control.budget = ps.JobBudget(limit=limit)
            assert control.run_held(command="exit 9") == 1
            held = json.loads(held_path.read_text())
            assert held["status"] == "held"
            assert held["operation"]["state"] == "exited" and held["operation"]["exit_code"] == 9
            assert held["charged_jobs"] == charged + (attempt + 1) * 3
            assert held["charged_jobs"] == control.budget.count == pod.record_count
            assert held["jobs"] == before["jobs"] and held["progress_sha256"] == before["progress_sha256"]
            assert progress_path.read_bytes() == progress_before
            assert _packet_snapshot(session.workdir) == packets_before
        records_before = pod.record_count
        blocked = held_control(session, pod, cli, transport_factory, command="printf budget-bypass")
        blocked.budget = ps.JobBudget(limit=limit)
        assert blocked.run_held(command="printf budget-bypass") == 1
        assert pod.record_count == records_before == limit - 1
        held = json.loads(held_path.read_text())
        assert held["charged_jobs"] == records_before and held["jobs"] == before["jobs"]
        assert "job budget" in "\n".join(blocked.test_messages)
        assert progress_path.read_bytes() == progress_before and _packet_snapshot(session.workdir) == packets_before
        assert not pod.terminated and pod.events.count("terminate") == 0
        assert pod.events.count("launch") == 1 and len(pod.launches) == 1

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_held_reconcile_terminal_receipt_persistence_failure_stays_in_use(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        progress_path = session.workdir / "pod-session-progress.json"
        progress_before = progress_path.read_bytes()
        packets_before = _packet_snapshot(session.workdir)
        control = held_control(session, pod, cli, transport_factory, command="exit 9")
        original_write = control._write_held
        def write(doc, *, create=False):
            operation = doc.get("operation")
            if operation and operation["label"] == "follow-up" and operation["state"] == "exited":
                raise OSError("fixture terminal receipt publication interrupted")
            return original_write(doc, create=create)
        monkeypatch.setattr(control, "_write_held", write)
        assert control.run_held(command="exit 9") == 1
        held = json.loads(held_path.read_text())
        assert held["status"] == "in_use" and held["operation"]["state"] == "starting"
        assert held["charged_jobs"] == control.budget.count == pod.record_count
        assert progress_path.read_bytes() == progress_before and _packet_snapshot(session.workdir) == packets_before
        assert "RECONCILIATION REQUIRED:" in "\n".join(control.test_messages)
        assert not pod.terminated and pod.events.count("terminate") == 0

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_held_reconcile_reservation_persistence_failure_never_dispatches(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        progress_path = session.workdir / "pod-session-progress.json"
        progress_before = progress_path.read_bytes()
        packets_before = _packet_snapshot(session.workdir)
        marker = tmp_path / "must-not-dispatch"
        command = f"printf forbidden > {shlex.quote(str(marker))}"
        control = held_control(session, pod, cli, transport_factory, command=command)
        original_write = control._write_held
        rejected = []
        def write(doc, *, create=False):
            operation = doc.get("operation")
            if operation and operation["label"] == "follow-up" and operation["state"] == "starting":
                rejected.append(operation["id"])
                raise OSError("fixture reservation publication interrupted before dispatch")
            return original_write(doc, create=create)
        monkeypatch.setattr(control, "_write_held", write)
        assert control.run_held(command=command) == 1
        held = json.loads(held_path.read_text())
        assert rejected and held["status"] == "in_use" and not marker.exists()
        assert pod.record_count <= control.budget.count <= pod.record_count + 1
        assert progress_path.read_bytes() == progress_before and _packet_snapshot(session.workdir) == packets_before
        assert not pod.terminated and pod.events.count("terminate") == 0
        records_before = pod.record_count
        reconcile = held_control(session, pod, cli, transport_factory, command=command)
        assert reconcile.run_held(command=command) == 1
        assert pod.record_count == records_before and not marker.exists()
        assert progress_path.read_bytes() == progress_before and _packet_snapshot(session.workdir) == packets_before

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


def test_held_reconcile_progress_start_response_loss_keeps_exact_operation(tmp_path, monkeypatch):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        progress_path = session.workdir / "pod-session-progress.json"
        before = json.loads(held_path.read_text())
        progress_before = progress_path.read_bytes()
        packets_before = _packet_snapshot(session.workdir)
        transport = pod.transport
        original_start, original_poll = transport.detached_start, transport.detached_poll
        target = {}
        def start(command, *, root, job_id=None, launch_timeout=60):
            durable = json.loads(held_path.read_text())
            assert durable["operation"]["id"] == job_id
            job = original_start(command, root=root, job_id=job_id, launch_timeout=launch_timeout)
            if durable["operation"]["label"] == "progress":
                target["id"] = job_id
                raise TimeoutError("fixture progress response lost after remote launch")
            return job
        def poll(job_id, *, root):
            if job_id == target.get("id"):
                return {"id":job_id, "dir":root + "/" + job_id, "state":"running", "exit_code":None}
            return original_poll(job_id, root=root)
        monkeypatch.setattr(transport, "detached_start", start)
        monkeypatch.setattr(transport, "detached_poll", poll)
        control = held_control(session, pod, cli, transport_factory, command="printf followed-up")
        assert control.run_held(command="printf followed-up") == 1
        held = json.loads(held_path.read_text())
        assert target.get("id"), control.test_messages
        assert held["status"] == "in_use" and held["jobs"] == before["jobs"]
        assert held["operation"] == {"id":target["id"], "label":"progress", "state":"starting", "exit_code":None}
        assert held["charged_jobs"] == control.budget.count == pod.record_count
        assert progress_path.read_bytes() == progress_before and _packet_snapshot(session.workdir) == packets_before
        records_before = pod.record_count
        blocked = held_control(session, pod, cli, transport_factory, command="printf forbidden-second-job")
        assert blocked.run_held(command="printf forbidden-second-job") == 1
        assert pod.record_count == records_before
        assert json.loads(held_path.read_text())["status"] == "in_use"
        assert progress_path.read_bytes() == progress_before and _packet_snapshot(session.workdir) == packets_before
        assert not pod.terminated and pod.events.count("terminate") == 0

    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)


@pytest.mark.parametrize("fault", ["diagnostic", "persistence"])
def test_transient_followup_diagnostic_or_persistence_error_retains_retry_custody(tmp_path, monkeypatch, fault):
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        held_path = session.workdir / "held-session.json"
        progress_path = session.workdir / "pod-session-progress.json"
        held_before = json.loads(held_path.read_text())
        progress_before = progress_path.read_bytes()
        packets_before = _packet_snapshot(session.workdir)
        control = held_control(session, pod, cli, transport_factory, command="printf followup")
        if fault == "diagnostic":
            original_record = control.record
            def fail_followup_record(step, result, detail):
                if step == "follow-up":
                    raise RuntimeError("fixture transient diagnostic interruption")
                return original_record(step, result, detail)
            monkeypatch.setattr(control, "record", fail_followup_record)
        else:
            monkeypatch.setattr(control, "write_progress", lambda **_kw: (_ for _ in ()).throw(
                OSError("fixture progress publication interruption")))
        assert control.run_held(command="printf followup") == 1
        assert not pod.terminated and pod.events.count("terminate") == 0
        assert len(pod.launches) == 1 and pod.events.count("launch") == 1
        assert progress_path.read_bytes() == progress_before
        assert _packet_snapshot(session.workdir) == packets_before
        failure_path = session.workdir / "held-session-followup-failures.jsonl"
        assert failure_path.is_file(), control.test_messages
        held_after = json.loads(held_path.read_text())
        assert held_after["status"] == "held"
        assert held_after["charged_jobs"] == control.budget.count == pod.record_count
        assert held_after["charged_jobs"] > held_before.get("charged_jobs", held_before["jobs"])
        assert held_after["operation"]["state"] == "exited"
        assert held_after["operation"]["exit_code"] == 0
        assert {key: held_after[key] for key in held_before if key not in ("charged_jobs", "operation")} == {
            key: value for key, value in held_before.items() if key not in ("charged_jobs", "operation")}
        failure = json.loads(failure_path.read_text().splitlines()[-1])
        assert failure["retryable"] is True and failure["session"] == session.sid
        assert failure["pod_id"] == session.pod_id
        assert failure["error"] == ("RuntimeError" if fault == "diagnostic" else "OSError")
        assert "RECONCILIATION REQUIRED:" not in "\n".join(control.test_messages)

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
                                  "identity", "storage", "readiness", "transport", "unaccounted-followup"])
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
        elif fault == "unaccounted-followup":
            # Older controllers restored the original count after failures;
            # those charges cannot be reconstructed from the legacy held record.
            (session.workdir / "held-session-followup-failures.jsonl").write_text(
                json.dumps({"retryable": True, "job_id": None}) + "\n")
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
        records_before = pod.record_count
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
        if fault == "unaccounted-followup":
            assert pod.record_count == records_before
            assert "prior follow-up charges need reconciliation" in "\n".join(resumed.test_messages)
        assert not marker.exists() and len(pod.launches) == 1
        assert not pod.terminated and pod.events.count("terminate") == 0
        assert "RECONCILIATION REQUIRED:" in "\n".join(resumed.test_messages)
        if fault not in ("missing", "corrupt"):
            assert held.exists()
            if fault in ("remote-source", "remote-manifest", "storage", "readiness", "transport"):
                assert json.loads(held.read_text())["status"] == "in_use"
        assert len(pod.launches) == 1 and not marker.exists()

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


def test_expected_storage_volume_id_match_is_accepted(tmp_path, monkeypatch):
    session, pod, code, messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, expected_storage_volume_id="fixture-volume")
    assert code == 0, "\n".join(messages)
    assert session.state["storage"]["volume_id"] == "fixture-volume"
    assert ("POST", "/uploads") in pod.requests
    assert_release_verified(session, pod)


def test_expected_storage_volume_id_mismatch_fails_closed_before_upload_and_releases(tmp_path, monkeypatch):
    session, pod, code, messages, _manifest, _document = run_jobs(
        tmp_path, monkeypatch, mixed=False, expected_storage_volume_id="expected-volume",
        observed_storage_volume_id="fixture-volume")
    assert code == 1
    assert any("does not match the expected storage volume ID" in message for message in messages)
    assert ("POST", "/uploads") not in pod.requests
    assert not pod.setup_log.exists()
    assert [record["step"] for record in session.records] == ["claim", "release"]
    assert_release_verified(session, pod)


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
def test_transient_detached_poll_failure_retains_only_recoverable_followup(tmp_path, monkeypatch, phase):
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
            assert (session.workdir / "held-session.json").exists()
            held = json.loads((session.workdir / "held-session.json").read_text())
            assert held["status"] == "held"
            assert held["operation"]["id"] == injected[0]
            assert held["operation"]["state"] == "exited" and held["operation"]["exit_code"] == 0
            assert held["charged_jobs"] == resumed.budget.count == pod.record_count
            assert "RECONCILIATION REQUIRED:" not in "\n".join(resumed.test_messages)
            assert not pod.terminated and pod.events.count("terminate") == 0
            progress = json.loads((session.workdir / "pod-session-progress.json").read_text())
            assert any(r["step"] == "job:image" and r["result"] == "failed" for r in progress["steps"])
        assert len(injected) == 1 and len(pod.launches) == 1
        if phase == "row":
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


@pytest.mark.parametrize("failure", [None, "install", "identity", "result", "evidence", "uncertain", "timeout"])
def test_source_bound_installer_then_h3_held_accounting(tmp_path, monkeypatch, failure):
    """Real held transport/accounting; selected CLI/readiness are CPU boundaries.

    The real canonical recovery and source checker have separate focused tests.
    This fixture refuses H3 until the selected installer boundary reports ready.
    """
    def after_run(session, pod, code, cli, transport_factory):
        assert code == 1 and not pod.terminated
        manifest = _held_joblist_manifest(tmp_path / "install-list")
        document = yaml.safe_load(manifest.read_text())
        document["jobs"] = [{"id": "recover", "type": "h3-install", "timeout": .05 if failure == "timeout" else 40}, document["jobs"][0]]
        manifest.write_text(yaml.safe_dump(document))
        selected = {"agent_root": "/synthetic/selected-agent", "runtime_root": "/synthetic/selected-runtime",
                    "agent_head": "a" * 40, "runtime_head": "b" * 40, "runtime_lock_sha256": "c" * 64}
        selection = tmp_path / "selected.json"
        selection.write_text(json.dumps(selected))
        evidence = {"schema": "astrid.pod-action-source.v1", **{k: selected[k] for k in
                    ("agent_head", "runtime_head", "runtime_lock_sha256")}, "identity": {
                    "release": 0, "managed_by": "source", "dirty": False, "dirty_hash": "d" * 64,
                    **{k: selected[k] for k in ("agent_head", "runtime_head")}}}
        ready = pod.root / "selected-ready"
        invocations = []
        def selected_cli(source, evidence_path, argv):
            assert source == selected
            invocations.append(argv)
            row_evidence = dict(evidence)
            if failure == "identity" and argv[:2] == ["engines", "install"]:
                row_evidence["agent_head"] = "0" * 40
            script = "import json,pathlib,subprocess,sys\n"
            if failure != "evidence":
                script += f"pathlib.Path({evidence_path!r}).write_text({json.dumps(row_evidence)!r})\n"
            if argv[:2] == ["engines", "install"]:
                assert argv[:3] == ["engines", "install", ps.ENGINE]
                assert argv[argv.index("--root") + 1] == ps.ENGINE_ROOT
                assert argv[argv.index("--model-cache-device") + 1] == "47"
                assert argv[argv.index("--model-cache-mount-id") + 1] == "102"
                assert "--model-cache-root" in argv
                result = {"schema": "astrid.pod-h3-install.v1", "engine": ps.ENGINE,
                          "root": ps.ENGINE_ROOT, "status": "ready", "receipt_sha256": "e" * 64,
                          "receipt_id": "canonical-synthetic-receipt"}
                if failure == "result": result["root"] = "/wrong/root"
                script += f"pathlib.Path({str(ready)!r}).touch()\nprint({json.dumps(result)!r})\n"
                if failure == "install": script += "sys.exit(7)\n"
            else:
                assert argv[:3] == ["action", "run", ps.ACTION]
                script += f"assert pathlib.Path({str(ready)!r}).is_file(), 'readiness requires install'\n"
                script += f"sys.exit(subprocess.call({['astrid', *argv]!r}))\n"
            return shlex.join([sys.executable, "-c", script])
        monkeypatch.setattr(ps, "source_invocation", selected_cli)
        # held_control reads selection before touching the transport.
        parser = ps.build_parser
        def with_selection():
            result = parser()
            result.set_defaults(h3_source_selection=str(selection))
            return result
        monkeypatch.setattr(ps, "build_parser", with_selection)
        before = pod.record_count
        held_before = json.loads((session.workdir / "held-session.json").read_text())
        control = held_control(session, pod, cli, transport_factory, jobs=manifest)
        labels = []
        reserve = control._reserve_held_job
        def counted(label):
            labels.append(label)
            return reserve(label)
        monkeypatch.setattr(control, "_reserve_held_job", counted)
        if failure in ("uncertain", "timeout"):
            original = control.transport_factory
            def uncertain_factory(*args):
                transport = original(*args)
                start, poll = transport.detached_start, transport.detached_poll
                timed_out = set()
                def uncertain(command, **kwargs):
                    if "canonical-synthetic-receipt" in command:
                        if failure == "uncertain":
                            raise OSError("synthetic lost launch response")
                        timed_out.add(kwargs["job_id"])
                    return start(command, **kwargs)
                def polling(job_id, **kwargs):
                    if job_id in timed_out:
                        return {"state": "running"}  # terminal proof unavailable until reconciliation
                    return poll(job_id, **kwargs)
                transport.detached_start, transport.detached_poll = uncertain, polling
                return transport
            monkeypatch.setattr(control, "transport_factory", uncertain_factory)
        assert control.run_held_jobs() == (0 if failure is None else 1), failure_diagnostics(control)
        held = json.loads((session.workdir / "held-session.json").read_text())
        for key in ("session", "pod_id", "source", "runtime_identity", "volume_id", "expires_at"):
            assert held[key] == held_before[key]
        assert held["charged_jobs"] == control.budget.count
        assert not pod.terminated and len(pod.launches) == 1
        assert control.budget.count - held_before.get("charged_jobs", held_before["jobs"]) == len(labels)
        progress = json.loads((session.workdir / "held-session-joblist-progress.json").read_text())
        if failure is None:
            expected = ["storage mount verification", "Runtime readiness", "progress", "job:recover", "progress",
                        "progress", "h3:clip:s0", "progress", "job:clip", "progress", "remote sha256", "progress", "progress"]
            assert labels == expected
            assert pod.record_count - before == len(labels) == 13
            assert 86 + len(labels) == 99 < 100
            assert held["status"] == "held" and progress == json.loads(
                (session.workdir / "pod-session-progress.remote.json").read_text())
            assert [argv[:2] for argv in invocations] == [["engines", "install"], ["action", "run"], ["action", "run"]]
            assert json.loads((session.workdir / "packets/recover/machine-check.json").read_text())["status"] == "passed"
            print(json.dumps({"records": len(labels), "labels": labels, "charged_before_projection": 86,
                              "charged_after_projection": 99, "remaining_before_strict_cap": 0}, sort_keys=True))
        else:
            assert len(invocations) == 1 and "h3:clip:s0" not in labels
            assert any(r["step"] == "job:recover" and r["result"] == "failed" for r in progress["steps"])
            assert json.loads((session.workdir / "packets/recover/machine-check.json").read_text())["status"] == "failed"
            assert held["status"] == ("in_use" if failure in ("uncertain", "timeout") else "held")
            assert pod.record_count - before == len(labels) - (failure == "uncertain")
            if failure in ("uncertain", "timeout"):
                assert held["operation"]["state"] == "starting" and held["operation"]["label"] == "job:recover"
            if failure in ("install", "result"):
                assert json.loads((session.workdir / "packets/recover/logs/job.source-identity.json").read_text()) == evidence
    run_jobs(tmp_path, monkeypatch, mixed=False, single=True, hold=True,
             failure="command", after_run=after_run)
