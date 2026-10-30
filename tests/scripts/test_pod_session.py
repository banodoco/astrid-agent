"""CPU tests for scripts/pod_session.py with a stub runpod CLI and a stub pod transport."""
import base64
import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pod_session.py"
spec = importlib.util.spec_from_file_location("pod_session_under_test", SCRIPT)
ps = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ps
spec.loader.exec_module(ps)

REQUEST = {
    "version": 1, "operation": "generate", "source": None, "output": {"duration": 9},
    "content": {"prompt": "A short shot."}, "changes": {"video": [], "audio": []},
    "references": [{"asset": "img1", "purpose": "appearance"}, {"asset": "img2", "purpose": "style"}],
    "overrides": {},
}
TOKEN_HOLDER = {}


@pytest.fixture(scope="session")
def video(tmp_path_factory):
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe not available")
    path = tmp_path_factory.mktemp("video") / "final.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1024x576:rate=24:duration=9",
                    "-f", "lavfi", "-i", "sine=duration=9", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "45",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "16k", "-shortest", str(path)], check=True)
    return path.read_bytes()


class FakePod:
    """Stands in for both the runpod-lifecycle CLI and the HTTPS pod transport."""

    def __init__(self, video_bytes, fail=None, fail_gpus=(), launch_result=None, list_result=None):
        self.video, self.fail, self.fail_gpus = video_bytes, fail, set(fail_gpus)
        self.launch_result, self.list_result = launch_result, list_result
        self.events, self.files, self.jobs, self.puts = [], {}, {}, {}
        self.starts = self.executes = 0
        self.terminated, self.launches, self.launch_storage_env = False, [], []
        self.http_checks = []
        self.pod = "abc123podid"

    # -- CLI -------------------------------------------------------------------------------
    def cli(self, argv, env, *, on_capacity_wait=None):
        cmd = argv[0]
        if cmd == "launch":
            self.launches.append(argv)
            self.launch_storage_env.append({key: env.get(key) for key in
                                            ("RUNPOD_STORAGE_NAME", "RUNPOD_STORAGE_VOLUMES", "RUNPOD_ATTACH_ONLY")})
            self.events.append("launch")
            if on_capacity_wait:
                on_capacity_wait('RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}\n')
            if self.launch_result is not None:
                if on_capacity_wait and "allocation_unknown" in str(self.launch_result):
                    on_capacity_wait('RUNPOD_TERMINAL_FAILURE {"classification":"allocation_unknown","placement_attempt":1}\n')
                return self.launch_result
            if self.fail == "missing_storage":
                return 1, "", "error: existing storage volume not found (attach-only)"
            gpu = argv[argv.index("--gpu-type") + 1]
            if self.fail == "claim" or gpu in self.fail_gpus:
                if on_capacity_wait:
                    on_capacity_wait("RUNPOD_CAPACITY_EXHAUSTED\n")
                return 1, "", "error: Failed to launch pod across GPU candidates ['fake']: no longer any instances available"
            return 0, json.dumps({"pod_id": self.pod, "name": "n", "https": "x"}), ""
        if cmd == "volumes":
            self.events.append("volumes")
            volumes = [{"id": "vol187", "name": ps.STORAGE_NAME, "dataCenterId": ps.DATACENTER}]
            if self.fail == "volume-missing": volumes = []
            if self.fail == "volume-ambiguous": volumes *= 2
            return 0, json.dumps(volumes), ""
        if cmd == "mounts":
            self.events.append("mounts")
            nonce = argv[argv.index("--nonce") + 1]
            provider = {"schema": "runpod.provider-mounts.v1", "pod_id": self.pod, "nonce": nonce,
                        "volume_mounts": [{"volume_id": "vol187", "volume_type": "NETWORK_VOLUME", "mount_path": ps.CACHE_MOUNT, "read_only": False}],
                        "legacy_evidence": {"present": False, "valid": True, "conflict": False}}
            return 0, json.dumps(provider), ""
        if cmd == "terminate":
            self.events.append("terminate")
            self.terminated = True
            return 0, "terminated", ""
        if cmd == "list" and not self.terminated and self.launches and "ready" in self.events:
            self.events.append("attachment")
            return 0, json.dumps([{"id": self.pod, "network_volume_id":
                None if self.fail == "attachment-missing" else "wrong" if self.fail == "attachment-wrong" else "vol187",
                "network_volume_evidence": {"present": True, "valid": self.fail != "attachment-missing", "conflict": self.fail == "attachment-wrong"}}]), ""
        if cmd == "list":
            self.events.append("list")
            return self.list_result if self.list_result is not None else (0, "[]", "")
        raise AssertionError(argv)

    def http_status(self, pod_id):
        self.http_checks.append(pod_id)
        return 404 if self.terminated else 200

    def factory(self, pod_id, token):
        TOKEN_HOLDER["token"] = token
        assert pod_id == self.pod
        return self

    # -- transport -------------------------------------------------------------------------
    def wait_ready(self, timeout=0):
        self.events.append("ready")

    def disconnect(self):
        self.events.append("disconnect")

    def put(self, local, remote, **kw):
        data = Path(local).read_bytes()
        self.files[remote] = data
        self.puts[remote] = data
        self.events.append("put:" + remote)

    def get(self, remote, local, **kw):
        if remote not in self.files:
            raise RuntimeError("no such remote file")
        Path(local).write_bytes(self.files[remote])

    def execute_command(self, command, timeout=600):
        self.executes += 1
        if "def storage_probe(" in command:
            import shlex
            request = json.loads(base64.b64decode(shlex.split(command)[-1]))
            self.events.append("exec:storage")
            if self.fail in ("mount-missing", "mount-wrong", "capacity"):
                return 1, "", "mount/capacity failed"
            return 0, json.dumps({key: value for key, value in request.items() if key != "provider_mapping"} | {"free_bytes": 10**15, "total_bytes": 10**15, "local_free_bytes": 10**15, "cache_device": 47, "cache_mount_id": 102}), ""
        if "stat -c %s" in command:
            data = self.files[ps.BUNDLE_REMOTE]
            size = len(data) + (1 if self.fail == "bundle" else 0)
            self.events.append("exec:bundle-check")
            return 0, f"{size}\n{hashlib.sha256(data).hexdigest()}  {ps.BUNDLE_REMOTE}\n", ""
        if command.startswith("sha256sum "):
            self.events.append("exec:sha256")
            lines = []
            for path in command.split()[1:]:
                data = self.files[path]
                if self.fail == "download-sha" and path.endswith(".media"):
                    data = data + b"x"
                lines.append(f"{hashlib.sha256(data).hexdigest()}  {path}")
            return 0, "\n".join(lines) + "\n", ""
        raise AssertionError(command)

    def detached_start(self, command, *, root, job_id=None, launch_timeout=60):
        self.starts += 1
        jid = f"{self.starts:032x}"
        label, code, out = self._run(command)
        self.events.append("job:" + label)
        self.files[f"{root}/{jid}/stdout"] = out.encode()
        self.files[f"{root}/{jid}/stderr"] = b""
        self.jobs[jid] = {"label": label, "code": code, "command": command}
        return {"id": jid, "dir": f"{root}/{jid}"}

    def detached_poll(self, job_id, *, root):
        job = self.jobs[job_id]
        return {"id": job_id, "state": "exited", "exit_code": job["code"], "pid": 1}

    def _run(self, command):
        fail = self.fail
        if ps.PROGRESS_REMOTE in command:
            blob = re.search(r"printf %s (\S+) \| base64", command).group(1).strip("'")
            self.files[ps.PROGRESS_REMOTE] = base64.b64decode(blob)
            return "progress", 0, ""
        if "pod_runtime_ready.py" in command:
            import shlex
            request = json.loads(base64.b64decode(shlex.split(command)[-1]))
            if fail == "runtime-identity": request["runtime_head"] = "wrong"
            return "runtime-ready", 1 if fail == "runtime-unready" else 0, json.dumps(request | {"ready": True, "stopped": True})
        if "pod_bootstrap.sh" in command:
            return "bootstrap", 1 if fail == "bootstrap" else 0, "bootstrap ok\n"
        if "engines status" in command:
            status = {"status": "installed", "health": {"status": "healthy"}}
            if fail == "status":
                status["health"]["status"] = "unhealthy"
            return "status", 0, json.dumps(status) + "\n"
        if "engines install" in command and "--deadline 14400" in command:
            steps = [{"id": "environment:comfy", "action": "executed"}, {"id": "model:a", "action": "executed"}]
            return "install", 1 if fail == "install" else 0, json.dumps({"status": "installed", "steps": steps})
        if "engines install" in command:
            action = "executed" if fail == "rerun" else "skipped"
            return "rerun", 0, json.dumps({"status": "installed", "steps": [{"id": "s", "action": action}] * 12})
        if "action run" in command:
            name = re.search(r"--out (\S+)", command).group(1)
            which = "s0" if "smoke-request.json" in command else "clip"
            summary_path = re.search(r"> (\S+\.summary\.json)", command).group(1)
            ok = fail != which
            self.files[summary_path] = json.dumps({
                "ok": ok, "out": name, "receipt": f"{name}/{ps.FINAL_RECEIPT}", "manifest": f"{name}/run/manifest.json",
            }).encode()
            verified = "passed" if fail != "receipt" else "not_claimed"
            sha = hashlib.sha256(self.video).hexdigest()
            self.files[f"{name}/{ps.FINAL_RECEIPT}"] = json.dumps({
                "states": {"final_composition_publication": {"status": verified,
                                                              "evidence": {"publication": {"object_id": "sha256:" + sha}}}},
                "publication_contract": {"final_composition_publication": "verified" if verified == "passed" else "required"},
            }).encode()
            self.files[f"{name}/{ps.FINAL_VIDEO}"] = self.video
            self.files[f"{name}/run/manifest.json"] = b'{"manifest": true}'
            return which, 0 if ok else 1, ""
        raise AssertionError(command)


def offline_launch_cli(binary, pod):
    """Exercise the real streaming launcher, with explicit read-only provider fakes."""
    launch = ps.default_cli_runner(str(binary))
    def cli(argv, env, *, on_capacity_wait=None):
        if argv[0] == "launch":
            pod.launches.append(argv)
            pod.events.append("launch")
            return launch(argv, env, on_capacity_wait=on_capacity_wait)
        return pod.cli(argv, env, on_capacity_wait=on_capacity_wait)
    cli.supports_capacity_progress = True
    return cli


def make_args(tmp_path, extra=()):
    sources = tmp_path / "sources"
    for name in ps.BUNDLE_TREES[1:]:
        (sources / name).mkdir(parents=True, exist_ok=True)
        (sources / name / "file.txt").write_text(name)
    agent = tmp_path / "agent"
    (agent / "scripts").mkdir(parents=True, exist_ok=True)
    (agent / "scripts" / "gpu_sanity.py").write_text("print('x')")
    (agent / "tests").mkdir(exist_ok=True)
    (agent / "tests" / "t.py").write_text("t")
    git_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True, env=git_env).stdout.strip()
    for root in [*(sources / name for name in ps.BUNDLE_TREES[1:]), agent]:
        if root == agent:
            (agent / "runtime.lock.json").write_text(json.dumps({
                "source_commit": git(sources / "astrid-runtime", "rev-parse", "HEAD")}))
        git(root, "init", "-q")
        git(root, "add", ".")
        git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    for n in ("img1", "img2"):
        (assets / f"{n}.png").write_bytes(n.encode())
    (tmp_path / "request.json").write_text(json.dumps(REQUEST, indent=2))
    (tmp_path / "asset-map.json").write_text(json.dumps({n: str(assets / f"{n}.png") for n in ("img1", "img2")}))
    argv = ["--datacenters", ps.DATACENTER, "--storage-name", ps.STORAGE_NAME,
            "--sources-dir", str(sources), "--request", str(tmp_path / "request.json"),
            "--asset-map", str(tmp_path / "asset-map.json"), "--workdir", str(tmp_path / "work"),
            "--token-dir", str(tmp_path / "tokens"), "--poll-interval", "0", *extra]
    return ps.build_parser().parse_args(argv), agent


