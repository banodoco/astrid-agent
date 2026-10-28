"""CPU tests for scripts/pod_session.py with a stub runpod CLI and a stub pod transport."""
import base64
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

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

    def __init__(self, video_bytes, fail=None, fail_gpus=()):
        self.video, self.fail, self.fail_gpus = video_bytes, fail, set(fail_gpus)
        self.events, self.files, self.jobs, self.puts = [], {}, {}, {}
        self.starts = self.executes = 0
        self.terminated, self.launches = False, []
        self.pod = "abc123podid"

    # -- CLI -------------------------------------------------------------------------------
    def cli(self, argv, env):
        cmd = argv[0]
        if cmd == "launch":
            self.launches.append(argv)
            self.events.append("launch")
            gpu = argv[argv.index("--gpu-type") + 1]
            if self.fail == "claim" or gpu in self.fail_gpus:
                return 1, "", "no capacity"
            return 0, json.dumps({"pod_id": self.pod, "name": "n", "https": "x"}), ""
        if cmd == "terminate":
            self.events.append("terminate")
            self.terminated = True
            return 0, "terminated", ""
        if cmd == "list":
            self.events.append("list")
            return 0, "[]", ""
        raise AssertionError(argv)

    def http_status(self, pod_id):
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


def make_args(tmp_path, extra=()):
    sources = tmp_path / "sources"
    for name in ps.BUNDLE_TREES[1:]:
        (sources / name).mkdir(parents=True, exist_ok=True)
        (sources / name / "file.txt").write_text(name)
        (sources / name / ".git").write_text("gitdir")
    agent = tmp_path / "agent"
    (agent / "scripts").mkdir(parents=True, exist_ok=True)
    (agent / "scripts" / "gpu_sanity.py").write_text("print('x')")
    (agent / "tests").mkdir(exist_ok=True)
    (agent / "tests" / "t.py").write_text("t")
    (agent / ".git").mkdir(exist_ok=True)
    (agent / ".git" / "HEAD").write_text("h")
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    for n in ("img1", "img2"):
        (assets / f"{n}.png").write_bytes(n.encode())
    (tmp_path / "request.json").write_text(json.dumps(REQUEST, indent=2))
    (tmp_path / "asset-map.json").write_text(json.dumps({n: str(assets / f"{n}.png") for n in ("img1", "img2")}))
    argv = ["--datacenters", "EUR-NO-1,EU-RO-1", "--sources-dir", str(sources), "--request", str(tmp_path / "request.json"),
            "--asset-map", str(tmp_path / "asset-map.json"), "--workdir", str(tmp_path / "work"),
            "--token-dir", str(tmp_path / "tokens"), "--poll-interval", "0", *extra]
    return ps.build_parser().parse_args(argv), agent


def run_session(tmp_path, video_bytes, *, fail=None, fail_gpus=(), budget=None, probe=None, extra=()):
    args, agent = make_args(tmp_path, extra)
    pod = FakePod(video_bytes, fail=fail, fail_gpus=fail_gpus)
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
    firsts = ["launch", "put:" + ps.BUNDLE_REMOTE, "exec:bundle-check", "job:bootstrap", "job:install", "job:rerun",
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
    assert gpus == ["RTX 5090", "RTX 5090", "RTX PRO 4500"] and dcs == ["EUR-NO-1", "EU-RO-1", "EUR-NO-1"]
    argv = pod.launches[-1]
    assert argv[argv.index("--allowed-cuda-versions") + 1] == "13.0,13.1,13.2,13.3"
    assert argv[argv.index("--container-disk-gb") + 1] == "120"
    assert argv[argv.index("--disk-size-gb") + 1] == "10"
    assert argv[argv.index("--image") + 1] == ps.recipe_image() and "--detach" in argv
    assert "--storage-name" not in argv


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
    assert {n.split("/")[0] for n in names} == set(ps.BUNDLE_TREES)
    assert "astrid-agent/scripts/gpu_sanity.py" in names
    assert not [n for n in names if "/.git" in n or n.startswith("astrid-agent/tests")]
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
