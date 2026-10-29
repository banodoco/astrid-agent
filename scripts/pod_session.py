#!/usr/bin/env python3
"""One command for a whole GPU pod session, fail closed at every step.

Usage: pod_session.py --datacenters EUR-NO-1 --storage-name storage --sources-dir DIR --workdir DIR
                      (--jobs jobs.yaml | --request job/request.json --asset-map job/asset-map.json)

Legacy steps: claim, bundle, bootstrap, install, rerun, S0, CLIP, download, release.
Job lists add explicit GPU sanity before installation, then S0 for each H3 job,
execution, verified download and review packet for each job, followed by release.
The first failing step stops the run; release always runs (terminate, GET 404, account list []).

Example jobs.yaml (local input paths are relative to this YAML file):
    version: 1
    jobs:
      - id: clip
        type: h3
        goal: Make a nine-second audiovisual clip.
        request: request.json
        assets: {reference: reference.png}  # alternatively asset-map: assets.json
      - id: image
        type: cmd
        goal: Generate an image and a result receipt.
        prompt: A blue room.
        command: [python, generate.py]
        input-files: {generate.py: generate.py, inputs/reference.png: reference.png}
        expected-outputs: [output.png, result.json]
        timeout: 600

H3 retains the nine-second request and one-second S0 contract. Cmd commands run
inside a fresh per-job pod directory in the existing H3 engine environment.
Expected outputs are relative paths; extensions select PNG decode, JSON-object or
MP4 probe checks, or use {path: output.png, kind: png, width: 1024, height: 576}.
Other files receive nonempty and SHA256 checks. Packets preserve inputs and logs,
README goal/prompt/check result, machine-check.json and verified outputs; H3 also
gets a local side-by-side MP4 when a visual reference is available.

Agent job budget: every detached start and every execute is one agent job record (the agent caps
records at 128). Polls and downloads are reads and create none. The driver counts records itself and
aborts, with a clear message, before the count reaches JOB_LIMIT (100). After every step the driver
writes /root/pod-session-progress.json on the pod through a job and reads it back with a download.

Credentials: the pod token is generated fresh per claim into a 0600 file and never printed. The
RunPod API key stays in the environment the runpod-lifecycle CLI already reads.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from astrid.packs.runpod.cuda_versions import DEFAULT_ALLOWED_CUDA_VERSIONS  # noqa: E402

STEPS = ("claim", "bundle", "bootstrap", "install", "rerun", "s0", "clip", "download", "release")
JOB_LIMIT = 100
DEFAULT_GPUS = ("RTX 5090", "RTX PRO 4500")
STORAGE_NAME = "storage"
DATACENTER = "EUR-NO-1"
CUDA_VERSIONS = ",".join(DEFAULT_ALLOWED_CUDA_VERSIONS)
ENGINE = "vibecomfy-h3"
PLATFORM = "linux-x86_64-cu130"
ENGINE_ROOT = "/root/astrid/engines/vibecomfy-h3"
BUNDLE_DIR = "/root/bundle"
BUNDLE_REMOTE = BUNDLE_DIR + "/pod-src.tgz"
SRC_DIR = BUNDLE_DIR + "/src"
CLIP_DIR = "/root/clip"
JOB_ROOT = "/root/astrid-jobs"
PROGRESS_REMOTE = "/root/pod-session-progress.json"
SANITY_CMD = f"/opt/astrid/bin/python {SRC_DIR}/astrid-agent/scripts/gpu_sanity.py"
ACTION = "h3_av.transform"
CLIP_SECONDS, SMOKE_SECONDS = 9, 1
FINAL_VIDEO = "07-finalizer/retrieved/verified-candidate.media"
FINAL_RECEIPT = "08-final-receipt.json"
COMPOSITION_MANIFEST = "05-compose/retrieved/composition-manifest.json"
BUNDLE_TREES = ("astrid-agent", "runpod-python", "astrid-runtime", "runpod-lifecycle", "VibeComfy")
EXPECTED_VIDEO = {"frames": 216, "rate": "24/1", "width": 1024, "height": 576}
_SECRET_LINE = re.compile(r"key|token|secret|bearer|authorization|password", re.I)


class SessionError(RuntimeError):
    """A step failed; the message is safe to print."""


class BudgetExceeded(SessionError):
    pass


def relative_file(value: Any) -> str:
    """Manifest paths on the pod stay inside the per-job working directory."""
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise SessionError("job file paths must be non-empty relative paths")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value in (".", "./"):
        raise SessionError(f"unsafe job file path: {value!r}")
    return path.as_posix()


def load_jobs(path: Path) -> list[dict]:
    """Read the deliberately small v1 operational manifest; local paths are manifest-relative."""
    import yaml

    path = Path(path).resolve()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SessionError(f"cannot read job list: {type(exc).__name__}") from None
    if not isinstance(document, dict) or document.get("version", 1) != 1:
        raise SessionError("job list must be a version: 1 YAML object")
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise SessionError("job list must contain a non-empty jobs list")
    seen, result = set(), []

    def local_file(value):
        if not isinstance(value, str):
            raise SessionError("local input files must be paths")
        local = (path.parent / value).resolve()
        if not local.is_file():
            raise SessionError(f"input is not a file: {local}")
        return local

    for original in jobs:
        if not isinstance(original, dict):
            raise SessionError("each job must be an object")
        job = dict(original)
        jid, kind = job.get("id"), job.get("type")
        if not isinstance(jid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", jid) or jid in seen:
            raise SessionError("job ids must be unique safe names (1-64 characters)")
        seen.add(jid)
        if kind not in ("h3", "cmd"):
            raise SessionError(f"job {jid}: type must be h3 or cmd")
        timeout = job.get("timeout", 5400)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise SessionError(f"job {jid}: timeout must be positive seconds")
        job["timeout"] = timeout
        if kind == "h3":
            job["request"] = local_file(job.get("request"))
            # Keep the existing nine-second H3 plus one-second S0 contract intact.
            derive_requests(job["request"])
            assets = job.get("assets")
            if assets is None:
                job["asset-map"] = local_file(job.get("asset-map"))
                assets = json.loads(job["asset-map"].read_text(encoding="utf-8"))
            if not isinstance(assets, dict) or not assets:
                raise SessionError(f"job {jid}: assets must be a non-empty map")
            if any(not isinstance(a, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", a) for a in assets):
                raise SessionError(f"job {jid}: unsafe asset id")
            job["assets"] = {a: local_file(p) for a, p in assets.items()}
            request = json.loads(job["request"].read_bytes())
            needed = {r.get("asset") for r in request.get("references", []) if isinstance(r, dict)}
            source = request.get("source")
            if isinstance(source, dict) and source.get("asset"):
                needed.add(source["asset"])
            if needed - job["assets"].keys():
                raise SessionError(f"job {jid}: missing assets {sorted(needed - job['assets'].keys())}")
            job.setdefault("prompt", (request.get("content") or {}).get("prompt", ""))
        else:
            command = job.get("command")
            if isinstance(command, list) and command and all(isinstance(a, str) for a in command):
                command = shlex.join(command)
            if not isinstance(command, str) or not command.strip() or "\x00" in command:
                raise SessionError(f"job {jid}: command must be shell text or an argv list")
            job["command"] = command
            inputs = job.get("input-files", {})
            if not isinstance(inputs, dict):
                raise SessionError(f"job {jid}: input-files must map remote relative paths to local paths")
            job["input-files"] = {relative_file(p): local_file(v) for p, v in inputs.items()}
            outputs = job.get("expected-outputs")
            if not isinstance(outputs, list) or not outputs:
                raise SessionError(f"job {jid}: expected-outputs must be a non-empty list")
            normalized = []
            for output in outputs:
                output = {"path": output} if isinstance(output, str) else dict(output) if isinstance(output, dict) else {}
                output["path"] = relative_file(output.get("path"))
                suffix = Path(output["path"]).suffix.lower()
                output.setdefault("kind", {".png": "png", ".json": "json", ".mp4": "mp4"}.get(suffix, "file"))
                if output["kind"] not in ("png", "json", "mp4", "file"):
                    raise SessionError(f"job {jid}: unknown output kind")
                if any(o["path"] == output["path"] for o in normalized):
                    raise SessionError(f"job {jid}: duplicate output path")
                normalized.append(output)
            job["expected-outputs"] = normalized
        result.append(job)
    return result


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_tail(text: str, limit: int = 1500) -> str:
    lines = [ln for ln in (text or "").splitlines() if not _SECRET_LINE.search(ln)]
    return "\n".join(lines)[-limit:]


class JobBudget:
    """Counts agent job records. ``charge`` refuses the record that would reach the limit."""

    def __init__(self, limit: int = JOB_LIMIT, count: int = 0):
        self.limit, self.count = limit, count

    def charge(self, what: str) -> None:
        if self.count + 1 >= self.limit:
            raise BudgetExceeded(
                f"job budget: {self.count} agent job records used; refusing '{what}' because it would reach "
                f"the limit of {self.limit} (the agent caps records at 128). Release this pod and claim a fresh one."
            )
        self.count += 1


# ---------------------------------------------------------------------------------------------
# Pure helpers: requests, bundle, recipe sizing, remote scripts
# ---------------------------------------------------------------------------------------------

def derive_requests(clip_path: Path) -> tuple[bytes, bytes]:
    """Return (clip request bytes unchanged, S0 request bytes with output.duration 1)."""
    raw = Path(clip_path).read_bytes()
    clip = json.loads(raw)
    duration = (clip.get("output") or {}).get("duration") if isinstance(clip, dict) else None
    if duration != CLIP_SECONDS or isinstance(duration, bool):
        raise SessionError(f"the clip request must have output.duration {CLIP_SECONDS}, found {duration!r}")
    smoke = copy.deepcopy(clip)
    smoke["output"]["duration"] = SMOKE_SECONDS
    return raw, (json.dumps(smoke, indent=2, ensure_ascii=False) + "\n").encode()


def remote_asset_map(local_map: Path, request: dict) -> tuple[dict[str, str], dict[str, Path]]:
    """Pod-side asset map ({id: pod path}) and the local files to upload for it."""
    mapping = json.loads(Path(local_map).read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or not mapping:
        raise SessionError("the asset map must be a non-empty JSON object")
    needed = {r.get("asset") for r in request.get("references", []) if isinstance(r, dict)}
    missing = sorted(a for a in needed if a not in mapping)
    if missing:
        raise SessionError(f"the asset map lacks assets the request references: {', '.join(map(str, missing))}")
    remote, uploads = {}, {}
    for asset, path in mapping.items():
        local = Path(path)
        if not local.is_file():
            raise SessionError(f"asset {asset!r} is not a file: {local}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(asset)):
            raise SessionError(f"unsafe asset id {asset!r}")
        target = f"{CLIP_DIR}/inputs/{asset}{local.suffix}"
        remote[asset], uploads[target] = target, local
    return remote, uploads


def recipe_footprint_gb() -> int:
    """Install footprint of the engine: its model bytes plus a 10 GB runtime allowance."""
    from astrid.core.generation.engine_recipe import load_recipe_model_lock

    total = sum(m.bytes for m in load_recipe_model_lock(ENGINE, PLATFORM).models)
    return math.ceil(total / 1e9) + 10


def container_disk_gb(requested: int | None, footprint: int) -> int:
    default = max(120, math.ceil(footprint * 1.5))
    disk = default if requested is None else requested
    if disk < footprint + 20:
        raise SessionError(f"container disk {disk} GB cannot hold the engine footprint of about {footprint} GB")
    return disk


def recipe_image() -> str:
    path = REPO / "astrid" / "packs" / "runpod" / "environment.linux-cu130.json"
    image = json.loads(path.read_text(encoding="utf-8")).get("image")
    if not isinstance(image, str) or not image:
        raise SessionError(f"no image in {path}")
    return image


def _bundle_git(root: Path, *args: str, input: bytes | None = None, **extra_env: str) -> bytes:
    # Never inherit a controller's redirected Git directory/index or export its
    # configuration, remotes, hooks, credentials, alternates or linked-worktree pointer.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, **extra_env)
    try:
        return subprocess.run(["git", "-C", str(root), *args], input=input, env=env,
                              capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionError(f"bundle: cannot verify source checkout {root.name} ({args[0]})") from exc


def build_bundle(agent_root: Path, sources_dir: Path, dest: Path) -> tuple[int, str]:
    """Ship clean committed snapshots as portable, detached shallow Git checkouts.

    Only the selected commit and its tree/blob objects travel; no ancestry or
    machine-local Git metadata is copied. All tracked files (including tests)
    are required by Runtime's content-sensitive source identity.
    """
    trees = {"astrid-agent": Path(agent_root)} | {n: Path(sources_dir) / n for n in BUNDLE_TREES[1:]}
    heads, paths = {}, {}
    for name, path in trees.items():
        if (not path.is_dir() or not (path / ".git").exists()
                or _bundle_git(path, "rev-parse", "--show-prefix") != b"\n"):
            raise SessionError(f"bundle: source must be an exact Git checkout root: {name}")
        if _bundle_git(path, "status", "--porcelain", "--untracked-files=all"):
            raise SessionError(f"bundle: source checkout is dirty: {name}")
        head = _bundle_git(path, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise SessionError(f"bundle: unsupported commit identity: {name}")
        heads[name] = head
        entries = _bundle_git(path, "ls-tree", "-rz", "--full-tree", head).split(b"\0")
        paths[name] = []
        for row in filter(None, entries):
            metadata, relative = row.split(b"\t", 1)
            if metadata.split()[0] not in (b"100644", b"100755", b"120000"):
                raise SessionError(f"bundle: unsupported tracked entry in {name}")
            paths[name].append(os.fsdecode(relative))
    lock = trees["astrid-agent"] / "runtime.lock.json"
    manifest = {"heads": heads, "runtime_lock_sha256": _file_sha256(lock)}
    if json.loads(lock.read_text())["source_commit"] != heads["astrid-runtime"]:
        raise SessionError("bundle: Runtime HEAD differs from Agent runtime.lock.json")

    def add_bytes(tar: tarfile.TarFile, name: str, data: bytes):
        info = tarfile.TarInfo(name)
        info.size, info.mode = len(data), 0o644
        tar.addfile(info, io.BytesIO(data))

    with tempfile.TemporaryDirectory(prefix="pod-source-", dir=Path(dest).parent) as staging:
        with tarfile.open(dest, "w:gz", dereference=False) as tar:
            for name, path in trees.items():
                head = heads[name]
                for relative in paths[name]:
                    tar.add(path / relative, arcname=f"{name}/{relative}", recursive=False)
                # pack-objects without --revs takes exactly the enumerated objects.
                # --no-walk includes HEAD's complete tree, but no parent commits.
                objects = _bundle_git(path, "rev-list", "--objects", "--no-walk", "--no-object-names", head)
                pack = Path(staging) / name
                pack_hash = _bundle_git(path, "pack-objects", str(pack), input=objects).decode().strip()
                for suffix in ("pack", "idx"):
                    tar.add(f"{pack}-{pack_hash}.{suffix}",
                            arcname=f"{name}/.git/objects/pack/pack-{pack_hash}.{suffix}")
                index = Path(staging) / f"{name}.index"
                _bundle_git(path, "read-tree", head, GIT_INDEX_FILE=str(index))
                tar.add(index, arcname=f"{name}/.git/index")
                add_bytes(tar, f"{name}/.git/HEAD", (head + "\n").encode())
                add_bytes(tar, f"{name}/.git/shallow", (head + "\n").encode())
                add_bytes(tar, f"{name}/.git/config", b"[core]\nrepositoryformatversion = 0\nbare = false\nfilemode = true\n")
                refs = tarfile.TarInfo(f"{name}/.git/refs")
                refs.type, refs.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(refs)
                if (_bundle_git(path, "rev-parse", "HEAD").decode().strip() != head
                        or _bundle_git(path, "status", "--porcelain", "--untracked-files=all")):
                    raise SessionError(f"bundle: source checkout changed during packaging: {name}")
            add_bytes(tar, ".pod-source.json", (json.dumps(manifest, sort_keys=True) + "\n").encode())
    return Path(dest).stat().st_size, _file_sha256(Path(dest))


def bootstrap_script() -> str:
    return "\n".join([
        "set -eu",
        f"mkdir -p {SRC_DIR}",
        f"tar xzf {BUNDLE_REMOTE} -C {SRC_DIR}",
        f"cd {BUNDLE_DIR}",
        "export ASTRID_BOOTSTRAP_ROOT=/root/astrid/bootstrap ASTRID_REALM_ROOT=/root/realm ASTRID_VENV=/opt/astrid",
        "bash src/astrid-agent/scripts/pod_bootstrap.sh",
    ]) + "\n"


def engine_script(verb: str, deadline: int) -> str:
    lines = ["set -eu", "export PATH=/opt/astrid/bin:$PATH"]
    if verb == "install":
        lines.append(f"export ASTRID_ENGINE_SANITY_CMD={shlex.quote(SANITY_CMD)}")
    lines.append(f"astrid engines {verb} {ENGINE} --placement local --root {ENGINE_ROOT} --deadline {deadline}")
    return "\n".join(lines) + "\n"


def action_script(name: str, request: str, asset_map: str, out: str) -> str:
    """The action runs with the pod's default environment; only PATH gains the venv's bin."""
    summary = f"{CLIP_DIR}/runs/{name}.summary.json"
    return "\n".join([
        "export PATH=/opt/astrid/bin:$PATH",
        f"if [ -e {shlex.quote(out)} ]; then echo 'out dir already exists' >&2; exit 3; fi",
        f"mkdir -p {CLIP_DIR}/runs",
        f"cd {CLIP_DIR}",
        "rc=0",
        f"astrid action run {ACTION} --request {shlex.quote(request)} --asset-map {shlex.quote(asset_map)} "
        f"--placement local --out {shlex.quote(out)} > {shlex.quote(summary)} || rc=$?",
        "exit $rc",
    ]) + "\n"


def progress_script(document: bytes) -> str:
    blob = base64.b64encode(document).decode()
    return "\n".join([
        "set -eu",
        f"printf %s {shlex.quote(blob)} | base64 -d > {PROGRESS_REMOTE}.tmp",
        f"mv {PROGRESS_REMOTE}.tmp {PROGRESS_REMOTE}",
    ]) + "\n"


def parse_json_object(text: str) -> dict:
    """The last top-level JSON object in ``text`` (a command may print log lines first)."""
    decoder, found = json.JSONDecoder(), None
    for match in re.finditer(r"^\{", text, re.M):
        try:
            value, _end = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict):
            found = value
    if found is None:
        raise SessionError("no JSON object in command output")
    return found


def probe_video(path: Path, ffprobe: str = "ffprobe") -> dict:
    done = subprocess.run([ffprobe, "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(path)],
                          capture_output=True, text=True, timeout=300)
    if done.returncode:
        raise SessionError(f"ffprobe failed: {safe_tail(done.stderr, 400)}")
    streams = json.loads(done.stdout).get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise SessionError("no video stream")
    return {"frames": int(video.get("nb_read_frames", -1)), "rate": video.get("r_frame_rate"),
            "width": video.get("width"), "height": video.get("height"),
            "audio": any(s.get("codec_type") == "audio" for s in streams)}


def check_video(info: dict) -> None:
    problems = [f"{k} {info.get(k)!r} != {v!r}" for k, v in EXPECTED_VIDEO.items() if info.get(k) != v]
    if not info.get("audio"):
        problems.append("no audio stream")
    if problems:
        raise SessionError("final video check failed: " + "; ".join(problems))


def _publication_state(receipt: dict):
    return (receipt.get("states") or {}).get("final_composition_publication")


def publication_verified(receipt: dict) -> bool:
    state = _publication_state(receipt)
    status = state.get("status") if isinstance(state, dict) else state
    contract = (receipt.get("publication_contract") or {}).get("final_composition_publication", "verified")
    return status in ("verified", "passed") and contract == "verified"


def attested_object_id(receipt: dict) -> str | None:
    """The sha256 object id the receipt publishes for the final composition, if it carries one."""
    state = _publication_state(receipt)
    if not isinstance(state, dict):
        return None
    object_id = ((state.get("evidence") or {}).get("publication") or {}).get("object_id")
    return object_id if isinstance(object_id, str) else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# Default collaborators (replaced by stubs in tests)
# ---------------------------------------------------------------------------------------------

def default_cli_runner(binary: str):
    def run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        done = subprocess.run([binary, "--transport", "https", *argv], env=env, capture_output=True, text=True)
        return done.returncode, done.stdout, done.stderr
    return run


def default_transport_factory(pod_id: str, token: str):
    from runpod_lifecycle.https import HttpsPodTransport
    return HttpsPodTransport(pod_id, token)


def default_http_status(pod_id: str) -> int:
    import httpx
    from runpod_lifecycle.https import pod_url
    return httpx.get(pod_url(pod_id), timeout=20).status_code


# ---------------------------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------------------------

class Session:
    def __init__(self, args: argparse.Namespace, *, cli: Callable, transport_factory: Callable,
                 http_status: Callable, probe: Callable = probe_video, budget: JobBudget | None = None,
                 sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print,
                 agent_root: Path = REPO):
        self.args, self.cli, self.transport_factory = args, cli, transport_factory
        self.agent_root = Path(agent_root)
        self.http_status, self.probe, self.sleep, self.say = http_status, probe, sleep, say
        self.budget = budget or JobBudget()
        self.sid = secrets.token_hex(4)
        self.name = f"astrid-pod-session-{self.sid}"
        self.workdir = Path(args.workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.pod_id: str | None = None
        self.transport = None
        self.token_path: Path | None = None
        self.released = False
        self.records: list[dict] = []
        self.state: dict[str, Any] = {}
        self.cli_env = {k: v for k, v in os.environ.items()
                        if k not in ("RUNPOD_STORAGE_NAME", "RUNPOD_STORAGE_VOLUMES", "RUNPOD_ATTACH_ONLY")}

    # -- agent jobs ---------------------------------------------------------------------------
    def start_job(self, label: str, command: str) -> dict:
        self.budget.charge(label)
        return self.transport.detached_start(command, root=JOB_ROOT)

    def execute(self, label: str, command: str, timeout: float = 600) -> tuple[int, str, str]:
        self.budget.charge(label)
        return self.transport.execute_command(command, timeout=timeout)

    def fetch(self, remote: str) -> str:
        local = self.workdir / "fetch" / (secrets.token_hex(4) + "-" + Path(remote).name)
        local.parent.mkdir(parents=True, exist_ok=True)
        self.transport.get(remote, local)
        text = local.read_text(encoding="utf-8", errors="replace")
        local.unlink()
        return text

    def wait_job(self, job: dict, timeout: float) -> int:
        deadline, errors = time.monotonic() + timeout, 0
        while True:
            try:
                state = self.transport.detached_poll(job["id"], root=JOB_ROOT)
                errors = 0
            except Exception as exc:  # polls are reads; rate limits and route gaps are transient
                errors += 1
                if errors > 20:
                    raise SessionError(f"job {job['id']}: poll failed {errors} times ({type(exc).__name__})") from None
                self.sleep(self.args.poll_interval)
                continue
            if state.get("state") == "exited" and state.get("exit_code") is not None:
                return int(state["exit_code"])
            if state.get("state") in ("cancelled",):
                raise SessionError(f"job {job['id']} was cancelled")
            if time.monotonic() >= deadline:
                raise SessionError(f"job {job['id']} did not exit within {int(timeout)} s")
            self.sleep(self.args.poll_interval)

    def run_job(self, label: str, command: str, timeout: float, *, want_stdout: bool = False) -> tuple[int, str, dict]:
        job = self.start_job(label, command)
        try:
            code = self.wait_job(job, timeout)
        except BaseException:
            # The detached process outlives the launcher; cancel it before releasing the pod.
            try:
                self.transport.detached_cancel(job["id"], root=JOB_ROOT)
            except Exception:
                pass
            raise
        out = ""
        if want_stdout or code != 0:
            try:
                out = self.fetch(job["dir"] + "/stdout")
                if code != 0:
                    self.say(f"[{label}] job {job['id']} exit {code}; stdout tail:\n{safe_tail(out)}")
                    self.say(f"[{label}] stderr tail:\n{safe_tail(self.fetch(job['dir'] + '/stderr'))}")
            except Exception:
                pass
        return code, out, job

    def must(self, label: str, command: str, timeout: float, **kw) -> tuple[str, dict]:
        code, out, job = self.run_job(label, command, timeout, **kw)
        if code != 0:
            raise SessionError(f"{label}: job {job['id']} exited {code}")
        return out, job

    # -- steps --------------------------------------------------------------------------------
    def claim(self) -> dict:
        from runpod_lifecycle.https import read_token_file, write_token_file

        if self.args.datacenters != [DATACENTER]:
            raise SessionError(f"claim: C0 requires exactly one datacenter: {DATACENTER}")
        if self.args.storage_name != STORAGE_NAME:
            raise SessionError(f"claim: C0 requires the existing storage volume named {STORAGE_NAME!r}")

        footprint = recipe_footprint_gb()
        disk = container_disk_gb(self.args.container_disk_gb, footprint)
        image = self.args.image or recipe_image()
        token_dir = Path(self.args.token_dir).resolve()
        token_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.token_path = token_dir / f"pod-session-{self.sid}.token"
        write_token_file(self.token_path, secrets.token_urlsafe(48))  # fresh per claim, O_EXCL, 0600
        env = dict(self.cli_env, RUNPOD_HTTPS_TOKEN_FILE=str(self.token_path),
                   RUNPOD_STORAGE_NAME=self.args.storage_name, RUNPOD_STORAGE_VOLUMES="",
                   RUNPOD_ATTACH_ONLY="true")
        self.cli_env = env
        tried = []
        for gpu in self.args.gpus:
            dc = DATACENTER
            argv = ["launch", "--detach", "--name", self.name, "--gpu-type", gpu, "--image", image,
                    "--datacenter-id", dc, "--allowed-cuda-versions", CUDA_VERSIONS,
                    "--container-disk-gb", str(disk), "--disk-size-gb", str(self.args.volume_gb),
                    "--storage-name", self.args.storage_name]
            if self.args.wait_capacity:
                argv += ["--wait-capacity", str(self.args.wait_capacity)]
            code, out, err = self.cli(argv, env)
            tried.append(f"{gpu}@{dc}")
            if code == 0:
                pod_id = parse_json_object(out).get("pod_id")
                if not isinstance(pod_id, str) or not re.fullmatch(r"[a-z0-9]+", pod_id):
                    raise SessionError("launch succeeded but printed no valid pod id")
                self.pod_id = pod_id
                self.transport = self.transport_factory(pod_id, read_token_file(self.token_path))
                self.transport.wait_ready(timeout=600)
                return {"pod_id": pod_id, "gpu": gpu, "datacenter": dc, "container_disk_gb": disk,
                        "volume_gb": self.args.volume_gb, "image": image}
            self.say(f"[claim] {gpu} @ {dc}: no pod (exit {code}) {safe_tail(err, 300)}")
        raise SessionError("claim: no pod from any of: " + ", ".join(tried))

    def put(self, local: Path, remote: str) -> None:
        for attempt in range(3):
            try:
                self.transport.put(local, remote)
                return
            except Exception:
                if attempt == 2:
                    raise
                self.sleep(20)

    def bundle_sources(self) -> dict:
        tar = self.workdir / "pod-src.tgz"
        size, digest = build_bundle(self.agent_root, Path(self.args.sources_dir), tar)
        self.put(tar, BUNDLE_REMOTE)
        code, out, err = self.execute("bundle size check", f"stat -c %s {BUNDLE_REMOTE} && sha256sum {BUNDLE_REMOTE}")
        fields = out.split()
        if code != 0 or len(fields) < 2 or fields[0] != str(size) or fields[1] != digest:
            raise SessionError(f"bundle: remote size/sha {fields[:2]} differs from local {size}/{digest[:12]}")
        return {"bytes": size, "sha256": digest}

    def bundle(self) -> dict:
        detail = self.bundle_sources()
        clip_raw, smoke_raw = derive_requests(Path(self.args.request))
        remote_map, uploads = remote_asset_map(Path(self.args.asset_map), json.loads(clip_raw))
        for name, data in (("request.json", clip_raw), ("smoke-request.json", smoke_raw),
                           ("asset-map.json", (json.dumps(remote_map, indent=2) + "\n").encode())):
            local = self.workdir / "inputs" / name
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            self.put(local, f"{CLIP_DIR}/{name}")
        for remote, local in uploads.items():
            self.put(local, remote)
        return detail

    def bootstrap(self) -> dict:
        _, job = self.must("bootstrap", bootstrap_script(), 3600)
        return {"job": job["id"]}

    def install(self) -> dict:
        _, job = self.must("install", engine_script("install", 14400), 14400 + 900)
        return {"job": job["id"], "sanity_cmd": SANITY_CMD}

    def rerun(self) -> dict:
        out, job = self.must("rerun", engine_script("install", 3600), 3600, want_stdout=True)
        executed = out.count('"executed"')
        if executed:
            raise SessionError(f"rerun: {executed} step(s) executed; every step must be skipped")
        status_out, status_job = self.must("status", engine_script("status", 600), 900, want_stdout=True)
        receipt = parse_json_object(status_out)
        health = receipt.get("health") or {}
        if receipt.get("status") != "installed" or health.get("status") != "healthy":
            raise SessionError(f"engines status: status {receipt.get('status')!r}, health {health.get('status')!r}")
        return {"rerun_job": job["id"], "status_job": status_job["id"], "executed": 0, "status": "installed",
                "health": "healthy"}

    def action(self, which: str, request: str, seconds: int, timeout: float) -> dict:
        out_dir = f"{CLIP_DIR}/out-{self.sid}-{which}"
        _, job = self.must(which, action_script(f"{self.sid}-{which}", f"{CLIP_DIR}/{request}",
                                                f"{CLIP_DIR}/asset-map.json", out_dir), timeout)
        summary = parse_json_object(self.fetch(f"{CLIP_DIR}/runs/{self.sid}-{which}.summary.json"))
        if summary.get("ok") is not True:
            raise SessionError(f"{which}: action summary ok is {summary.get('ok')!r}")
        self.state[which] = summary
        return {"job": job["id"], "out": out_dir, "seconds": seconds}

    def s0(self) -> dict:
        return self.action("s0", "smoke-request.json", SMOKE_SECONDS, 3600)

    def clip(self) -> dict:
        detail = self.action("clip", "request.json", CLIP_SECONDS, 5400)
        summary = self.state["clip"]
        receipt = json.loads(self.fetch(summary.get("receipt") or f"{detail['out']}/{FINAL_RECEIPT}"))
        if not publication_verified(receipt):
            raise SessionError("clip: states.final_composition_publication is not verified")
        detail["final_composition_publication"] = "verified"
        return detail

    def download(self) -> dict:
        summary = self.state.get("clip")
        if not summary:
            raise SessionError("download: no CLIP summary")
        out = summary["out"]
        manifest = summary.get("manifest") if isinstance(summary.get("manifest"), str) and summary.get("manifest") else f"{out}/{COMPOSITION_MANIFEST}"
        remote = {"video": f"{out}/{FINAL_VIDEO}", "receipt": summary.get("receipt") or f"{out}/{FINAL_RECEIPT}",
                  "manifest": manifest}
        dest = self.workdir / "download"
        local, digests = self.download_files(remote, dest)
        receipt = json.loads(local["receipt"].read_text(encoding="utf-8"))
        if not publication_verified(receipt):
            raise SessionError("download: the downloaded receipt does not show a verified final composition")
        attested = attested_object_id(receipt)
        if attested and attested != "sha256:" + digests["video"]:
            raise SessionError("download: the final video's sha256 differs from the receipt's published object id")
        info = self.probe(local["video"])
        check_video(info)
        return {"dir": str(dest), "sha256": digests, "video": info, "receipt_attests_video": bool(attested)}

    def download_files(self, remote: dict[str, str], dest: Path) -> tuple[dict, dict]:
        """Rehash downloaded bytes against the pod, in addition to transport verification."""
        code, listing, _ = self.execute("remote sha256", "sha256sum " + " ".join(shlex.quote(p) for p in remote.values()))
        remote_sums = {}
        for line in listing.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                remote_sums[parts[1].strip().lstrip("*")] = parts[0]
        if code != 0 or any(p not in remote_sums for p in remote.values()):
            raise SessionError("download: the pod could not hash the expected outputs")
        dest.mkdir(parents=True, exist_ok=True)
        local, digests = {}, {}
        for kind, path in remote.items():
            target = dest / kind
            # Legacy download names are the remote basenames; packet keys carry subdirectories.
            if kind in ("video", "receipt", "manifest"):
                target = dest / Path(path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            self.transport.get(path, target)
            digests[kind] = _file_sha256(target)
            if digests[kind] != remote_sums[path]:
                raise SessionError(f"download: sha256 of the {kind} differs from the pod's")
            local[kind] = target
        return local, digests

    def _status(self, pod: str) -> int | None:
        try:
            return self.http_status(pod)
        except Exception:
            return None

    def release(self) -> dict:
        if self.released:
            return {"already": True}
        if self.transport is not None:
            try:
                self.transport.disconnect()
            except Exception:
                pass
        env = self.cli_env
        pods = [self.pod_id] if self.pod_id else []
        if not pods:  # a failed launch can leave a pod behind: sweep by this session's name
            code, out, _ = self.cli(["list", "--name-prefix", self.name, "--json"], env)
            if code == 0:
                try:
                    pods = [p["id"] for p in json.loads(out) if isinstance(p, dict) and p.get("id")]
                except ValueError:
                    pass
        for pod in pods:
            code, _out, err = self.cli(["terminate", pod, "--yes"], env)
            if code != 0:
                raise SessionError(f"release: terminate {pod} failed (exit {code}); terminate it by hand: {safe_tail(err, 300)}")
        if pods:
            for attempt in range(12):
                statuses = [self._status(p) for p in pods]
                code, out, _ = self.cli(["list", "--json"], env)
                listed = json.loads(out) if code == 0 and out.strip() else None
                if all(s == 404 for s in statuses) and listed == []:
                    break
                if attempt == 11:
                    raise SessionError(f"release: pod(s) {pods} not gone (GET {statuses}, list {listed!r}); terminate by hand")
                self.sleep(10)
        self.released = True
        if self.token_path is not None:
            try:
                self.token_path.unlink()
            except OSError:
                pass
        return {"pods": pods, "get": 404, "list": []}

    # -- progress -----------------------------------------------------------------------------
    def progress_document(self) -> bytes:
        return (json.dumps({"schema": "astrid.pod-session-progress.v1", "session": self.sid, "pod_id": self.pod_id,
                            "jobs": self.budget.count, "steps": self.records}, indent=2, sort_keys=True) + "\n").encode()

    def write_progress(self, *, strict: bool) -> None:
        local = self.workdir / "pod-session-progress.json"
        local.write_bytes(self.progress_document())
        if self.transport is None or self.released:
            return
        try:
            self.budget.charge("progress")  # the progress job is itself a record, counted in the document it writes
            job = self.transport.detached_start(progress_script(self.progress_document()), root=JOB_ROOT)
            if self.wait_job(job, 300) != 0:
                raise SessionError(f"progress: job {job['id']} failed")
            self.transport.get(PROGRESS_REMOTE, self.workdir / "pod-session-progress.remote.json")
            local.write_bytes(self.progress_document())
        except Exception:
            if strict:
                raise

    # -- driver -------------------------------------------------------------------------------
    def record(self, step: str, result: str, detail: Any) -> None:
        self.records.append({"step": step, "time": now(), "result": result, "detail": detail, "jobs": self.budget.count})
        self.say(f"[{step}] {result} jobs={self.budget.count} {json.dumps(detail, default=str)[:300]}")

    def run(self) -> int:
        failure: BaseException | None = None
        try:
            for step, operation in self.run_steps():
                try:
                    detail = operation()
                except BaseException as exc:
                    self.record(step, "failed", {"error": str(exc) or type(exc).__name__})
                    raise
                self.record(step, "ok", detail)
                self.write_progress(strict=True)
        except BaseException as exc:  # fail closed: stop, then release below
            failure = exc
            if not isinstance(exc, BudgetExceeded):
                try:
                    self.write_progress(strict=False)
                except Exception:
                    pass
            self.say(f"STOPPED: {exc}" if isinstance(exc, SessionError) else f"STOPPED: {type(exc).__name__}: {exc}")
        released = True
        try:
            self.record("release", "ok", self.release())
        except Exception as exc:
            released = False
            self.record("release", "failed", {"error": str(exc), "pod_id": self.pod_id})
            self.say(f"RELEASE FAILED for pod {self.pod_id}: terminate it by hand")
        try:
            (self.workdir / "pod-session-progress.json").write_bytes(self.progress_document())
        except OSError:
            pass
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        ok = failure is None and released
        self.say("POD SESSION " + ("COMPLETE" if ok else "FAILED") + f" (agent job records used: {self.budget.count})")
        return 0 if ok else 1

    def run_steps(self):
        return [(step, getattr(self, step)) for step in STEPS[:-1]]


class JobListSession(Session):
    """Job-list policy on the existing claim, detached-job, transfer and release stack.

    Jobs share the existing bootstrapped H3 engine environment. A cmd job may invoke
    any installed command, including generate; this runner does not install a second runtime.
    """

    def __init__(self, args, **kwargs):
        self.jobs = load_jobs(Path(args.jobs))
        super().__init__(args, **kwargs)
        for job in self.jobs:
            packet = self.workdir / "packets" / job["id"]
            if packet.exists():
                raise SessionError(f"packet already exists; choose a fresh workdir: {packet}")
            job["packet"] = packet
            job["remote"] = f"{CLIP_DIR}/jobs/{self.sid}-{job['id']}"

    def packet_check(self, job, status, **detail):
        packet = job["packet"]
        packet.mkdir(parents=True, exist_ok=True)
        check = {"schema": "astrid.pod-job-check.v1", "id": job["id"], "type": job["type"],
                 "status": status, **detail}
        (packet / "machine-check.json").write_text(json.dumps(check, indent=2, sort_keys=True) + "\n")
        (packet / "README.md").write_text(
            f"# {job['id']}\n\nGoal: {job.get('goal', job['id'])}\n\n"
            f"Prompt: {job.get('prompt', '')}\n\nMachine check: **{status}**. "
            "See [machine-check.json](machine-check.json) for hashes and checks.\n\n"
            "Original inputs are in `inputs/`; downloaded outputs are in `outputs/`. "
            "`side-by-side.mp4`, when present, shows the reference beside the H3 output.\n",
            encoding="utf-8")
        return check

    def bundle(self):
        detail = self.bundle_sources()
        for job in self.jobs:
            packet, remote = job["packet"], job["remote"]
            inputs = packet / "inputs"
            inputs.mkdir(parents=True)
            (packet / "job.json").write_text(json.dumps(
                {k: v for k, v in job.items() if k not in ("packet", "remote")}, default=str, indent=2) + "\n")
            self.packet_check(job, "pending")

            def stage(local, relative):
                target = inputs / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local, target)
                self.put(target, f"{remote}/{relative}")

            if job["type"] == "h3":
                clip, smoke = derive_requests(job["request"])
                amap = {}
                for asset, local in job["assets"].items():
                    relative = f"assets/{asset}{local.suffix}"
                    stage(local, relative)
                    amap[asset] = f"{remote}/{relative}"
                for name, data in (("request.json", clip), ("smoke-request.json", smoke),
                                   ("asset-map.json", (json.dumps(amap, indent=2) + "\n").encode())):
                    target = inputs / name
                    target.write_bytes(data)
                    self.put(target, f"{remote}/{name}")
                if job.get("asset-map"):
                    shutil.copyfile(job["asset-map"], inputs / "original-asset-map.json")
            else:
                for relative, local in job["input-files"].items():
                    stage(local, relative)
        return detail | {"jobs": [j["id"] for j in self.jobs]}

    def sanity(self):
        _, job = self.must("GPU sanity", SANITY_CMD, 600)
        return {"job": job["id"], "command": SANITY_CMD}

    def run_manifest_job(self, job, smoke=False):
        remote, jid = job["remote"], job["id"]
        label = f"h3:{jid}:s0" if smoke else f"job:{jid}"
        if job["type"] == "h3":
            name = f"{self.sid}-{jid}-{'s0' if smoke else 'clip'}"
            out = f"{remote}/{'s0' if smoke else 'out'}"
            request = "smoke-request.json" if smoke else "request.json"
            command = action_script(name, f"{remote}/{request}", f"{remote}/asset-map.json", out)
        else:
            command = "\n".join(["set -eu", "export PATH=/opt/astrid/bin:$PATH",
                                 f"mkdir -p {shlex.quote(remote)}", f"cd {shlex.quote(remote)}", job["command"]]) + "\n"
        code, stdout, handle = self.run_job(label, command, job["timeout"], want_stdout=True)
        logs = job["packet"] / "logs"
        logs.mkdir(exist_ok=True)
        phase = "s0" if smoke else "job"
        (logs / f"{phase}.stdout").write_text(stdout, encoding="utf-8")
        (logs / f"{phase}.stderr").write_text(self.fetch(handle["dir"] + "/stderr"), encoding="utf-8")
        if code:
            raise SessionError(f"{label}: job {handle['id']} exited {code}")
        if job["type"] == "h3":
            summary = parse_json_object(self.fetch(f"{CLIP_DIR}/runs/{name}.summary.json"))
            if summary.get("ok") is not True or summary.get("out") != out:
                raise SessionError(f"{label}: H3 action did not publish a successful output summary")
            if not smoke:
                job["summary"] = summary
        return {"job": handle["id"], "type": job["type"]}

    def verify_output(self, path, spec):
        kind = spec["kind"]
        info = {"kind": kind, "bytes": path.stat().st_size, "sha256": _file_sha256(path)}
        if not info["bytes"]:
            raise SessionError(f"empty output: {path.name}")
        if kind == "json":
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise SessionError(f"JSON output must be an object: {path.name}")
        elif kind in ("mp4", "png"):
            if kind == "png":
                with path.open("rb") as stream:
                    if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                        raise SessionError(f"output is not a PNG: {path.name}")
                decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                                         capture_output=True, timeout=300)
                if decoded.returncode:
                    raise SessionError(f"PNG decode failed: {path.name}")
            media = self.probe(path)
            if any(not isinstance(media.get(k), int) or media[k] <= 0 for k in ("width", "height", "frames")):
                raise SessionError(f"invalid media dimensions/frames: {path.name}")
            for key in ("width", "height"):
                if key in spec and media[key] != spec[key]:
                    raise SessionError(f"{path.name}: {key} {media[key]} != {spec[key]}")
            info.update(media)
        return info

    def download_manifest_job(self, job):
        if job["type"] == "h3":
            summary = job["summary"]
            out = summary["out"]
            remote = {"final.mp4": f"{out}/{FINAL_VIDEO}",
                      "receipt.json": summary.get("receipt") or f"{out}/{FINAL_RECEIPT}",
                      "composition-manifest.json": summary.get("manifest") or f"{out}/{COMPOSITION_MANIFEST}"}
            specs = {p: {"kind": "mp4" if p.endswith(".mp4") else "json"} for p in remote}
        else:
            specs = {o["path"]: o for o in job["expected-outputs"]}
            remote = {p: f"{job['remote']}/{p}" for p in specs}
        local, digests = self.download_files(remote, job["packet"] / "outputs")
        outputs = {p: self.verify_output(path, specs[p]) for p, path in local.items()}
        if job["type"] == "h3":
            receipt = json.loads(local["receipt.json"].read_text(encoding="utf-8"))
            if not publication_verified(receipt):
                raise SessionError("H3 downloaded receipt does not verify final composition publication")
            attested = attested_object_id(receipt)
            if attested and attested != "sha256:" + digests["final.mp4"]:
                raise SessionError("H3 video differs from the receipt's published object id")
            check_video(outputs["final.mp4"])
        job["outputs"] = outputs
        return {"packet": str(job["packet"]), "sha256": digests}

    def finish_packet(self, job):
        if job["type"] == "h3":
            # A local review derivative; the verified downloaded MP4 remains the original.
            references = [p for p in (job["packet"] / "inputs" / "assets").iterdir()
                          if p.suffix.lower() in (".mp4", ".png", ".jpg", ".jpeg", ".webp")]
            if references:
                reference = references[0]
                source = json.loads(job["request"].read_bytes()).get("source")
                if isinstance(source, dict) and source.get("asset") in job["assets"]:
                    asset = source["asset"]
                    candidate = job["packet"] / "inputs" / "assets" / (asset + job["assets"][asset].suffix)
                    if candidate in references:
                        reference = candidate
                dest = job["packet"] / "outputs" / "side-by-side.mp4"
                filters = ("[0:v]scale=512:288:force_original_aspect_ratio=decrease,"
                           "pad=512:288:(ow-iw)/2:(oh-ih)/2,setsar=1[a];"
                           "[1:v]scale=512:288,setsar=1[b];[a][b]hstack=inputs=2:shortest=1[v]")
                loop = ["-stream_loop", "-1"] if reference.suffix.lower() == ".mp4" else ["-loop", "1"]
                done = subprocess.run(["ffmpeg", "-v", "error", *loop, "-i", str(reference), "-i",
                                       str(job["packet"] / "outputs" / "final.mp4"), "-filter_complex", filters,
                                       "-map", "[v]", "-map", "1:a?", "-t", str(CLIP_SECONDS), "-r", "24",
                                       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                       "-c:a", "aac", str(dest)], capture_output=True, timeout=300)
                if done.returncode:
                    raise SessionError("could not create H3 side-by-side review video")
                job["outputs"]["side-by-side.mp4"] = self.verify_output(dest, {"kind": "mp4"})
        self.packet_check(job, "passed", outputs=job["outputs"])
        return {"packet": str(job["packet"]), "machine_check": "passed"}

    def record(self, step, result, detail):
        super().record(step, result, detail)
        if result == "failed":
            for job in self.jobs:
                if step in (f"job:{job['id']}", f"download:{job['id']}", f"packet:{job['id']}", f"h3:{job['id']}:s0"):
                    self.packet_check(job, "failed", error=detail.get("error"), outputs=job.get("outputs", {}))

    def run_steps(self):
        steps = [(step, getattr(self, step)) for step in ("claim", "bundle", "bootstrap", "sanity", "install", "rerun")]
        for job in self.jobs:
            if job["type"] == "h3":
                steps.append((f"h3:{job['id']}:s0", lambda j=job: self.run_manifest_job(j, smoke=True)))
            steps.extend([(f"job:{job['id']}", lambda j=job: self.run_manifest_job(j)),
                          (f"download:{job['id']}", lambda j=job: self.download_manifest_job(j)),
                          (f"packet:{job['id']}", lambda j=job: self.finish_packet(j))])
        return steps


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datacenters", required=True, type=lambda s: [d.strip() for d in s.split(",") if d.strip()],
                   help=f"the single C0 datacenter (must be {DATACENTER})")
    p.add_argument("--storage-name", default=STORAGE_NAME,
                   help=f"existing RunPod network volume (must be {STORAGE_NAME})")
    p.add_argument("--gpus", default=",".join(DEFAULT_GPUS), type=lambda s: [g.strip() for g in s.split(",") if g.strip()],
                   help="GPU names in order of preference (default: %(default)s)")
    p.add_argument("--sources-dir", required=True, help="directory holding runpod-python astrid-runtime runpod-lifecycle VibeComfy")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--request", help="the 9 s clip request JSON (requires --asset-map)")
    mode.add_argument("--jobs", help="version: 1 YAML job list (h3 request/assets or cmd command/input-files/expected-outputs)")
    p.add_argument("--asset-map", help="JSON map of request asset ids to local files")
    p.add_argument("--workdir", required=True, help="local directory for the bundle, downloads and progress")
    p.add_argument("--token-dir", default=str(Path.home() / ".astrid-pod-tokens"), help="directory for the per-claim token file")
    p.add_argument("--container-disk-gb", type=int, help="default: max(120, 1.5 x the engine install footprint)")
    p.add_argument("--volume-gb", type=int, default=10)
    p.add_argument("--image", help="default: the image in astrid/packs/runpod/environment.linux-cu130.json")
    p.add_argument("--wait-capacity", type=int, default=0, help="seconds to retry each GPU/datacenter claim")
    p.add_argument("--poll-interval", type=float, default=30.0)
    p.add_argument("--runpod-cli", default=None, help="runpod-lifecycle executable (default: beside this Python, else PATH)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.request and not args.asset_map:
        parser.error("--request requires --asset-map")
    if args.jobs and args.asset_map:
        parser.error("--asset-map belongs to --request; use assets/asset-map in each h3 job")
    binary = args.runpod_cli
    if binary is None:
        sibling = Path(sys.executable).parent / "runpod-lifecycle"
        binary = str(sibling) if sibling.exists() else "runpod-lifecycle"
    try:
        session = (JobListSession if args.jobs else Session)(
            args, cli=default_cli_runner(binary), transport_factory=default_transport_factory, http_status=default_http_status)
    except (SessionError, ValueError) as exc:
        parser.error(str(exc))
    return session.run()


if __name__ == "__main__":
    sys.exit(main())