FakePod.cli.supports_capacity_progress = True

def run_session(tmp_path, video_bytes, *, fail=None, fail_gpus=(), budget=None, probe=None, extra=(),
                launch_result=None, list_result=None):
    args, agent = make_args(tmp_path, extra)
    pod = FakePod(video_bytes, fail=fail, fail_gpus=fail_gpus, launch_result=launch_result, list_result=list_result)
    messages = []
    kw = {"probe": probe} if probe else {}
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         budget=budget, sleep=lambda s: None, say=messages.append, agent_root=agent, **kw)
    code = session.run()
    return session, pod, code, messages


def job_commands(pod, label):
    return [j["command"] for j in pod.jobs.values() if j["label"] == label]


# ---- (a) step order -----------------------------------------------------------------------

def test_full_run_order_and_outputs(tmp_path, video, capsys):
    session, pod, code, messages = run_session(tmp_path, video)
    assert code == 0, messages
    assert [r["step"] for r in session.records] == list(ps.STEPS)
    assert all(r["result"] == "ok" for r in session.records)
    labels = [e for e in pod.events if not e.startswith("job:progress")]
    firsts = ["launch", "put:" + ps.BUNDLE_REMOTE, "exec:bundle-check", "job:bootstrap", "job:runtime-ready", "job:install", "job:rerun",
              "job:status", "job:s0", "job:clip", "exec:sha256", "terminate"]
    positions = [labels.index(e) for e in firsts]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)
    assert pod.events.count("terminate") == 1 and pod.events[-1] != "launch"
    # progress: one job after each of the eight pod steps, readable back from the pod
    assert pod.events.count("job:progress") == 8
    remote = json.loads(pod.files[ps.PROGRESS_REMOTE])
    assert [s["step"] for s in remote["steps"]] == list(ps.STEPS[:-1])
    assert remote["jobs"] == session.budget.count  # the last progress job counts itself; release makes none
    assert (tmp_path / "work" / "pod-session-progress.remote.json").exists()
    # the counter reconciles with the agent job records the stub saw
    assert session.budget.count == pod.starts + pod.executes
    # download: verified sha, ffprobe passed, files present
    detail = session.records[-2]["detail"]
    assert detail["video"] == {"frames": 216, "rate": "24/1", "width": 1024, "height": 576, "audio": True}
    assert set(p.name for p in (tmp_path / "work" / "download").iterdir()) == {
        "verified-candidate.media", ps.FINAL_RECEIPT, "manifest.json"}
    # release verified GET 404 and list []
    assert session.records[-1]["detail"] == {"pods": [pod.pod], "get": 404, "list": []}
    assert pod.http_checks == [pod.pod] and pod.events.count("list") == 1 and session.released
    # token: fresh, never printed, removed after release
    out = capsys.readouterr()
    token = TOKEN_HOLDER["token"]
    assert len(token) >= 32 and token not in out.out + out.err + "".join(messages)
    assert not list((tmp_path / "tokens").glob("*.token"))


def test_claim_arguments_and_gpu_fallback(tmp_path, video):
    session, pod, code, _ = run_session(tmp_path, video, fail_gpus={"RTX 5090"})
    assert code == 0
    gpus = [a[a.index("--gpu-type") + 1] for a in pod.launches]
    dcs = [a[a.index("--datacenter-id") + 1] for a in pod.launches]
    assert gpus == ["RTX 5090", "RTX PRO 4500"] and dcs == [ps.DATACENTER, ps.DATACENTER]
    assert len(pod.launches) == 2 and "claim" not in session.state
    argv = pod.launches[-1]
    assert argv[argv.index("--allowed-cuda-versions") + 1] == "13.0,13.1,13.2,13.3"
    assert argv[argv.index("--container-disk-gb") + 1] == "120"
    assert argv[argv.index("--disk-size-gb") + 1] == "10"
    assert argv[argv.index("--image") + 1] == ps.recipe_image() and "--detach" in argv
    assert argv[argv.index("--storage-name") + 1] == ps.STORAGE_NAME
    assert pod.launch_storage_env == [
        {"RUNPOD_STORAGE_NAME": ps.STORAGE_NAME, "RUNPOD_STORAGE_VOLUMES": "", "RUNPOD_ATTACH_ONLY": "true"}
    ] * 2
    assert all("--storage-volumes" not in a for a in pod.launches)


@pytest.mark.parametrize("datacenters", [["EU-RO-1"], [ps.DATACENTER, "EU-RO-1"], [ps.DATACENTER, ps.DATACENTER]])
def test_invalid_datacenter_selection_fails_before_claim(tmp_path, video, datacenters):
    args, agent = make_args(tmp_path)
    args.datacenters = datacenters
    pod = FakePod(video)
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         sleep=lambda s: None, say=lambda message: None, agent_root=agent)

    assert session.run() == 1
    assert pod.launches == [] and pod.events == ["list"] and pod.starts == 0 and pod.executes == 0
    assert not list((tmp_path / "tokens").glob("*.token"))


def test_missing_storage_fails_closed_without_pod_or_fallback(tmp_path, video):
    args, agent = make_args(tmp_path)
    pod = FakePod(video, fail="missing_storage")
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         sleep=lambda s: None, say=lambda message: None, agent_root=agent)

    assert session.run() == 1
    assert len(pod.launches) == 1
    assert session.state["claim"]["state"] == "launch_failed"
    assert session.records[-1]["detail"]["status"] == "unresolved"
    assert all(a[a.index("--storage-name") + 1] == ps.STORAGE_NAME for a in pod.launches)
    assert all(a[a.index("--datacenter-id") + 1] == ps.DATACENTER for a in pod.launches)
    assert all("--storage-volumes" not in a for a in pod.launches)
    assert all(env["RUNPOD_ATTACH_ONLY"] == "true" and env["RUNPOD_STORAGE_VOLUMES"] == ""
               for env in pod.launch_storage_env)
    assert not pod.terminated and "ready" not in pod.events and pod.starts == 0 and pod.executes == 0


def test_missing_storage_name_fails_before_claim(tmp_path, video):
    args, agent = make_args(tmp_path)
    args.storage_name = ""
    pod = FakePod(video)
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         sleep=lambda s: None, say=lambda message: None, agent_root=agent)

    assert session.run() == 1
    assert pod.launches == [] and pod.events == ["list"] and pod.starts == 0 and pod.executes == 0
    assert not list((tmp_path / "tokens").glob("*.token"))


@pytest.mark.parametrize("launch_result", [
    (1, "", "error: allocation_unknown for request 'test'; reconcile the provider allocation before retrying"),
    (1, '{"status": "allocation_unknown", "reconciliation_required": true}', ""),
    (0, "{}", ""),
    (0, "not JSON", ""),
])
def test_ambiguous_claim_stops_after_one_launch_and_stays_unresolved(tmp_path, launch_result):
    session, pod, code, _ = run_session(tmp_path, b"", launch_result=launch_result)
    assert code == 1 and len(pod.launches) == 1
    assert [r["step"] for r in session.records] == ["claim", "release"]
    claim, release = session.records
    assert claim["result"] == "failed" and claim["detail"]["state"] == "allocation_unknown"
    assert claim["detail"]["reconciliation_required"] is True
    assert claim["detail"]["attempted"] == ["RTX 5090@EUR-NO-1"]
    assert "reconcile" in claim["detail"]["error"]
    assert release["result"] == "failed" and release["detail"]["status"] == "unresolved"
    assert release["detail"]["reconciliation_required"] is True
    assert release["detail"]["list"] == [] and release["detail"]["list_scope"] == "session"
    assert release["detail"]["get"] is None
    assert not session.released and not pod.terminated and not pod.http_checks
    assert "ready" not in pod.events and pod.starts == 0
    report = json.loads((tmp_path / "work" / "pod-session-progress.json").read_text())
    assert report["pod_id"] is None and report["steps"] == session.records


def test_known_no_capacity_release_reports_only_scoped_list_evidence(tmp_path):
    session, pod, code, _ = run_session(tmp_path, b"", fail="claim")
    assert code == 1 and len(pod.launches) == 2
    release = session.records[-1]
    assert release["result"] == "ok"
    assert release["detail"] == {"pods": [], "get": None, "list": [], "list_scope": "session", "list_exit_code": 0}
    assert session.released and not pod.http_checks


@pytest.mark.parametrize("wait", [0, 86400])
def test_capacity_policy_reaches_every_candidate_and_progress(tmp_path, wait):
    args, agent = make_args(tmp_path, ["--wait-capacity", str(wait)])
    pod = FakePod(b"", fail_gpus={"RTX 5090"})
    observations = []

    def cli(argv, env, *, on_capacity_wait=None):
        if argv[0] == "launch":
            # Inspect the local evidence while launch is blocked, before returning.
            progress = json.loads((Path(args.workdir) / "pod-session-progress.json").read_text())
            observations.append(progress["claim_attempt"])
        return pod.cli(argv, env, on_capacity_wait=on_capacity_wait)

    cli.supports_capacity_progress = True
    session = ps.Session(args, cli=cli, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=lambda _: None)
    result = session.claim()
    assert len(pod.launches) == 2 and result["wait_capacity_sec"] == wait
    assert [o["gpu"] for o in observations] == ["RTX 5090", "RTX PRO 4500"]
    assert all(o["datacenter"] == ps.DATACENTER for o in observations)
    assert all(o["wait_capacity_sec"] == wait and o["state"] == "launching" and o["started"] for o in observations)
    for argv in pod.launches:
        if wait:
            assert argv[argv.index("--wait-capacity") + 1] == str(wait)
        else:
            assert "--wait-capacity" not in argv


@pytest.mark.parametrize("stderr", [
    "error: HTTP 401 Unauthorized",
    "error: HTTP 403 Forbidden; no longer any instances available",
    "error: malformed request: syntax error",
    "error: GraphQL validation failed",
    "error: connection reset; no longer any instances available",
    "error: timeout waiting for response",
    "error: unrecognized failure",
    "warning: no longer any instances available\nerror: unauthorized",
])
def test_non_capacity_cli_failure_never_advances_with_wait(tmp_path, stderr):
    session, pod, code, _ = run_session(tmp_path, b"", extra=["--wait-capacity", "86400"],
                                      launch_result=(1, "", stderr))
    assert code == 1 and len(pod.launches) == 1
    assert session.records[0]["detail"]["state"] == "launch_failed"
    assert session.records[0]["detail"]["wait_capacity_sec"] == 86400
    assert session.records[-1]["result"] == "failed" and not session.released


@pytest.mark.parametrize("wait", [0, 86400])
@pytest.mark.parametrize("outcome", ["capacity", "timeout", "auth", "malformed", "ambiguous_capacity"])
def test_runner_through_lifecycle_wait_is_capacity_only(tmp_path, monkeypatch, wait, outcome):
    from runpod_lifecycle import cli, lifecycle
    from runpod_lifecycle.config import RunPodConfig
    from runpod_lifecycle.errors import ProviderCapacityUnavailable, ProviderRequestRejected
    from runpod_lifecycle.pod import Pod

    args, agent = make_args(tmp_path, ["--wait-capacity", str(wait)])
    pod = FakePod(b"")
    failures = {"capacity": ProviderCapacityUnavailable(200, "no longer any instances available"),
                "ambiguous_capacity": RuntimeError("no longer any instances available"),
                "timeout": TimeoutError("no longer any instances available; response timed out"),
                "auth": ProviderRequestRejected(401, "Unauthorized"),
                "malformed": RuntimeError("no pod ID returned")}
    create = Mock(side_effect=[failures[outcome], {"id": pod.pod}])
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(lifecycle, "find_gpu_type", lambda *_: {"id": "fake"})
    monkeypatch.setattr(lifecycle, "create_pod", create)
    monkeypatch.setattr(lifecycle.asyncio, "sleep", sleep)
    monkeypatch.setattr(Pod, "wait_ready", AsyncMock())
    monkeypatch.setattr(cli, "_resolve_config", lambda parsed: RunPodConfig(
        api_key="synthetic", gpu_type=parsed.gpu_type, ram_tiers_enabled=False,
        transport="https", https_token="synthetic", storage_volumes=(), storage_name=None))
    launch_args = []

    def runner(argv, env, *, on_capacity_wait=None):
        if argv[0] != "launch":
            return pod.cli(argv, env)
        pod.launches.append(argv)
        parsed = cli.build_parser().parse_args(["--transport", "https", *argv])
        launch_args.append(parsed)
        stdout = io.StringIO()
        parsed.capacity_progress_fd = 1
        with monkeypatch.context() as scope, contextlib.redirect_stdout(stdout):
            scope.setattr(cli, "_write_capacity_progress", lambda fd, line: on_capacity_wait(line))
            try:
                return asyncio.run(cli._cmd_launch(parsed)), stdout.getvalue(), ""
            except Exception as exc:
                return 1, stdout.getvalue(), f"error: {exc}"

    runner.supports_capacity_progress = True
    session = ps.Session(args, cli=runner, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=lambda _: None, sleep=lambda _: None)
    if outcome == "capacity":
        assert session.claim()["pod_id"] == pod.pod
        assert create.call_count == 2
        assert len(pod.launches) == (1 if wait else 2)
        assert sleeps == ([30] if wait else [])
    else:
        with pytest.raises(ps.SessionError, match="allocation_unknown"):
            session.claim()
        assert create.call_count == 1 and len(pod.launches) == 1 and not sleeps
        assert session.state["claim"]["reconciliation_required"] is True
    assert all(a.wait_capacity == wait for a in launch_args)


def test_ambiguity_with_wait_stays_unresolved_after_release(tmp_path):
    session, pod, code, _ = run_session(tmp_path, b"", extra=["--wait-capacity", "86400"],
                                      launch_result=(1, "", "error: allocation_unknown"))
    assert code == 1 and len(pod.launches) == 1
    assert pod.launches[0][-2:] == ["--wait-capacity", "86400"]
    assert session.records[0]["detail"]["reconciliation_required"] is True
    assert session.records[-1]["result"] == "failed" and not session.released


def test_controller_transport_exception_requires_reconciliation(tmp_path):
    args, agent = make_args(tmp_path, ["--wait-capacity", "86400"])
    pod = FakePod(b"")

    def runner(argv, env):
        if argv[0] == "launch":
            pod.launches.append(argv)
            raise ConnectionError("response lost")
        return pod.cli(argv, env)

    session = ps.Session(args, cli=runner, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=lambda _: None)
    assert session.run() == 1 and len(pod.launches) == 1
    assert session.records[0]["detail"]["state"] == "allocation_unknown"
    assert session.records[-1]["result"] == "failed" and not session.released


def test_exhausted_capacity_wait_can_advance(tmp_path):
    args, agent = make_args(tmp_path, ["--wait-capacity", "86400"])
    pod = FakePod(b"")

    def runner(argv, env, *, on_capacity_wait=None):
        result = pod.cli(argv, env)
        if argv[0] == "launch" and len(pod.launches) == 1:
            on_capacity_wait('RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}\n')
            on_capacity_wait("RUNPOD_CAPACITY_EXHAUSTED\n")
            return 1, "", ("error: Capacity did not become available after 2881 attempts over 86400s: "
                            "Failed to launch pod across GPU candidates ['fake']: no longer any instances available")
        return result

    runner.supports_capacity_progress = True
    session = ps.Session(args, cli=runner, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=lambda _: None)
    assert session.claim()["pod_id"] == pod.pod and len(pod.launches) == 2
    assert all(a[-2:] == ["--wait-capacity", "86400"] for a in pod.launches)


def test_https_bootstrap_sdk_serialization_roundtrips():
    from runpod.api.mutations.pods import generate_pod_deployment_mutation
    from runpod_lifecycle.https import agent_bootstrap

    arguments = agent_bootstrap("synthetic-offline-token-not-a-credential")["docker_args"]
    mutation = generate_pod_deployment_mutation(name="fake", image_name="fake", gpu_type_id="fake",
                                               docker_args=arguments)
    literal = mutation.split("dockerArgs: ", 1)[1]
    decoded, end = json.JSONDecoder().raw_decode(literal)
    assert decoded == arguments and literal[end:].lstrip().startswith((",", "}"))


@pytest.mark.parametrize("mode", ["legacy", "jobs", "held_jobs"])
def test_main_binds_controller_and_cli_to_bundle_sources(tmp_path, monkeypatch, mode):
    args, _ = make_args(tmp_path)
    if mode == "jobs":
        args.jobs, args.request, args.asset_map = "synthetic-unopened-jobs.yaml", None, None
    elif mode == "held_jobs":
        args.jobs, args.request, args.asset_map = None, None, None
        args.resume_held_jobs = "synthetic-unopened-held-jobs.yaml"
    sources = Path(args.sources_dir)
    (sources / "runpod-python/runpod/api/mutations").mkdir(parents=True)
    (sources / "runpod-python/runpod/__init__.py").write_text("# fake source package")
    (sources / "runpod-python/runpod/api/mutations/pods.py").write_text("# source marker")
    (sources / "runpod-lifecycle/src/runpod_lifecycle").mkdir(parents=True)
    (sources / "runpod-lifecycle/src/runpod_lifecycle/__init__.py").write_text("# fake source package")
    (sources / "runpod-lifecycle/src/runpod_lifecycle/cli.py").write_text("# source marker")
    paths = ps.controller_source_paths(sources)
    monkeypatch.setattr(ps.sys, "path", list(sys.path))
    monkeypatch.setattr(ps, "build_parser", lambda: SimpleNamespace(parse_args=lambda _: args))
    fake_cli = tmp_path / "fake-lifecycle"
    fake_cli.write_text(f"#!{sys.executable}\n" +
                        "import importlib.util, json, sys\n" +
                        "print(json.dumps({'sdk': importlib.util.find_spec('runpod').origin, " +
                        "'lifecycle': importlib.util.find_spec('runpod_lifecycle').origin, 'argv': sys.argv[1:]}))\n")
    fake_cli.chmod(0o700)
    args.runpod_cli = str(fake_cli)

    def session(*_args, **kwargs):
        def run():
            assert sys.path[:2] == list(map(str, paths))
            env = {"PYTHONPATH": "/prior-source"}
            code, stdout, stderr = kwargs["cli"](["launch"], env)
            assert code == 0, stderr
            origins = json.loads(stdout)
            assert Path(origins["sdk"]) == sources / "runpod-python/runpod/__init__.py"
            assert Path(origins["lifecycle"]) == sources / "runpod-lifecycle/src/runpod_lifecycle/__init__.py"
            assert origins["argv"] == ["--transport", "https", "launch"]
            assert env == {"PYTHONPATH": "/prior-source"}
            return 7
        if mode == "held_jobs":
            return SimpleNamespace(run_held_jobs=run,
                                   run=lambda: pytest.fail("held job list used provisioning driver"))
        return SimpleNamespace(run=run)

    selected, other = ("JobListSession", "Session") if mode != "legacy" else ("Session", "JobListSession")
    monkeypatch.setattr(ps, selected, session)
    monkeypatch.setattr(ps, other, lambda *_a, **_k: pytest.fail("wrong A3 session dispatch"))
    assert ps.main([]) == 7


def test_missing_controller_source_fails_before_cli(tmp_path):
    with pytest.raises(ps.SessionError, match="controller source is missing"):
        ps.controller_source_paths(tmp_path)


def test_negative_capacity_wait_is_rejected_before_sources_or_launch(tmp_path, monkeypatch):
    args, _ = make_args(tmp_path, ["--wait-capacity", "-1"])
    monkeypatch.setattr(ps, "build_parser", lambda: SimpleNamespace(parse_args=lambda _: args))
    with pytest.raises(ps.SessionError, match="must be non-negative"):
        ps.main([])


@pytest.mark.parametrize("list_result", [
    (1, "[]", "listing failed"),
    (0, "not JSON", ""),
    (0, "{}", ""),
    (0, "null", ""),
    (0, "[{}]", ""),
    (0, '[{"id": 123}]', ""),
])
def test_no_id_release_cannot_verify_failed_or_malformed_list(tmp_path, list_result):
    session, pod, code, _ = run_session(tmp_path, b"", launch_result=(1, "", "error: allocation_unknown"),
                                         list_result=list_result)
    assert code == 1 and len(pod.launches) == 1
    release = session.records[-1]
    assert release["result"] == "failed"
    detail = release["detail"]
    assert detail["status"] == "unresolved" and detail["reconciliation_required"] is True
    assert detail["pods"] is None and detail["get"] is None and detail["list"] is None
    assert detail["list_scope"] == "session" and detail["list_error"]
    assert detail["list_exit_code"] == list_result[0]
    assert not session.released and not pod.terminated and not pod.http_checks
    report = json.loads((tmp_path / "work" / "pod-session-progress.json").read_text())
    assert report["steps"][-1] == release


@pytest.mark.parametrize("list_result, listed_ids", [
    ((1, "[]", "listing failed"), None),
    ((0, "not JSON", ""), None),
    ((0, "[{}]", ""), None),
    ((0, '[{"id": "stillrunning", "name": "unneeded metadata"}]', ""), [{"id": "stillrunning"}]),
])
def test_known_pod_release_requires_verified_empty_account_list(tmp_path, video, list_result, listed_ids):
    session, pod, code, _ = run_session(tmp_path, video, list_result=list_result)
    assert code == 1 and pod.terminated and not session.released
    release = session.records[-1]
    assert release["result"] == "failed"
    detail = release["detail"]
    assert detail["pods"] == [pod.pod] and detail["get"] == 404  # actually observed, while list remains unknown
    assert detail["list"] == listed_ids and detail["list_scope"] == "account"
    if listed_ids is None:
        assert detail["list_error"]
    assert pod.http_checks == [pod.pod] * 12
    report = json.loads((tmp_path / "work" / "pod-session-progress.json").read_text())
    assert report["steps"][-1] == release


def test_container_disk_is_sized_from_the_recipe():
    footprint = ps.recipe_footprint_gb()
    assert 60 <= footprint <= 70
    assert ps.container_disk_gb(None, footprint) == 120
    assert ps.container_disk_gb(200, footprint) == 200
    with pytest.raises(ps.SessionError):
        ps.container_disk_gb(footprint, footprint)


def test_bundle_contents_and_size_check(tmp_path, video):
    session, pod, code, _ = run_session(tmp_path, video)
    assert code == 0
    with tarfile.open(tmp_path / "work" / "pod-src.tgz") as tar:
        names = tar.getnames()
    assert {n.split("/")[0] for n in names} == set(ps.BUNDLE_TREES) | {".pod-source.json"}
    assert "astrid-agent/scripts/gpu_sanity.py" in names
    assert "astrid-agent/tests/t.py" in names
    assert all(f"{name}/.git/HEAD" in names for name in ps.BUNDLE_TREES)
    assert not any("/.git/config.worktree" in n or "/.git/hooks/" in n for n in names)
    assert len(pod.puts[ps.BUNDLE_REMOTE]) == (tmp_path / "work" / "pod-src.tgz").stat().st_size


# ---- (b) failing steps stop the run and still release -----------------------------------------

AFTER = {
    "claim": [], "bundle": ["job:bootstrap"], "bootstrap": ["job:install"], "install": ["job:rerun"],
    "rerun": ["job:status", "job:s0"], "status": ["job:s0"], "s0": ["job:clip"], "clip": ["exec:sha256"],
    "download-sha": [], "receipt": ["exec:sha256"],
}


@pytest.mark.parametrize("fail", list(AFTER))
def test_failure_stops_and_releases(tmp_path, video, fail):
    session, pod, code, messages = run_session(tmp_path, video, fail=fail)
    assert code == 1
    failed = [r["step"] for r in session.records if r["result"] == "failed"]
    assert len(failed) == 1 and session.records[-1]["step"] == "release" and session.records[-1]["result"] == "ok"
    for event in AFTER[fail]:
        assert event not in pod.events
    if fail == "claim":
        assert not pod.terminated and "ready" not in pod.events and pod.starts == 0
    else:
        assert pod.terminated and pod.events.count("terminate") == 1
    assert (tmp_path / "work" / "pod-session-progress.json").exists()
    assert "POD SESSION FAILED" in messages[-1]


def test_download_probe_failure_releases(tmp_path, video):
    def bad_probe(path):
        return {"frames": 215, "rate": "24/1", "width": 1024, "height": 576, "audio": True}
    session, pod, code, _ = run_session(tmp_path, video, probe=bad_probe)
    assert code == 1 and pod.terminated
    assert "frames" in session.records[-2]["detail"]["error"]


def test_check_video_rejects_each_property():
    good = {"frames": 216, "rate": "24/1", "width": 1024, "height": 576, "audio": True}
    ps.check_video(good)
    for key, value in (("frames", 215), ("rate", "25/1"), ("width", 1023), ("height", 575), ("audio", False)):
        with pytest.raises(ps.SessionError):
            ps.check_video(good | {key: value})


def test_release_failure_is_a_failed_run(tmp_path, video):
    args, agent = make_args(tmp_path)
    pod = FakePod(video)
    pod.http_status = lambda pod_id: 200
    messages = []
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         sleep=lambda s: None, say=messages.append, agent_root=agent)
    assert session.run() == 1
    assert session.records[-1]["step"] == "release" and session.records[-1]["result"] == "failed"
    assert any("terminate it by hand" in m for m in messages)


def test_interrupt_still_releases(tmp_path, video):
    args, agent = make_args(tmp_path)
    pod = FakePod(video)
    original = pod.detached_start

    def boom(command, **kw):
        if "pod_bootstrap.sh" in command:
            raise KeyboardInterrupt
        return original(command, **kw)

    pod.detached_start = boom
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         sleep=lambda s: None, say=lambda m: None, agent_root=agent)
    with pytest.raises(KeyboardInterrupt):
        session.run()
    assert pod.terminated


# ---- (c) job budget ---------------------------------------------------------------------------

def test_budget_refuses_the_record_that_reaches_100():
    budget = ps.JobBudget()
    for _ in range(99):
        budget.charge("x")
    assert budget.count == 99
    with pytest.raises(ps.BudgetExceeded, match="job budget"):
        budget.charge("one too many")
    assert budget.count == 99
    assert ps.JOB_LIMIT == 100


def test_session_aborts_before_100_and_releases(tmp_path, video):
    budget = ps.JobBudget(count=96)
    session, pod, code, messages = run_session(tmp_path, video, budget=budget)
    assert code == 1 and budget.count <= 99
    assert pod.starts + pod.executes == budget.count - 96
    assert "job:install" not in pod.events and pod.terminated
    failed = [r for r in session.records if r["result"] == "failed"][0]
    assert "job budget" in failed["detail"]["error"] and "100" in failed["detail"]["error"]
    assert any("job budget" in m for m in messages)


def test_normal_run_stays_far_below_the_budget(tmp_path, video):
    session, pod, code, _ = run_session(tmp_path, video)
    assert code == 0 and session.budget.count < 30


# ---- (d) request derivation -------------------------------------------------------------------

def test_clip_and_s0_requests_are_derived(tmp_path):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(REQUEST, indent=2))
    clip, smoke = ps.derive_requests(path)
    assert clip == path.read_bytes()
    clip_json, smoke_json = json.loads(clip), json.loads(smoke)
    assert clip_json["output"]["duration"] == 9 and smoke_json["output"]["duration"] == 1
    assert smoke_json | {"output": clip_json["output"]} == clip_json
    assert list(smoke_json) == list(clip_json)
    for bad in (1, 8, 9.5, "9", None):
        path.write_text(json.dumps(REQUEST | {"output": {"duration": bad}}))
        with pytest.raises(ps.SessionError):
            ps.derive_requests(path)


def test_uploaded_requests_and_asset_map(tmp_path, video):
    session, pod, code, _ = run_session(tmp_path, video)
    assert code == 0
    assert json.loads(pod.puts[f"{ps.CLIP_DIR}/request.json"])["output"]["duration"] == 9
    assert json.loads(pod.puts[f"{ps.CLIP_DIR}/smoke-request.json"])["output"]["duration"] == 1
    amap = json.loads(pod.puts[f"{ps.CLIP_DIR}/asset-map.json"])
    assert amap == {"img1": "/root/clip/inputs/img1.png", "img2": "/root/clip/inputs/img2.png"}
    assert all(path in pod.puts for path in amap.values())
    s0, clip = job_commands(pod, "s0")[0], job_commands(pod, "clip")[0]
    assert "--request /root/clip/smoke-request.json" in s0 and "--request /root/clip/request.json" in clip
    outs = [re.search(r"--out (\S+)", c).group(1) for c in (s0, clip)]
    assert outs[0] != outs[1] and all("out-" in o for o in outs)
    for command in (s0, clip):  # the action runs with the pod's own environment: only PATH gains the venv bin
        assert re.findall(r"^export .*$", command, re.M) == ["export PATH=/opt/astrid/bin:$PATH"]
        assert "ASTRID_ENGINE" not in command and "--placement local" in command
        assert "if [ -e " in command  # a fresh out dir is required


def test_asset_map_must_cover_the_request(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"img1": str(tmp_path / "a.png")}))
    with pytest.raises(ps.SessionError, match="img2"):
        ps.remote_asset_map(tmp_path / "m.json", REQUEST)


# ---- (e) sanity command and the remote scripts ---------------------------------------------------

def test_install_sets_the_gpu_sanity_command(tmp_path, video):
    session, pod, code, _ = run_session(tmp_path, video)
    assert code == 0
    install = job_commands(pod, "install")[0]
    sanity = "/opt/astrid/bin/python /root/bundle/src/astrid-agent/scripts/gpu_sanity.py"
    assert f"export ASTRID_ENGINE_SANITY_CMD='{sanity}'" in install
    assert ("--root /root/astrid/engines/vibecomfy-h3" in install and "--deadline 14400" in install
            and "engines install vibecomfy-h3 --placement local" in install)
    # the export comes before the install command, and the script the command names ships in this repo
    assert install.index("ASTRID_ENGINE_SANITY_CMD") < install.index("astrid engines install")
    assert (SCRIPT.parent / "gpu_sanity.py").is_file()
    assert "ASTRID_ENGINE_SANITY_CMD" in job_commands(pod, "rerun")[0]
    assert "ASTRID_ENGINE_SANITY_CMD" not in job_commands(pod, "status")[0]


def test_bootstrap_job_environment(tmp_path, video):
    session, pod, code, _ = run_session(tmp_path, video)
    boot = job_commands(pod, "bootstrap")[0]
    assert "export ASTRID_BOOTSTRAP_ROOT=/root/astrid/bootstrap ASTRID_REALM_ROOT=/root/realm ASTRID_VENV=/opt/astrid" in boot
    assert boot.index("tar xzf") < boot.index("pod_bootstrap.sh") and "set -eu" in boot


def test_rerun_with_an_executed_step_fails(tmp_path, video):
    session, pod, code, _ = run_session(tmp_path, video, fail="rerun")
    assert code == 1 and "step(s) executed" in [r for r in session.records if r["result"] == "failed"][0]["detail"]["error"]


def test_parse_json_object_takes_the_last_object():
    assert ps.parse_json_object('log line\n{"a": 1}\nmore\n{"b": 2}\n') == {"b": 2}
    with pytest.raises(ps.SessionError):
        ps.parse_json_object("null\n")


def test_capacity_progress_streams_before_cli_exits_and_rejects_untrusted_fields(tmp_path):
    import threading

    args, agent = make_args(tmp_path, ["--wait-capacity", "120"])
    pod = FakePod(b"")
    release = tmp_path / "release"
    executable = tmp_path / "fake-lifecycle"
    executable.write_text(f'''#!{sys.executable}
import json, sys, time
# Large outputs verify progress is not blocked by undrained subprocess pipes.
sys.stdout.write(" " * 200000 + "\\n" + json.dumps({{"pod_id": "abc123podid"}}))
sys.stdout.flush()
sys.stderr.write("x" * 200000 + '\\nRUNPOD_CAPACITY_WAIT {{"placement_attempt":7,"retry_interval":30,"wait_seconds":120}}\\n')
sys.stderr.flush()
fd = int(sys.argv[sys.argv.index("--capacity-progress-fd") + 1])
progress = __import__("os").fdopen(fd, "w")
for line in [
    'RUNPOD_PLACEMENT_ATTEMPT {{"placement_attempt":1}}',
    'RUNPOD_CAPACITY_WAIT {{"placement_attempt":1,"retry_interval":30,"wait_seconds":120}}',
]:
    print(line, file=progress, flush=True)
deadline = time.monotonic() + 15
while not __import__("pathlib").Path({str(release)!r}).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
print('RUNPOD_PLACEMENT_ATTEMPT {{"placement_attempt":2}}', file=progress, flush=True)
''')
    executable.chmod(0o700)
    messages, ready, result = [], threading.Event(), []

    def say(message):
        messages.append(message)
        if "capacity retry after attempt" in message:
            ready.set()

    session = ps.Session(args, cli=offline_launch_cli(executable, pod), transport_factory=pod.factory,
                         http_status=pod.http_status, agent_root=agent, say=say)
    def claim():
        try:
            result.append(session.claim())
        except Exception as exc:
            result.append(exc)

    thread = threading.Thread(target=claim, daemon=True)
    thread.start()
    try:
        assert ready.wait(10), "retry event was not delivered while the subprocess ran"
        assert thread.is_alive()
        progress = (Path(args.workdir) / "pod-session-progress.json").read_text()
        attempt = json.loads(progress)["claim_attempt"]
        assert (attempt["placement_attempt"], attempt["retry_interval"], attempt["wait_seconds"]) == (1, 30, 120)
        assert sum("capacity retry after attempt" in msg for msg in messages) == 1
        for forbidden in ("synthetic-secret", "private-provider-id", "provider error"):
            assert forbidden not in progress and forbidden not in "\n".join(messages)
    finally:
        release.touch()
        thread.join(10)
    assert not thread.is_alive()
    assert len(result) == 1 and isinstance(result[0], dict), result


def test_terminal_capacity_error_is_not_echoed(tmp_path):
    raw = ("error: Failed to launch pod across GPU candidates ['private-provider-id']: "
           "no longer any instances available synthetic-secret")
    _, _, _, messages = run_session(tmp_path, b"", launch_result=(1, "", raw))
    assert "private-provider-id" not in "\n".join(messages)
    assert "synthetic-secret" not in "\n".join(messages)


def test_wait_capacity_help_describes_per_placement_total():
    help_text = " ".join(ps.build_parser().format_help().split())
    assert "each GPU/datacenter placement" in help_text
    assert "total time can exceed this across placements" in help_text


@pytest.mark.parametrize("wait", [0, 120])
@pytest.mark.parametrize("typed_capacity", [False, True])
def test_only_private_capacity_exhaustion_advances_placement(tmp_path, wait, typed_capacity):
    args, agent = make_args(tmp_path, ["--wait-capacity", str(wait)])
    pod, messages = FakePod(b""), []
    count = tmp_path / "launch-count"
    executable = tmp_path / "fake-lifecycle"
    executable.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
counter = pathlib.Path({str(count)!r})
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
fd = int(sys.argv[sys.argv.index("--capacity-progress-fd") + 1])
if n == 1:
    print("RUNPOD_CAPACITY_EXHAUSTED", file=sys.stderr)
    print("error: Failed to launch pod across GPU candidates ['private-provider-id']: no longer any instances available", file=sys.stderr)
    if {typed_capacity!r}:
        os.write(fd, b'RUNPOD_PLACEMENT_ATTEMPT {{"placement_attempt":1}}\\n')
        os.write(fd, b"RUNPOD_CAPACITY_EXHAUSTED\\n")
    sys.exit(1)
print(json.dumps({{"pod_id": "abc123podid"}}))
''')
    executable.chmod(0o700)
    session = ps.Session(args, cli=offline_launch_cli(executable, pod), transport_factory=pod.factory,
                         http_status=pod.http_status, agent_root=agent, say=messages.append)
    if typed_capacity:
        assert session.claim()["pod_id"] == pod.pod
        assert count.read_text() == "2"
    else:
        with pytest.raises(ps.SessionError, match="non-capacity"):
            session.claim()
        assert count.read_text() == "1"
    assert "private-provider-id" not in "\n".join(messages)


def test_untyped_injected_runner_cannot_advance_on_capacity_text(tmp_path):
    args, agent = make_args(tmp_path, ["--wait-capacity", "120"])
    pod = FakePod(b"")
    calls = []
    def runner(argv, env):
        calls.append(argv)
        return 1, "", "error: Failed to launch pod across GPU candidates ['fake']: no longer any instances available"
    session = ps.Session(args, cli=runner, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=lambda _: None)
    with pytest.raises(ps.SessionError, match="non-capacity"):
        session.claim()
    assert len(calls) == 1


def test_real_lifecycle_hook_reaches_session_before_retry(tmp_path):
    import threading

    args, agent = make_args(tmp_path, ["--wait-capacity", "120"])
    pod = FakePod(b"")
    release = tmp_path / "release"
    executable = tmp_path / "offline-lifecycle"
    executable.write_text(f'''#!{sys.executable}
import pathlib, time
from runpod_lifecycle import cli, lifecycle
from runpod_lifecycle.config import RunPodConfig
from runpod_lifecycle.errors import ProviderCapacityUnavailable
from runpod_lifecycle.pod import Pod
cli._resolve_config = lambda parsed: RunPodConfig(api_key="synthetic", gpu_type=parsed.gpu_type,
    ram_tiers_enabled=False, transport="https", https_token="synthetic", storage_volumes=(), storage_name=None)
lifecycle.find_gpu_type = lambda *args: {{"id": "fake"}}
calls = 0
def create(*args, **kwargs):
    global calls
    calls += 1
    if calls == 1:
        raise ProviderCapacityUnavailable(200, "no longer any instances available synthetic-secret private-provider-id")
    return {{"id": "abc123podid"}}
lifecycle.create_pod = create
async def sleep(seconds):
    deadline = time.monotonic() + 15
    while not pathlib.Path({str(release)!r}).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
lifecycle.asyncio.sleep = sleep
async def ready(self, timeout=0):
    pass
Pod.wait_ready = ready
raise SystemExit(cli.main())
''')
    executable.chmod(0o700)
    ready, messages, result = threading.Event(), [], []
    def say(message):
        messages.append(message)
        if "capacity retry after attempt" in message:
            ready.set()
    session = ps.Session(args, cli=offline_launch_cli(executable, pod), transport_factory=pod.factory,
                         http_status=pod.http_status, agent_root=agent, say=say)
    def claim():
        try:
            result.append(session.claim())
        except Exception as exc:
            result.append(exc)
    thread = threading.Thread(target=claim, daemon=True)
    thread.start()
    try:
        assert ready.wait(10), result
        assert thread.is_alive()
        progress = (Path(args.workdir) / "pod-session-progress.json").read_text()
        attempt = json.loads(progress)["claim_attempt"]
        assert attempt["placement_attempt"] == 1 and attempt["wait_seconds"] == 120
        assert 0 < attempt["retry_interval"] <= 30
        for forbidden in ("synthetic-secret", "private-provider-id", "no longer any instances available"):
            assert forbidden not in progress and forbidden not in "\n".join(messages)
    finally:
        release.touch()
        thread.join(10)
    assert not thread.is_alive()
    assert len(result) == 1 and isinstance(result[0], dict), result


@pytest.mark.parametrize("volume_exists", [False, True])
def test_fixed_storage_through_lifecycle_never_resizes_or_falls_back(tmp_path, monkeypatch, volume_exists):
    """Real CLI/config/lifecycle, with all provider boundaries replaced locally."""
    from runpod_lifecycle import cli, lifecycle
    from runpod_lifecycle.pod import Pod

    monkeypatch.setenv("RUNPOD_STORAGE_NAME", "inherited-other")
    monkeypatch.setenv("RUNPOD_STORAGE_VOLUMES", "alternate-a,alternate-b")
    monkeypatch.setenv("RUNPOD_ATTACH_ONLY", "false")
    args, agent = make_args(tmp_path, ["--volume-gb", "999999"])
    pod = FakePod(b"")
    lookup = Mock(return_value="synthetic-storage-id" if volume_exists else None)
    create = Mock(return_value={"id": pod.pod})
    resize = Mock(side_effect=AssertionError("attach-only must never resize"))
    monkeypatch.setattr(cli, "_resolve_api_key", lambda _args: "synthetic")
    monkeypatch.setattr(lifecycle, "get_storage_volume_id", lookup)
    monkeypatch.setattr(lifecycle, "check_and_expand_storage", resize)
    monkeypatch.setattr(lifecycle, "find_gpu_type", lambda *_: {"id": "synthetic-gpu"})
    monkeypatch.setattr(lifecycle, "create_pod", create)
    monkeypatch.setattr(Pod, "wait_ready", AsyncMock())
    configs = []
    resolve = cli._resolve_config

    def runner(argv, env):
        if argv[0] != "launch":
            return pod.cli(argv, env)
        pod.launches.append(argv)
        with monkeypatch.context() as scope:
            for key in ("RUNPOD_STORAGE_NAME", "RUNPOD_STORAGE_VOLUMES", "RUNPOD_ATTACH_ONLY", "RUNPOD_HTTPS_TOKEN_FILE"):
                scope.setenv(key, env[key])
            parsed = cli.build_parser().parse_args(["--transport", "https", *argv])
            config = resolve(parsed)
            configs.append(config)
            scope.setattr(cli, "_resolve_config", lambda _args: config)
            stdout = io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout):
                    return asyncio.run(cli._cmd_launch(parsed)), stdout.getvalue(), ""
            except Exception as exc:
                return 1, stdout.getvalue(), f"error: {exc}"

    session = ps.Session(args, cli=runner, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=lambda _: None)
    if volume_exists:
        assert session.claim()["pod_id"] == pod.pod
        assert create.call_count == 1
        assert create.call_args.kwargs["network_volume_id"] == "synthetic-storage-id"
    else:
        with pytest.raises(ps.SessionError, match="non-capacity"):
            session.claim()
        create.assert_not_called()
        assert not pod.starts and "ready" not in pod.events
    assert len(pod.launches) == len(configs) == 1
    assert configs[0].attach_only and configs[0].storage_name == ps.STORAGE_NAME
    assert configs[0].storage_volumes == ()
    assert pod.launches[0][pod.launches[0].index("--datacenter-id") + 1] == ps.DATACENTER
    lookup.assert_called_once_with("synthetic", ps.STORAGE_NAME)
    resize.assert_not_called()


@pytest.mark.parametrize("failure", ["volume-missing", "volume-ambiguous", "attachment-missing", "attachment-wrong",
                                      "mount-missing", "mount-wrong", "capacity", "runtime-identity", "runtime-unready"])
def test_storage_and_live_runtime_fail_before_model_install_and_release(tmp_path, failure):
    session, pod, code, _ = run_session(tmp_path, b"", fail=failure)
    assert code == 1 and pod.terminated and session.released
    assert "job:install" not in pod.events and "job:rerun" not in pod.events
    assert session.records[-1]["step"] == "release" and session.records[-1]["result"] == "ok"
    if failure.startswith(("volume", "attachment", "mount")) or failure == "capacity":
        assert "job:bootstrap" not in pod.events
    else:
        assert "job:bootstrap" in pod.events and "job:runtime-ready" in pod.events


@pytest.mark.parametrize("case", [
    "exact", "opaque", "mount-missing", "mount-duplicate", "mount-device", "mount-malformed",
    "capacity", "missing", "null", "wrong", "empty-id", "numeric-id", "list-id", "missing-id",
    "wrong-type", "wrong-path", "readonly", "readonly-int", "duplicate", "duplicate-path",
    "absent-pod", "wrong-pod", "malformed-collection", "malformed-row", "malformed-json",
    "unavailable", "timeout", "stale", "unjoinable", "duplicate-json", "legacy-conflict", "legacy-malformed", "legacy-path", "legacy-object",
])
def test_provider_attachment_through_real_lifecycle_json_guards_probe_bundle_install(
    tmp_path, monkeypatch, video, case,
):
    from runpod_lifecycle import api, cli
    import runpod
    from runpod.api import ctl_commands

    args, agent = make_args(tmp_path)
    pod = FakePod(video)
    item = {"volumeId":"vol187", "volumeType":"NETWORK_VOLUME", "mountPath":"/workspace", "readOnly":False}
    observed = {"id":pod.pod, "volumeMounts":[item]}
    if case == "missing": observed.pop("volumeMounts")
    if case == "null": observed["volumeMounts"] = None
    if case == "wrong": item["volumeId"] = "wrong"
    if case == "empty-id": item["volumeId"] = ""
    if case == "numeric-id": item["volumeId"] = 187
    if case == "list-id": item["volumeId"] = ["vol187"]
    if case == "missing-id": item.pop("volumeId")
    if case == "wrong-type": item["volumeType"] = "CONTAINER_DISK"
    if case == "wrong-path": item["mountPath"] = "/other"
    if case == "readonly": item["readOnly"] = True
    if case == "readonly-int": item["readOnly"] = 0
    if case == "duplicate": observed["volumeMounts"] *= 2
    if case == "duplicate-path": observed["volumeMounts"].append({**item,"volumeId":"other"})
    if case == "wrong-pod": observed["id"] = "otherpod"
    if case == "malformed-row": observed["volumeMounts"] = ["PRIVATE_ROW"]
    graphql_calls = []
    def graphql(query, api_key=None):
        graphql_calls.append(query)
        assert "volumeMounts" in query and "networkVolumeId" not in query and "objectStores" not in query
        assert f'"{pod.pod}"' in query
        assert "ready" in pod.events and not pod.puts and not pod.starts
        if case == "timeout": raise TimeoutError("PRIVATE_TIMEOUT")
        if case == "unavailable": raise RuntimeError("PRIVATE_UNAVAILABLE")
        if case == "malformed-json": return "PRIVATE_NON_JSON"
        if case == "malformed-collection": return {"data":{"pod":[observed]}}
        return {"data":{"pod":None if case == "absent-pod" else observed}}
    monkeypatch.setattr(ctl_commands, "run_graphql_query", graphql)
    monkeypatch.setattr(api, "runpod", runpod)
    monkeypatch.setattr(cli, "_resolve_api_key", lambda _: "synthetic")
    original_execute = pod.execute_command
    def execute(command, timeout=600):
        if "def storage_probe(" not in command:
            return original_execute(command, timeout=timeout)
        pod.executes += 1
        pod.events.append("exec:storage")
        _, _, source, blob = shlex.split(command)
        namespace = {"__name__":"transported_storage_probe"}
        exec(compile(source, "transported-pod-storage-probe.py", "exec"), namespace)
        row = "102 93 0:47 / /workspace rw,PRIVATE_OPTION shared:30 - nfs4 opaque:/PRIVATE_SOURCE rw\n"
        if case == "mount-missing": row = ""
        if case == "mount-duplicate": row *= 2
        if case == "mount-device": row = row.replace("0:47", "0:99")
        if case == "mount-malformed": row = "102 93 0:47 / /workspace rw - nfs4\n"
        original_read, original_lstat, original_stat, original_exists = Path.read_text, Path.lstat, Path.stat, Path.exists
        def read(path, *a, **kw):
            return row if path == Path("/proc/self/mountinfo") else original_read(path,*a,**kw)
        def lstat(path,*a,**kw):
            if path == Path("/workspace"): return SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_dev=os.makedev(0,47))
            if Path("/workspace") in path.parents: raise FileNotFoundError(str(path))
            return original_lstat(path,*a,**kw)
        def exists(path):
            if path == Path("/root"): return True
            if Path("/root") in path.parents: return False
            return original_exists(path)
        def path_stat(path,*a,**kw):
            return SimpleNamespace(st_dev=os.makedev(0,48)) if path == Path("/root") else original_stat(path,*a,**kw)
        def statvfs(path):
            return SimpleNamespace(f_bavail=1 if case == "capacity" else 10**15,f_frsize=1,f_blocks=10**15)
        stdout,stderr=io.StringIO(),io.StringIO()
        with monkeypatch.context() as patch:
            patch.setattr(Path,"read_text",read); patch.setattr(Path,"lstat",lstat)
            patch.setattr(Path,"exists",exists); patch.setattr(Path,"stat",path_stat)
            patch.setattr(Path,"is_symlink",lambda _:False); patch.setattr(os,"statvfs",statvfs)
            patch.setattr(sys,"argv",["-c",blob])
            with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr): code=namespace["main"]()
        return code,stdout.getvalue(),stderr.getvalue()+"PRIVATE_UNSTRUCTURED\n"
    monkeypatch.setattr(pod,"execute_command",execute)
    def runner(argv,env,**kwargs):
        if argv[0] == "mounts":
            pod.events.append("mounts")
            stdout=io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout): code=asyncio.run(cli._cmd_mounts(cli.build_parser().parse_args(argv)))
                result=json.loads(stdout.getvalue()) if code == 0 else None
                if case == "stale": result["nonce"]="f"*32
                if case == "unjoinable": result["pod_id"]="otherpod"
                if case == "duplicate-json":
                    return code, json.dumps(result)[:-1] + ',"pod_id":"otherpod"}', ""
                return code,json.dumps(result) if result is not None else "",""
            except Exception:
                return 1,"","provider observation failed"
        code,out,err=pod.cli(argv,env,**kwargs)
        if argv == ["list","--json"] and not pod.terminated:
            result=json.loads(out)
            if case == "legacy-conflict": result[0]["network_volume_id"]="other"
            if case == "legacy-malformed": result[0]["network_volume_evidence"]["valid"]=False
            if case == "legacy-path": result[0]["network_volume_mount_path"]="/wrong"
            if case == "legacy-object": result[0]["legacy_object_mounts"]=[{"volume_id":"object1","mount_path":"/objects","read_only":True}]
            return code,json.dumps(result),err
        return code,out,err
    session=ps.Session(args,cli=runner,transport_factory=pod.factory,http_status=pod.http_status,
                       agent_root=agent,sleep=lambda _:None,say=lambda _:None)
    code=session.run()
    assert len(pod.launches)==1 and pod.terminated and session.released
    assert len(graphql_calls)==(0 if case in ("legacy-conflict","legacy-malformed","legacy-path") else 1)
    if case in ("exact","opaque"):
        assert code==0 and session._cache_binding["cache_device"]==os.makedev(0,47)
        assert session._cache_binding["cache_mount_id"]==102
        assert pod.events.index("ready") < pod.events.index("mounts") < pod.events.index("exec:storage") < pod.events.index("job:install")
    else:
        assert code==1 and ps.BUNDLE_REMOTE not in pod.puts
        assert "job:bootstrap" not in pod.events and "job:install" not in pod.events
        if not case.startswith("mount-") and case != "capacity": assert "exec:storage" not in pod.events
    retained=json.dumps(session.state)+(session.workdir/"pod-session-progress.json").read_text()
    assert all(value not in retained for value in ("PRIVATE_","volume_mounts","provider_mapping","opaque:/","cache_device","cache_mount_id"))




def test_cache_mount_and_live_handshake_order_are_bound_to_claim(tmp_path, video):
    session, pod, code, messages = run_session(tmp_path, video)
    assert code == 0, messages
    sequence = ["ready", "volumes", "attachment", "exec:storage", "job:bootstrap", "job:runtime-ready", "job:install"]
    positions = [pod.events.index(e) for e in sequence]
    assert positions == sorted(positions)
    assert session.state["storage"]["pod_id"] == pod.pod
    assert session.state["runtime_ready"]["volume_id"] == session.state["storage"]["volume_id"]
    assert session.state["runtime_ready"]["runtime_lock_sha256"] == json.loads((session.workdir / "source-identity.json").read_text())["runtime_lock_sha256"]
    install = job_commands(pod, "install")[0]
    assert "--model-cache-root /workspace/astrid/engines/vibecomfy-h3/models" in install
    assert "--root /root/astrid/engines/vibecomfy-h3" in install


def test_synthetic_successful_storage_observation_is_private_and_in_progress(tmp_path, video, monkeypatch):
    args, agent = make_args(tmp_path)
    pod = FakePod(video)
    original_execute = pod.execute_command
    observation = {"schema": "astrid.pod-storage-diagnostic.v1", "stage": "complete",
                   "reason": "verified", "identity_matches": True}
    def execute(command, timeout=600):
        code, out, err = original_execute(command, timeout=timeout)
        if "def storage_probe(" in command:
            err = "PRIVATE_ENV=do-not-retain\nASTRID_STORAGE_DIAGNOSTIC " + json.dumps(observation) + "\n"
        return code, out, err
    monkeypatch.setattr(pod, "execute_command", execute)
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, sleep=lambda _: None, say=lambda _: None)
    assert session.run() == 0 and session.released
    artifact = session.workdir / "storage-mount-observation.json"
    assert json.loads(artifact.read_text()) == observation
    assert artifact.stat().st_mode & 0o777 == 0o600
    progress = (session.workdir / "pod-session-progress.json").read_text()
    assert json.loads(progress)["storage_diagnostic"] == observation
    assert "PRIVATE_ENV" not in progress


@pytest.mark.parametrize("case", ["malformed", "duplicate", "oversize", "extra-field", "wrong-mount", "control", "wrong-type", "duplicate-key", "stage-type", "reason-type", "identity-type", "schema", "source", "stage", "reason"])
def test_storage_diagnostic_parser_rejects_unallowlisted_records(case):
    observation = {"schema": "astrid.pod-storage-diagnostic.v1", "stage": "identity",
                   "reason": "mismatch", "identity_matches": False}
    if case == "extra-field": observation["environment"] = "PRIVATE_VALUE"
    if case == "wrong-mount": observation["mounts"] = [{"mountpoint": "/unrelated"}]
    if case == "control": observation["reason"] = "mismatch\x1b[1m"
    if case == "wrong-type": observation = [None]
    if case == "stage-type": observation["stage"] = ["identity"]
    if case == "reason-type": observation["reason"] = {"private": "value"}
    if case == "identity-type": observation["identity_matches"] = 1
    if case == "schema": observation["schema"] = "unknown"
    if case == "source": observation["source"] = "PRIVATE_RAW_SOURCE"
    if case == "stage": observation["stage"] = "PRIVATE_RAW_SOURCE"
    if case == "reason": observation["reason"] = "PRIVATE_RAW_SOURCE"
    text = "ASTRID_STORAGE_DIAGNOSTIC " + json.dumps(observation) + "\n"
    if case == "malformed": text = "ASTRID_STORAGE_DIAGNOSTIC {bad json}\n"
    if case == "duplicate": text *= 2
    if case == "oversize": text = "ASTRID_STORAGE_DIAGNOSTIC " + "x" * 5000
    if case == "duplicate-key": text = text.replace('"identity_matches": false', '"identity_matches": true, "identity_matches": false')
    assert ps.parse_storage_diagnostic("PRIVATE_ENV=do-not-retain\n" + text) is None


def test_mount_diagnostic_parser_redacts_source_again():
    # The new bounded schema is stricter: even redacted source fields are rejected.
    observation = {"schema": "astrid.pod-storage-diagnostic.v1", "stage": "identity",
                   "reason": "mismatch", "identity_matches": False,
                   "source": "https://USER:PRIVATE_PASSWORD@host/vol187?token=PRIVATE_QUERY#PRIVATE_FRAGMENT"}
    assert ps.parse_storage_diagnostic("ASTRID_STORAGE_DIAGNOSTIC " + json.dumps(observation)) is None


@pytest.mark.parametrize("case,stage,reason,identity", [
    ("match", "complete", "verified", True),
    ("mismatch", "identity", "mismatch", False),
    ("malformed", "mountinfo", "malformed", None),
    ("capacity", "capacity", "insufficient", True),
    ("local", "local", "insufficient", True),
])
def test_transported_storage_diagnostic_is_bounded_and_persisted(tmp_path, video, monkeypatch, case, stage, reason, identity):
    args, agent = make_args(tmp_path)
    pod = FakePod(video)
    original_execute = pod.execute_command
    raw_source = "mfs#vol187/PRIVATE_RAW_SOURCE" if case != "mismatch" else "mfs#opaque/PRIVATE_RAW_SOURCE"
    row = f"102 93 0:47 / /workspace rw,PRIVATE_MOUNT_OPTION shared:30 - fuse.mfs {raw_source} rw,PRIVATE_SUPER_OPTION\n"
    if case == "malformed":
        row = "102 93 0:47 / /workspace rw - fuse.mfs\n"

    def execute(command, timeout=600):
        if "def storage_probe(" not in command:
            return original_execute(command, timeout=timeout)
        pod.executes += 1
        pod.events.append("exec:storage")
        _, _, source, blob = shlex.split(command)
        if case == "mismatch":
            request = json.loads(base64.b64decode(blob))
            request["provider_mapping"]["volume_mounts"][0]["volume_id"] = "other"
            blob = base64.b64encode(json.dumps(request).encode()).decode()
        namespace = {"__name__": "transported_storage_probe"}
        exec(compile(source, "transported_storage_probe.py", "exec"), namespace)
        original_read, original_lstat, original_stat, original_exists = Path.read_text, Path.lstat, Path.stat, Path.exists
        device = os.makedev(0, 47)
        def read(path, *a, **kw):
            return row if path == Path("/proc/self/mountinfo") else original_read(path, *a, **kw)
        def lstat(path, *a, **kw):
            if path == Path("/workspace"):
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_dev=device)
            if str(path).startswith("/workspace/"):
                raise FileNotFoundError(str(path))
            return original_lstat(path, *a, **kw)
        def exists(path):
            if path == Path("/root"):
                return True
            if path == Path("/root/astrid") or Path("/root/astrid") in path.parents:
                return False
            return original_exists(path)
        def path_stat(path, *a, **kw):
            return SimpleNamespace(st_dev=os.makedev(0, 48)) if path == Path("/root") else original_stat(path, *a, **kw)
        def statvfs(path):
            short = case == "capacity" and path == Path("/workspace") or case == "local" and path == Path("/root")
            return SimpleNamespace(f_bavail=1 if short else 10**15, f_frsize=1, f_blocks=10**15)
        stdout, stderr = io.StringIO(), io.StringIO()
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", read)
            patch.setattr(Path, "lstat", lstat)
            patch.setattr(Path, "is_symlink", lambda path: False)
            patch.setattr(Path, "exists", exists)
            patch.setattr(Path, "stat", path_stat)
            patch.setattr(os, "statvfs", statvfs)
            patch.setattr(sys, "argv", ["-c", blob])
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = namespace["main"]()
        assert raw_source not in stdout.getvalue() + stderr.getvalue()
        assert "PRIVATE_" not in stdout.getvalue() + stderr.getvalue()
        return code, stdout.getvalue(), stderr.getvalue() + "PRIVATE_UNSTRUCTURED_OUTPUT\n"

    monkeypatch.setattr(pod, "execute_command", execute)
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, sleep=lambda _: None, say=lambda _: None)
    code = session.run()
    assert code == (0 if case == "match" else 1) and pod.terminated and session.released
    if case != "match":
        assert ps.BUNDLE_REMOTE not in pod.puts
        assert "job:bootstrap" not in pod.events and "job:install" not in pod.events
    path = session.workdir / "storage-mount-observation.json"
    diagnostic = json.loads(path.read_text())
    assert diagnostic == {"schema": "astrid.pod-storage-diagnostic.v1", "stage": stage, "reason": reason,
                          "identity_matches": identity}
    assert path.stat().st_mode & 0o777 == 0o600
    progress = (session.workdir / "pod-session-progress.json").read_text()
    assert json.loads(progress)["storage_diagnostic"] == diagnostic
    remote_progress = pod.files[ps.PROGRESS_REMOTE].decode()
    assert json.loads(remote_progress)["storage_diagnostic"] == diagnostic
    retained = path.read_text() + progress + remote_progress + json.dumps(session.state)
    assert raw_source not in retained and "PRIVATE_" not in retained


def test_storage_diagnostic_parser_accepts_bounded_stage_errors():
    for stage in ("mountinfo", "paths", "mount", "cache", "capacity", "local", "unknown"):
        value = {"schema": "astrid.pod-storage-diagnostic.v1", "stage": stage, "reason": "error", "identity_matches": True}
        assert ps.parse_storage_diagnostic("ASTRID_STORAGE_DIAGNOSTIC " + json.dumps(value)) == value


@pytest.mark.parametrize("case", ["total", "source", "options", "mount-evidence", "request-type", "total-under-free"])
def test_storage_receipt_rejects_private_total_bytes(tmp_path, video, monkeypatch, case):
    args, agent = make_args(tmp_path)
    pod = FakePod(video)
    original_execute = pod.execute_command
    def execute(command, timeout=600):
        code, out, err = original_execute(command, timeout=timeout)
        if "def storage_probe(" in command:
            receipt = json.loads(out)
            if case == "total": receipt["total_bytes"] = "PRIVATE_RAW_SOURCE"
            if case == "source": receipt["source"] = "PRIVATE_RAW_SOURCE"
            if case == "options": receipt["options"] = "PRIVATE_MOUNT_OPTION"
            if case == "mount-evidence": receipt["mount_evidence"] = {"source": "PRIVATE_RAW_SOURCE"}
            if case == "request-type": receipt["required_volume_bytes"] = float(receipt["required_volume_bytes"])
            if case == "total-under-free": receipt["total_bytes"] = receipt["free_bytes"] - 1
            out = json.dumps(receipt)
        return code, out, err
    monkeypatch.setattr(pod, "execute_command", execute)
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, sleep=lambda _: None, say=lambda _: None)
    assert session.run() == 1 and session.released
    assert "exec:storage" in pod.events and ps.BUNDLE_REMOTE not in pod.puts
    retained = (session.workdir / "pod-session-progress.json").read_text() + json.dumps(session.state)
    assert "storage" not in session.state and "PRIVATE_" not in retained


def test_reused_workdir_cannot_retain_stale_storage_observation(tmp_path, video, monkeypatch):
    args, agent = make_args(tmp_path)
    workdir = Path(args.workdir)
    workdir.mkdir()
    artifact = workdir / "storage-mount-observation.json"
    artifact.write_text('{"stage": "complete", "identity_matches": true}')
    pod = FakePod(video)
    original_execute = pod.execute_command

    def execute(command, timeout=600):
        if "def storage_probe(" in command:
            return 1, "", "ASTRID_STORAGE_DIAGNOSTIC {invalid"
        return original_execute(command, timeout=timeout)

    monkeypatch.setattr(pod, "execute_command", execute)
    session = ps.Session(args, cli=pod.cli, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, sleep=lambda _: None, say=lambda _: None)
    assert not artifact.exists()
    assert session.run() == 1 and session.released
    assert not artifact.exists()
    assert json.loads((workdir / "pod-session-progress.json").read_text())["storage_diagnostic"] is None
    assert ps.BUNDLE_REMOTE not in pod.puts


def test_persistent_cache_survives_verified_release_and_revalidates_on_fresh_launch(tmp_path, monkeypatch, video):
    """Real transported probe and pinned downloader; provider/Runtime/GPU are CPU fakes."""
    import time
    from astrid.core.generation import model_downloader as downloader
    from astrid.core.generation.engine_contracts import ModelSpec
    from tests.core.generation.test_model_downloader import Response

    payload = b"immutable CPU model bytes for persistent H3 cache rehearsal"
    model = ModelSpec(name="session-tiny-model", repository="owner/model", revision="a" * 40,
                      path="weights/model.bin", url=None, destination="nested/model.bin",
                      bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    mount = tmp_path / "attached-volume"
    mount.mkdir()
    cache = mount / "models"
    device = mount.stat().st_dev
    monkeypatch.setattr(ps, "CACHE_MOUNT", str(mount))
    monkeypatch.setattr(ps, "MODEL_CACHE_ROOT", str(cache))
    monkeypatch.setattr(downloader, "_mount_identity", lambda _: (102, device, str(mount)))
    downloads, results, sessions = [], [], []
    replaced = []
    original_replace = downloader.os.replace
    def replace(source, destination, *args, **kwargs):
        if "dst_dir_fd" in kwargs:
            # Atomic cache publication happens only after temp bytes are exact.
            assert Path(source).name.startswith(".model.bin.cache-")
            assert not Path(source).is_absolute() and not Path(destination).is_absolute()
            replaced.append(str(destination))
        return original_replace(source, destination, *args, **kwargs)
    monkeypatch.setattr(downloader.os, "replace", replace)
    def opener(request, timeout):
        assert request.full_url == "https://huggingface.co/owner/model/resolve/" + "a" * 40 + "/weights/model.bin"
        downloads.append(request.full_url)
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        return Response(payload[start:end+1], start, end, len(payload))

    class CachedPod(FakePod):
        def _run(self, command):
            if "engines install" in command:
                argv = shlex.split(command.splitlines()[-1])
                option = lambda name: argv[argv.index(name)+1]
                assert option("--model-cache-root") == str(cache)
                assert int(option("--model-cache-device")) == device
                assert int(option("--model-cache-mount-id")) == 102
                result = downloader.download_model(model, models_root=str(Path(option("--root")) / "models"),
                    deadline=time.monotonic()+10, opener=opener, model_cache_root=option("--model-cache-root"),
                    model_cache_device=int(option("--model-cache-device")), model_cache_mount_id=int(option("--model-cache-mount-id")))
                results.append(result)
                destination = Path(option("--root")) / "models" / model.destination
                assert destination.read_bytes() == payload
            return super()._run(command)

    for launch, mutation, expected_downloads in ((1,None,1),(2,None,1),(3,"corrupt",2),(4,"missing",3)):
        cache_file = cache / downloader._cache_relative(model)
        if mutation == "corrupt": cache_file.write_bytes(b"x" * len(payload))  # same-size wrong hash
        if mutation == "missing": cache_file.unlink()
        root = tmp_path / f"launch-{launch}"
        args, agent = make_args(root)
        local = root / "local-engine"
        monkeypatch.setattr(ps, "ENGINE_ROOT", str(local))
        pod = CachedPod(video)
        original_execute = pod.execute_command
        def execute(command, timeout=600):
            if "def storage_probe(" not in command: return original_execute(command,timeout=timeout)
            pod.executes += 1
            pod.events.append("exec:storage")
            _,_,source,blob=shlex.split(command)
            namespace={"__name__":"transported_storage_probe"}
            exec(compile(source,"transported-pod-storage-probe.py","exec"),namespace)
            original_read,original_stat=Path.read_text,Path.stat
            row=f"102 93 {os.major(device)}:{os.minor(device)} / {mount} rw - nfs4 opaque:/PRIVATE_SOURCE rw\n"
            def read(path,*a,**kw):
                return row if path == Path("/proc/self/mountinfo") else original_read(path,*a,**kw)
            def path_stat(path,*a,**kw):
                result=original_stat(path,*a,**kw)
                if path == root:
                    values=list(result); values[2]=device+1
                    return os.stat_result(values)
                return result
            stdout,stderr=io.StringIO(),io.StringIO()
            with monkeypatch.context() as patch:
                patch.setattr(Path,"read_text",read); patch.setattr(Path,"stat",path_stat)
                patch.setattr(os,"statvfs",lambda _:SimpleNamespace(f_bavail=10**15,f_frsize=1,f_blocks=10**15))
                patch.setattr(sys,"argv",["-c",blob])
                with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr): code=namespace["main"]()
            return code,stdout.getvalue(),stderr.getvalue()
        monkeypatch.setattr(pod,"execute_command",execute)
        session=ps.Session(args,cli=pod.cli,transport_factory=pod.factory,http_status=pod.http_status,
                           agent_root=agent,sleep=lambda _:None,say=lambda _:None)
        assert session.run() == 0
        assert session.released and pod.terminated and session.records[-1]["detail"]["get"] == 404
        assert session.records[-1]["detail"]["list"] == []
        assert cache_file.read_bytes() == payload and len(downloads) == expected_downloads
        assert hashlib.sha256(cache_file.read_bytes()).hexdigest() == model.sha256
        assert not list(cache.rglob("*.cache-*"))
        assert all(key not in json.dumps(session.state) + (session.workdir / "pod-session-progress.json").read_text() for key in ("provider_mapping", "cache_device", "cache_mount_id"))
        assert pod.events.index("exec:storage") < pod.events.index("job:bootstrap") < pod.events.index("job:install")
        sessions.append(session)
    # First install + repair installs publish; second launch reads and hashes cache.
    assert len(replaced) == 3 and len(downloads) == 3
    assert len(results) == 8  # install plus idempotent rerun per launch
    assert not any((Path(item.state["storage"]["engine_root"]) in cache.parents) for item in sessions)


@pytest.mark.parametrize("path", ["/other", "/workspace/../other", 187])
def test_actual_legacy_sdk_mount_path_projection_refuses_before_private_upload(tmp_path, monkeypatch, video, path):
    from runpod_lifecycle import api, cli
    import runpod
    from runpod.api import ctl_commands
    args, agent = make_args(tmp_path)
    pod = FakePod(video)
    queries = []
    def graphql(query, api_key=None):
        queries.append(query)
        assert query == ctl_commands.pod_queries.QUERY_POD and "volumeMountPath" in query
        return {"data":{"myself":{"pods":[{"id":pod.pod,"name":"fixture","volumeMountPath":path,"runtime":{"ports":[]}}]}}}
    monkeypatch.setattr(ctl_commands,"run_graphql_query",graphql)
    monkeypatch.setattr(api,"runpod",runpod)
    monkeypatch.setattr(cli,"_resolve_api_key",lambda _:"synthetic")
    monkeypatch.setattr(api.httpx,"get",lambda *a,**kw:SimpleNamespace(status_code=200,json=lambda:[{"id":pod.pod,"networkVolume":{"id":"vol187"}}]))
    def runner(argv,env,**kwargs):
        if argv == ["list","--json"] and not pod.terminated and "ready" in pod.events:
            pod.events.append("attachment")
            stdout=io.StringIO()
            with contextlib.redirect_stdout(stdout): code=asyncio.run(cli._cmd_list(cli.build_parser().parse_args(argv)))
            return code,stdout.getvalue(),""
        assert argv[0] != "mounts"  # Independent legacy contradiction refuses first.
        return pod.cli(argv,env,**kwargs)
    session=ps.Session(args,cli=runner,transport_factory=pod.factory,http_status=pod.http_status,
                       agent_root=agent,sleep=lambda _:None,say=lambda _:None)
    assert session.run()==1 and pod.terminated and session.released
    assert len(queries)==1 and ps.BUNDLE_REMOTE not in pod.puts
    assert not pod.executes and "job:bootstrap" not in pod.events and "job:install" not in pod.events
    assert "network_volume_mount_path" not in json.dumps(session.state)


@pytest.mark.parametrize("case", ["missing-volume", "duplicate-volume", "wrong-attachment", "missing-attachment", "duplicate-pod"])
def test_storage_attachment_refuses_before_probe_and_upload(tmp_path, video, case):
    args, agent = make_args(tmp_path)
    pod = FakePod(video)
    original_cli = pod.cli
    def cli(argv, env):
        if argv == ["volumes", "ls", "--json"]:
            volumes = [{"name":ps.STORAGE_NAME,"dataCenterId":ps.DATACENTER,"id":"vol187"}]
            if case == "missing-volume": volumes=[]
            if case == "duplicate-volume": volumes*=2
            return 0,json.dumps(volumes),""
        if argv == ["list","--json"] and not pod.terminated and "ready" in pod.events:
            rows=[{"id":pod.pod,"network_volume_id":"wrong" if case == "wrong-attachment" else "vol187",
                   "network_volume_evidence":{"present":True,"valid":True,"conflict":False}}]
            if case == "missing-attachment":
                # Missing normalized legacy verdict cannot be treated as absence.
                del rows[0]["network_volume_id"]
                del rows[0]["network_volume_evidence"]
            if case == "duplicate-pod": rows*=2
            return 0,json.dumps(rows),""
        return original_cli(argv,env)
    session=ps.Session(args,cli=cli,transport_factory=pod.factory,http_status=pod.http_status,
                       agent_root=agent,sleep=lambda _:None,say=lambda _:None)
    assert session.run()==1 and session.released and pod.terminated
    assert "exec:storage" not in pod.events and ps.BUNDLE_REMOTE not in pod.puts
    assert "job:bootstrap" not in pod.events and "job:install" not in pod.events


def test_capacity_retries_then_terminal_rejection_preserves_exact_attempt(tmp_path):
    args, agent = make_args(tmp_path, ["--wait-capacity", "120"])
    pod = FakePod(b"")
    messages = []
    def runner(argv, env, *, on_capacity_wait=None):
        if argv[0] != "launch":
            return pod.cli(argv, env)
        pod.launches.append(argv)
        for line in [
            'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}',
            'RUNPOD_CAPACITY_WAIT {"placement_attempt":1,"retry_interval":30,"wait_seconds":120}',
            'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":2}',
            'RUNPOD_CAPACITY_WAIT {"placement_attempt":2,"retry_interval":30,"wait_seconds":120}',
            'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":3}',
            'RUNPOD_TERMINAL_FAILURE {"classification":"non_capacity_failure","placement_attempt":3}',
        ]:
            on_capacity_wait(line + "\n")
        return 1, "", "secret-provider-response private-id"
    runner.supports_capacity_progress = True
    session = ps.Session(args, cli=runner, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=messages.append)
    assert session.run() == 1 and len(pod.launches) == 1
    detail = session.records[0]["detail"]
    assert detail["state"] == "launch_failed" and detail["classification"] == "non_capacity_failure"
    assert detail["placement_attempt"] == 3 and detail["reconciliation_required"] is True
    report = (Path(args.workdir) / "pod-session-progress.json").read_text()
    attempt = json.loads(report)["claim_attempt"]
    assert attempt["placement_attempt"] == 3 and attempt["classification"] == "non_capacity_failure"
    assert attempt["reconciliation_required"] is True and "retry_interval" not in attempt
    assert "secret-provider-response" not in report + "\n".join(messages)
    assert "private-id" not in report + "\n".join(messages)
    assert not pod.terminated and session.records[-1]["result"] == "failed"


@pytest.mark.parametrize("events", [
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_WAIT {"placement_attempt":1,"retry_interval":30,"wait_seconds":120,"pod_id":"private-id"}', 'RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_WAIT {"placement_attempt":"secret","retry_interval":30,"wait_seconds":120}', 'RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_WAIT {"placement_attempt":true,"retry_interval":30,"wait_seconds":120}', 'RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_WAIT {"placement_attempt":1,"retry_interval":30,"wait_seconds":999}', 'RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_WAIT {"placement_attempt":1,"retry_interval":30,"wait_seconds":120}', 'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":2}'],
    ['unexpected secret-provider-response'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1,"placement_attempt":1}', 'RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":2}', 'RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_WAIT {"placement_attempt":2,"retry_interval":30,"wait_seconds":120}', 'RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_EXHAUSTED', 'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":2}'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_EXHAUSTED', 'RUNPOD_TERMINAL_FAILURE {"classification":"unknown","placement_attempt":1}'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_TERMINAL_FAILURE {"classification":"non_capacity_failure","placement_attempt":2}'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_TERMINAL_FAILURE {"classification":"non_capacity_failure"}'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_TERMINAL_FAILURE {"classification":"non_capacity_failure","placement_attempt":1,"secret":"private-id"}'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_TERMINAL_FAILURE {broken'],
])
def test_invalid_progress_cannot_authorize_fallback_or_forge_attempt(tmp_path, events):
    args, agent = make_args(tmp_path, ["--wait-capacity", "120"])
    pod = FakePod(b"")
    def runner(argv, env, *, on_capacity_wait=None):
        if argv[0] != "launch":
            return pod.cli(argv, env)
        pod.launches.append(argv)
        for line in events:
            on_capacity_wait(line + "\n")
        return 1, "", "secret-provider-response"
    runner.supports_capacity_progress = True
    session = ps.Session(args, cli=runner, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=lambda _: None)
    assert session.run() == 1 and len(pod.launches) == 1
    detail = session.records[0]["detail"]
    assert detail["reconciliation_required"] is True and detail["classification"] == "unknown"
    if events[0] == 'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}':
        expected = 2 if len(events) == 3 and events[2] == 'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":2}' and 'RUNPOD_CAPACITY_WAIT' in events[1] else 1
        assert detail["placement_attempt"] == expected
    else:
        assert "placement_attempt" not in detail
    report = (Path(args.workdir) / "pod-session-progress.json").read_text()
    assert "private-id" not in report and "secret-provider-response" not in report
    assert session.records[-1]["result"] == "failed"


def test_structured_ambiguous_allocation_after_retry_is_terminal(tmp_path):
    args, agent = make_args(tmp_path, ["--wait-capacity", "120"])
    pod = FakePod(b"")
    def runner(argv, env, *, on_capacity_wait=None):
        if argv[0] != "launch":
            return pod.cli(argv, env)
        pod.launches.append(argv)
        for line in [
            'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}',
            'RUNPOD_CAPACITY_WAIT {"placement_attempt":1,"retry_interval":30,"wait_seconds":120}',
            'RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":2}',
            'RUNPOD_TERMINAL_FAILURE {"classification":"allocation_unknown","placement_attempt":2}',
        ]:
            on_capacity_wait(line + "\n")
        return 1, "", "private-id secret-provider-response"
    runner.supports_capacity_progress = True
    session = ps.Session(args, cli=runner, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=lambda _: None)
    assert session.run() == 1 and len(pod.launches) == 1
    detail = session.records[0]["detail"]
    assert detail["state"] == detail["classification"] == "allocation_unknown"
    assert detail["placement_attempt"] == 2 and detail["reconciliation_required"] is True
    assert "private-id" not in (Path(args.workdir) / "pod-session-progress.json").read_text()
    assert session.records[-1]["result"] == "failed"


@pytest.mark.parametrize("events", [
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":2}'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}', 'RUNPOD_CAPACITY_EXHAUSTED'],
    ['RUNPOD_PLACEMENT_ATTEMPT {"placement_attempt":1}',
     'RUNPOD_CAPACITY_WAIT {"placement_attempt":1,"retry_interval":30,"wait_seconds":120}'],
])
def test_success_with_invalid_progress_retains_known_pod_for_release(tmp_path, events):
    args, agent = make_args(tmp_path, ["--wait-capacity", "120"])
    pod = FakePod(b"")
    def runner(argv, env, *, on_capacity_wait=None):
        if argv[0] != "launch":
            return pod.cli(argv, env)
        pod.launches.append(argv)
        for event in events:
            on_capacity_wait(event + "\n")
        return 0, json.dumps({"pod_id": pod.pod}), ""
    runner.supports_capacity_progress = True
    session = ps.Session(args, cli=runner, transport_factory=pod.factory, http_status=pod.http_status,
                         agent_root=agent, say=lambda _: None, sleep=lambda _: None)
    assert session.run() == 1 and len(pod.launches) == 1
    assert session.pod_id == pod.pod and pod.terminated
    assert session.records[0]["detail"]["reconciliation_required"] is True
    assert session.records[-1]["result"] == "failed"


def test_real_lifecycle_capacity_retries_then_rejection_stop_without_fallback(tmp_path):
    args, agent = make_args(tmp_path, ["--wait-capacity", "120"])
    pod = FakePod(b"")
    count = tmp_path / "create-count"
    executable = tmp_path / "offline-lifecycle-rejection"
    executable.write_text(f'''#!{sys.executable}
import pathlib
from runpod_lifecycle import cli, lifecycle
from runpod_lifecycle.config import RunPodConfig
from runpod_lifecycle.errors import ProviderCapacityUnavailable, ProviderRequestRejected
cli._resolve_config = lambda parsed: RunPodConfig(api_key="synthetic", gpu_type=parsed.gpu_type,
    ram_tiers_enabled=False, transport="https", https_token="synthetic", storage_volumes=(), storage_name=None)
lifecycle.find_gpu_type = lambda *args: {{"id": "fake"}}
calls = 0
def create(*args, **kwargs):
    global calls
    calls += 1
    pathlib.Path({str(count)!r}).write_text(str(calls))
    if calls <= 2:
        raise ProviderCapacityUnavailable(200, "no longer any instances available secret-provider-response")
    raise ProviderRequestRejected(403, "private-id secret-provider-response")
lifecycle.create_pod = create
async def sleep(seconds):
    pass
lifecycle.asyncio.sleep = sleep
raise SystemExit(cli.main())
''')
    executable.chmod(0o700)
    messages = []
    session = ps.Session(args, cli=offline_launch_cli(executable, pod), transport_factory=pod.factory,
                         http_status=pod.http_status, agent_root=agent, say=messages.append)
    assert session.run() == 1 and count.read_text() == "3" and len(pod.launches) == 1
    detail = session.records[0]["detail"]
    assert detail["placement_attempt"] == 3 and detail["reconciliation_required"] is True
    assert detail["classification"] == "allocation_unknown"
    report = (Path(args.workdir) / "pod-session-progress.json").read_text()
    assert "secret-provider-response" not in report + "\n".join(messages)
    assert "private-id" not in report + "\n".join(messages)
