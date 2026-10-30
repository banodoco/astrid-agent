#!/usr/bin/env python3
"""One command for a whole GPU pod session, fail closed at every step.

Usage: pod_session.py --datacenters EUR-NO-1 --storage-name storage --sources-dir DIR --workdir DIR
                      (--jobs jobs.yaml | --request job/request.json --asset-map job/asset-map.json)

Legacy steps: claim, bundle, bootstrap, install, rerun, S0, CLIP, download, release.
Job lists use install-owned GPU sanity after environments and before models, then S0 for each H3 job,
execution, verified download and review packet for each job, followed by release.
The first failing step stops the run; release is attempted and reports only observed evidence.
Known pods require terminate, GET 404 and account list []; unknown allocations need reconciliation.

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
import fcntl
import hashlib
import io
import json
import math
import os
import re
import secrets
import shlex
import shutil
import stat
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
CACHE_MOUNT = "/workspace"
MODEL_CACHE_ROOT = CACHE_MOUNT + "/astrid/engines/vibecomfy-h3/models"
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


class JobRowError(SessionError):
    """A completed job or its local result failed while the session remains usable."""


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


_STORAGE_DIAGNOSTIC_REASONS = {
    "mountinfo": {"malformed", "error"},
    "paths": {"invalid", "symlink", "not_separate", "error"},
    "mount": {"not_directory", "absent_or_ambiguous", "device_mismatch", "invalid_request", "error"},
    "identity": {"mismatch", "invalid_request"},
    "cache": {"nested_mount", "unsafe", "error"},
    "capacity": {"invalid_requirement", "insufficient", "error"},
    "local": {"same_filesystem", "invalid_requirement", "insufficient", "error"},
    "complete": {"verified"},
    "unknown": {"error"},
}


def parse_storage_diagnostic(stderr: str) -> dict | None:
    """Accept only one bounded record with enumerated, non-sensitive fields."""
    prefix = "ASTRID_STORAGE_DIAGNOSTIC "
    records = [line[len(prefix):] for line in stderr.splitlines() if line.startswith(prefix)]
    if len(records) != 1 or len(records[0]) > 512:
        return None

    def unique_fields(pairs):
        value = {}
        for key, field in pairs:
            if key in value:
                raise ValueError("ambiguous diagnostic field")
            value[key] = field
        return value

    try:
        value = json.loads(records[0], object_pairs_hook=unique_fields)
        if (not isinstance(value, dict)
                or set(value) != {"schema", "stage", "reason", "identity_matches"}
                or value["schema"] != "astrid.pod-storage-diagnostic.v1"
                or not isinstance(value["stage"], str) or not isinstance(value["reason"], str)
                or value["reason"] not in _STORAGE_DIAGNOSTIC_REASONS.get(value["stage"], set())
                or (value["identity_matches"] is not None and type(value["identity_matches"]) is not bool)):
            return None
        return value
    except (TypeError, ValueError, KeyError):
        return None


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


def engine_script(verb: str, deadline: int, storage: dict | None = None) -> str:
    lines = ["set -eu", "export PATH=/opt/astrid/bin:$PATH"]
    if verb == "install":
        lines.append(f"export ASTRID_ENGINE_SANITY_CMD={shlex.quote(SANITY_CMD)}")
    cache_option = f" --model-cache-root {shlex.quote(MODEL_CACHE_ROOT)}" if verb == "install" else ""
    if verb == "install" and storage is not None:
        cache_option += f" --model-cache-device {storage['cache_device']} --model-cache-mount-id {storage['cache_mount_id']}"
    lines.append(f"astrid engines {verb} {ENGINE} --placement local --root {ENGINE_ROOT} --deadline {deadline}{cache_option}")
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

def controller_source_paths(sources_dir: Path) -> tuple[Path, Path]:
    """Use the same SDK/lifecycle sources locally that this session bundles."""
    sources = Path(sources_dir).resolve()
    paths = (sources / "runpod-python", sources / "runpod-lifecycle" / "src")
    for root, marker in zip(paths, ("runpod/api/mutations/pods.py", "runpod_lifecycle/cli.py")):
        if not (root / marker).is_file():
            raise SessionError(f"controller source is missing: {root / marker}")
    return paths


def capacity_wait_event(line: str, configured_wait: int) -> dict | None:
    """Accept only the fixed lifecycle progress record, never arbitrary stderr."""
    prefix = "RUNPOD_CAPACITY_WAIT "
    if not line.startswith(prefix) or not configured_wait:
        return None
    try:
        event = json.loads(line[len(prefix):])
    except (ValueError, RecursionError):
        return None
    if not isinstance(event, dict) or set(event) != {"placement_attempt", "retry_interval", "wait_seconds"}:
        return None
    attempt, interval, wait = (event[k] for k in ("placement_attempt", "retry_interval", "wait_seconds"))
    if (type(attempt) is not int or not 1 <= attempt <= 2**31 - 1
            or type(wait) is not int or wait != configured_wait
            or type(interval) not in (int, float) or not math.isfinite(interval)
            or not 0 < interval <= wait):
        return None
    return {"placement_attempt": attempt, "retry_interval": interval, "wait_seconds": wait}


def default_cli_runner(binary: str, source_paths: tuple[Path, ...] = ()):
    def run(argv: list[str], env: dict[str, str], *, on_capacity_wait=None) -> tuple[int, str, str]:
        if source_paths:
            env = dict(env, PYTHONPATH=os.pathsep.join(
                [*(str(p) for p in source_paths), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]))
        command = [binary, "--transport", "https", *argv]
        if on_capacity_wait is None:
            done = subprocess.run(command, env=env, capture_output=True, text=True)
            return done.returncode, done.stdout, done.stderr
        # Keep normal CLI output private while reading a dedicated hook-only pipe.
        # Spooling both outputs also prevents a full stdout or stderr pipe blocking launch.
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "r", encoding="utf-8", errors="replace") as progress, \
                os.fdopen(write_fd, "w", encoding="utf-8") as progress_writer, \
                tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout, \
                tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
            proc = subprocess.Popen([*command, "--capacity-progress-fd", str(write_fd)], env=env,
                                    stdout=stdout, stderr=stderr, pass_fds=(write_fd,))
            progress_writer.close()
            try:
                for line in progress:
                    on_capacity_wait(line)
                code = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            stdout.seek(0)
            stderr.seek(0)
            return code, stdout.read(), stderr.read()

    run.supports_capacity_progress = True
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
        self._strict_transport = bool(getattr(args, "hold_on_row_failure", False))
        self._held_doc: dict | None = None
        self.records: list[dict] = []
        self.state: dict[str, Any] = {}
        self._cache_binding: dict[str, int] = {}
        self.cli_env = {k: v for k, v in os.environ.items()
                        if k not in ("RUNPOD_STORAGE_NAME", "RUNPOD_STORAGE_VOLUMES", "RUNPOD_ATTACH_ONLY")}

    # -- agent jobs ---------------------------------------------------------------------------
    def _persist_held_operation(self) -> None:
        self._write_held(self._held_doc)
        if self._read_held() != self._held_doc:
            raise SessionError("held state: operation publication differs")

    def _reserve_held_job(self, label: str) -> str:
        doc = self._held_doc
        prior = doc["operation"]
        if prior is not None and prior["state"] != "exited":
            raise SessionError("held state: prior operation needs terminal proof")
        self.budget.charge(label)
        job_id = secrets.token_hex(16)
        # Reserve both the identity and the charge before dispatch. A lost reply
        # may have consumed the record, so an uncertain reservation is never refunded.
        doc.update(charged_jobs=self.budget.count,
                   operation={"id": job_id, "label": label, "state": "starting", "exit_code": None})
        self._persist_held_operation()
        return job_id

    def _observe_held_terminal(self, job: dict, state: dict) -> bool:
        if self._held_doc is None:
            return False
        operation = self._held_doc["operation"]
        if (not isinstance(state, dict) or state.get("id") != job["id"]
                or state.get("dir") != JOB_ROOT + "/" + job["id"]
                or state.get("state") != "exited" or type(state.get("exit_code")) is not int
                or not 0 <= state["exit_code"] <= 255
                or operation is None or operation["id"] != job["id"]):
            return False
        operation.update(state="exited", exit_code=state["exit_code"])
        self._persist_held_operation()
        return True

    def _reconcile_held_operation(self) -> bool:
        operation = self._held_doc["operation"]
        if operation is None:
            return False
        # Even a saved terminal receipt is corroborated on a later invocation.
        # Missing/starting/running/cancelled are all uncertain. Cancellation's
        # shell writes its marker without verifying process-group death.
        state = self.transport.detached_poll(operation["id"], root=JOB_ROOT)
        return self._observe_held_terminal(operation, state)

    def start_job(self, label: str, command: str) -> dict:
        if self._held_doc is not None:
            job_id = self._reserve_held_job(label)
            job = self.transport.detached_start(command, root=JOB_ROOT, job_id=job_id)
            if (not isinstance(job, dict) or job.get("id") != job_id
                    or job.get("dir") != JOB_ROOT + "/" + job_id):
                raise SessionError("held state: detached launch identity differs")
            return job
        self.budget.charge(label)
        return self.transport.detached_start(command, root=JOB_ROOT)

    def execute(self, label: str, command: str, timeout: float = 600) -> tuple[int, str, str]:
        if self._held_doc is not None:
            code, out, job = self.run_job(label, command, timeout, want_stdout=True, strict_fetch=True)
            return code, out, self.fetch(job["dir"] + "/stderr")
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
            except Exception as exc:  # ordinary sessions may retry transient read errors
                if self._strict_transport:
                    raise SessionError(f"job {job['id']}: poll failed ({type(exc).__name__})") from None
                errors += 1
                if errors > 20:
                    raise SessionError(f"job {job['id']}: poll failed {errors} times ({type(exc).__name__})") from None
                self.sleep(self.args.poll_interval)
                continue
            if state.get("state") == "exited" and state.get("exit_code") is not None:
                if self._held_doc is not None and not self._observe_held_terminal(job, state):
                    raise SessionError("held state: invalid terminal receipt")
                return int(state["exit_code"])
            if state.get("state") in ("cancelled",):
                raise SessionError(f"job {job['id']} was cancelled")
            if time.monotonic() >= deadline:
                raise SessionError(f"job {job['id']} did not exit within {int(timeout)} s")
            self.sleep(self.args.poll_interval)

    def run_job(self, label: str, command: str, timeout: float, *, want_stdout: bool = False,
                strict_fetch: bool = False) -> tuple[int, str, dict]:
        job = self.start_job(label, command)
        try:
            code = self.wait_job(job, timeout)
        except BaseException:
            if self._held_doc is not None:
                # The held pod and operation remain in custody. The existing
                # cancel API creates an unidentifiable control job and cannot
                # prove death; do not spend another record or free the slot.
                raise
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
                if strict_fetch or self._strict_transport:
                    raise
        return code, out, job

    def must(self, label: str, command: str, timeout: float, **kw) -> tuple[str, dict]:
        code, out, job = self.run_job(label, command, timeout, **kw)
        if code != 0:
            raise SessionError(f"{label}: job {job['id']} exited {code}")
        return out, job

    # -- steps --------------------------------------------------------------------------------
    def capacity_wait_progress(self, line: str) -> None:
        # This pipe is a protocol, not provider output. Reject malformed, out-of-order,
        # or contradictory protocol records without copying their contents to evidence.
        if line == "RUNPOD_CAPACITY_EXHAUSTED\n":
            if self._progress_phase != "started":
                self._progress_invalid = True
            else:
                self._progress_phase = "exhausted"
            return
        prefixes = ("RUNPOD_PLACEMENT_ATTEMPT ", "RUNPOD_TERMINAL_FAILURE ", "RUNPOD_CAPACITY_WAIT ")
        prefix = next((p for p in prefixes if line.startswith(p)), None)
        if prefix is None:
            self._progress_invalid = True
            return
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate progress key")
                result[key] = value
            return result
        try:
            event = json.loads(line[len(prefix):], object_pairs_hook=unique_pairs)
        except (ValueError, RecursionError):
            self._progress_invalid = True
            return
        if not isinstance(event, dict):
            self._progress_invalid = True
            return
        attempt = event.get("placement_attempt")
        if type(attempt) is not int or not 1 <= attempt <= 2**31 - 1:
            self._progress_invalid = True
            return
        if prefix == prefixes[0]:
            if (set(event) != {"placement_attempt"} or attempt != self._progress_attempt + 1
                    or self._progress_phase not in ("initial", "waiting")):
                self._progress_invalid = True
                return
            self._progress_attempt = attempt
            self._progress_phase = "started"
            claim = self.state["claim_attempt"]
            claim.pop("retry_interval", None)
            claim.pop("wait_seconds", None)
            claim["placement_attempt"] = attempt
            self.write_progress(strict=True)
        elif prefix == prefixes[1]:
            if (set(event) != {"classification", "placement_attempt"}
                    or event.get("classification") not in ("allocation_unknown", "non_capacity_failure")
                    or attempt != self._progress_attempt or self._progress_phase != "started"):
                self._progress_invalid = True
                return
            self._progress_phase = "terminal"
            self._terminal_classification = event["classification"]
            claim = self.state["claim_attempt"]
            claim.update(state=event["classification"], classification=event["classification"],
                         reconciliation_required=True)
            self.write_progress(strict=True)
        else:
            event = capacity_wait_event(line, self.args.wait_capacity)
            if event is None or attempt != self._progress_attempt or self._progress_phase != "started":
                self._progress_invalid = True
                return
            self._progress_phase = "waiting"
            self.state["claim_attempt"].update(event)
            self.write_progress(strict=True)
            self.say(f"[claim] capacity retry after attempt {attempt}; "
                     f"waiting {event['retry_interval']}s (placement limit {event['wait_seconds']}s)")
            sys.stdout.flush()

    def claim(self) -> dict:
        from runpod_lifecycle import AllocationUnknown
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
                   RUNPOD_ATTACH_ONLY="true", RUNPOD_VOLUME_MOUNT_PATH=CACHE_MOUNT)
        self.cli_env = env
        tried = []
        for gpu in self.args.gpus:
            for dc in self.args.datacenters:
                self.state["claim_attempt"] = {"gpu": gpu, "datacenter": dc, "state": "launching",
                                                "wait_capacity_sec": self.args.wait_capacity, "started": now()}
                self.say(f"[claim] {gpu} @ {dc}: capacity wait {self.args.wait_capacity}s; known capacity only")
                self.write_progress(strict=True)  # local evidence exists while the lifecycle CLI waits
                argv = ["launch", "--detach", "--name", self.name, "--gpu-type", gpu, "--image", image,
                        "--datacenter-id", dc, "--allowed-cuda-versions", CUDA_VERSIONS,
                        "--container-disk-gb", str(disk), "--disk-size-gb", str(self.args.volume_gb),
                        "--storage-name", self.args.storage_name]
                if self.args.wait_capacity:
                    argv += ["--wait-capacity", str(self.args.wait_capacity)]
                self._progress_attempt = 0
                self._progress_phase = "initial"
                self._progress_invalid = False
                self._terminal_classification = None
                cli_exception = False
                try:
                    if getattr(self.cli, "supports_capacity_progress", False):
                        code, out, err = self.cli(argv, env, on_capacity_wait=self.capacity_wait_progress)
                    else:
                        code, out, err = self.cli(argv, env)
                except Exception:
                    cli_exception = True
                    code, out, err = 1, "", ""
                tried.append(f"{gpu}@{dc}")
                self.state["claim_attempt"]["state"] = "allocated" if code == 0 else "failed"
                # Never classify provider output or infer an attempt from a missing event.
                ambiguous = cli_exception or self._terminal_classification == AllocationUnknown.status
                if code == 0:
                    try:
                        pod_id = parse_json_object(out).get("pod_id")
                    except SessionError:
                        pod_id = None
                    valid_pod_id = isinstance(pod_id, str) and re.fullmatch(r"[a-z0-9]+", pod_id)
                    if valid_pod_id:
                        # Preserve custody even if the progress protocol is contradictory.
                        self.pod_id = pod_id
                    ambiguous = ambiguous or not valid_pod_id
                protocol_failure = (self._progress_invalid or self._progress_phase == "terminal"
                                    or (code == 0 and self._progress_phase in ("waiting", "exhausted")))
                if ambiguous or protocol_failure:
                    classification = (AllocationUnknown.status if ambiguous else
                                      "unknown" if self._progress_invalid else
                                      self._terminal_classification or "unknown")
                    claim_state = AllocationUnknown.status if ambiguous else "launch_failed"
                    self.state["claim_attempt"].update(state=claim_state, classification=classification,
                                                       reconciliation_required=True)
                    self.state["claim"] = {"state": claim_state, "classification": classification,
                                           "reconciliation_required": True, "gpu": gpu, "datacenter": dc,
                                           "attempted": tried, "exit_code": code,
                                           "wait_capacity_sec": self.args.wait_capacity}
                    if self._progress_attempt:
                        self.state["claim"]["placement_attempt"] = self._progress_attempt
                    if ambiguous:
                        raise SessionError("claim: allocation_unknown; reconcile the provider allocation before another launch")
                    raise SessionError("claim: non-capacity launch failure; reconcile before another launch")
                if code == 0:
                    self.pod_id = pod_id
                    self.transport = self.transport_factory(pod_id, read_token_file(self.token_path))
                    self.transport.wait_ready(timeout=600)
                    storage = self.verify_storage(footprint)
                    return {"pod_id": pod_id, "gpu": gpu, "datacenter": dc, "container_disk_gb": disk,
                            "volume_gb": self.args.volume_gb, "image": image,
                            "wait_capacity_sec": self.args.wait_capacity, "storage": storage}
                if self._progress_phase != "exhausted":
                    self.state["claim"] = {"state": "launch_failed", "reconciliation_required": True,
                                           "gpu": gpu, "datacenter": dc, "attempted": tried, "exit_code": code,
                                           "wait_capacity_sec": self.args.wait_capacity,
                                           "classification": "unknown"}
                    self.state["claim_attempt"].update(classification="unknown", reconciliation_required=True)
                    if self._progress_attempt:
                        self.state["claim"]["placement_attempt"] = self._progress_attempt
                    raise SessionError("claim: non-capacity launch failure; reconcile before another launch")
                self.say(f"[claim] {gpu} @ {dc}: no capacity (exit {code})")
        raise SessionError("claim: no pod from any of: " + ", ".join(tried))

    def verify_storage(self, footprint: int) -> dict:
        """Corroborate exact account attachment with live mount source and statvfs."""
        def listing(argv):
            code, out, _ = self.cli(argv, self.cli_env)
            try:
                result = json.loads(out)
            except (ValueError, TypeError):
                result = None
            if code or not isinstance(result, list) or any(not isinstance(v, dict) for v in result):
                raise SessionError("claim: storage observation is missing or ambiguous")
            return result
        volumes = [v for v in listing(["volumes", "ls", "--json"])
                   if v.get("name") == STORAGE_NAME and v.get("dataCenterId") == DATACENTER]
        if len(volumes) != 1 or not isinstance(volumes[0].get("id"), str) or not volumes[0]["id"]:
            raise SessionError("claim: requested existing volume identity is missing or ambiguous")
        volume_id = volumes[0]["id"]
        expected_volume_id = getattr(self.args, "storage_volume_id", None)
        if expected_volume_id is not None and volume_id != expected_volume_id:
            raise SessionError("claim: existing volume ID does not match the expected storage volume ID")
        pods = [v for v in listing(["list", "--json"]) if v.get("id") == self.pod_id]
        if len(pods) != 1:
            raise SessionError("claim: exact pod legacy observation is missing or ambiguous")
        legacy = pods[0].get("network_volume_evidence")
        if (not isinstance(legacy, dict) or set(legacy) != {"present", "valid", "conflict"}
                or any(type(value) is not bool for value in legacy.values())
                or not legacy["valid"] or legacy["conflict"]
                or pods[0].get("network_volume_id") not in (None, volume_id)):
            raise SessionError("claim: legacy attachment evidence is malformed or conflicting")
        legacy_path = pods[0].get("network_volume_mount_path")
        if legacy_path is not None and (not isinstance(legacy_path, str) or legacy_path != CACHE_MOUNT):
            raise SessionError("claim: legacy attachment path conflicts with the requested mount")
        legacy_objects = pods[0].get("legacy_object_mounts")
        if legacy_objects is not None:
            if not isinstance(legacy_objects, list) or len(legacy_objects) > 16:
                raise SessionError("claim: legacy object mount evidence is malformed")
            object_facts = []
            for item in legacy_objects:
                if (not isinstance(item, dict) or set(item) != {"volume_id", "mount_path", "read_only"}
                        or not isinstance(item["volume_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", item["volume_id"])
                        or not isinstance(item["mount_path"], str) or not item["mount_path"].startswith("/")
                        or str(Path(item["mount_path"])) != item["mount_path"] or ".." in Path(item["mount_path"]).parts
                        or "\\" in item["mount_path"] or any(ord(char) < 32 for char in item["mount_path"])
                        or type(item["read_only"]) is not bool):
                    raise SessionError("claim: legacy object mount evidence is malformed")
                object_facts.append((item["volume_id"], item["mount_path"], item["read_only"]))
            if len(set(object_facts)) != len(object_facts):
                raise SessionError("claim: legacy object mount evidence is duplicated")
        nonce = secrets.token_hex(16)
        code, out, _ = self.cli(["mounts", self.pod_id, "--nonce", nonce, "--json"], self.cli_env)
        if code:
            raise SessionError("claim: exact pod typed provider observation is unavailable")
        # Independently revalidate the CLI contract before any private upload.
        try:
            from pod_storage_probe import strict_json_object, validate_provider_mapping
        except ModuleNotFoundError:
            from scripts.pod_storage_probe import strict_json_object, validate_provider_mapping
        try:
            provider = strict_json_object(out)
            validate_provider_mapping(provider, pod_id=self.pod_id, volume_id=volume_id, nonce=nonce, mount=CACHE_MOUNT)
        except ValueError:
            raise SessionError("claim: exact pod typed provider observation is invalid") from None
        if legacy_objects is not None:
            typed_objects = [(item["volume_id"], item["mount_path"], item["read_only"])
                             for item in provider["volume_mounts"] if item["volume_type"] == "OBJECT_STORE_VOLUME"]
            if set(object_facts) != set(typed_objects):
                raise SessionError("claim: legacy object mounts conflict with typed provider observation")
        provider["legacy_evidence"] = {"present": legacy["present"] or provider["legacy_evidence"]["present"],
                                       "valid": True, "conflict": False}
        request = {"pod_id": self.pod_id, "volume_id": volume_id, "nonce": nonce,
                   "mount": CACHE_MOUNT, "model_cache_root": MODEL_CACHE_ROOT, "engine_root": ENGINE_ROOT,
                   "required_volume_bytes": self.args.volume_gb * 10**9,
                   "required_local_bytes": (footprint + 20) * 10**9, "provider_mapping": provider}
        source = (REPO / "scripts/pod_storage_probe.py").read_text()
        blob = base64.b64encode(json.dumps(request).encode()).decode()
        command = "python3 -c " + shlex.quote(source) + " " + shlex.quote(blob)
        code, out, err = self.execute("storage mount verification", command)
        diagnostic = parse_storage_diagnostic(err)
        if diagnostic is not None:
            self.state["storage_diagnostic"] = diagnostic
            path = self.workdir / "storage-mount-observation.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(diagnostic, handle, indent=2, sort_keys=True)
                handle.write("\n")
        if code:
            raise SessionError("claim: mounted storage verification failed")
        receipt = strict_json_object(out)
        expected = {key: value for key, value in request.items() if key != "provider_mapping"}
        if (set(receipt) != set(expected) | {"free_bytes", "total_bytes", "local_free_bytes", "cache_device", "cache_mount_id"}
                or any(type(receipt.get(k)) is not type(v) or receipt.get(k) != v for k, v in expected.items())
                or type(receipt.get("cache_device")) is not int or receipt["cache_device"] < 0
                or type(receipt.get("cache_mount_id")) is not int or receipt["cache_mount_id"] <= 0
                or type(receipt.get("total_bytes")) is not int
                or type(receipt.get("free_bytes")) is not int
                or receipt["free_bytes"] < request["required_volume_bytes"]
                or receipt["total_bytes"] < receipt["free_bytes"]
                or type(receipt.get("local_free_bytes")) is not int
                or receipt["local_free_bytes"] < request["required_local_bytes"]):
            raise SessionError("claim: storage evidence differs from the requested mount/capacity")
        # Keep transient live filesystem binding out of progress/state/logs.
        self._cache_binding = {key: receipt.pop(key) for key in ("cache_device", "cache_mount_id")}
        self.state["storage"] = receipt
        return receipt

    def put(self, local: Path, remote: str) -> None:
        for attempt in range(3):
            try:
                self.transport.put(local, remote)
                return
            except Exception:
                if self._strict_transport or attempt == 2:
                    raise
                self.sleep(20)

    def bundle_sources(self) -> dict:
        tar = self.workdir / "pod-src.tgz"
        size, digest = build_bundle(self.agent_root, Path(self.args.sources_dir), tar)
        with tarfile.open(tar) as archive:
            manifest = json.load(archive.extractfile(".pod-source.json"))
        (self.workdir / "source-identity.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
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

    def runtime_readiness(self, manifest: dict) -> tuple[dict, dict]:
        request = {"pod_id": self.pod_id, "volume_id": self.state["storage"]["volume_id"],
                   "nonce": secrets.token_hex(16), "runtime_head": manifest["heads"]["astrid-runtime"],
                   "runtime_lock_sha256": manifest["runtime_lock_sha256"]}
        blob = base64.b64encode(json.dumps(request).encode()).decode()
        out, probe = self.must("Runtime readiness", f"export PATH=/opt/astrid/bin:$PATH\npython3 {SRC_DIR}/astrid-agent/scripts/pod_runtime_ready.py {shlex.quote(blob)}", 600, want_stdout=True)
        receipt = parse_json_object(out)
        if any(receipt.get(k) != v for k, v in request.items()) or receipt.get("ready") is not True or receipt.get("stopped") is not True:
            raise SessionError("bootstrap: live Runtime identity/readiness evidence differs")
        self.state["runtime_ready"] = receipt
        return receipt, probe

    def bootstrap(self) -> dict:
        _, job = self.must("bootstrap", bootstrap_script(), 3600)
        manifest = json.loads((self.workdir / "source-identity.json").read_text())
        receipt, probe = self.runtime_readiness(manifest)
        return {"job": job["id"], "runtime_job": probe["id"], "runtime": receipt}

    def install(self) -> dict:
        _, job = self.must("install", engine_script("install", 14400, self._cache_binding), 14400 + 900)
        return {"job": job["id"], "sanity_cmd": SANITY_CMD}

    def rerun(self) -> dict:
        out, job = self.must("rerun", engine_script("install", 3600, self._cache_binding), 3600, want_stdout=True)
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
        evidence = {"pods": pods if pods else None, "get": None, "list": None}
        self.state["release"] = evidence
        unresolved = self.state.get("claim", {}).get("reconciliation_required", False)
        if unresolved:
            evidence.update(status="unresolved", reconciliation_required=True)

        def list_pods(*, session_only: bool = False) -> list[dict] | None:
            evidence.update(list=None, list_scope="session" if session_only else "account")
            evidence.pop("list_error", None)
            argv = ["list", *(["--name-prefix", self.name] if session_only else []), "--json"]
            try:
                code, out, _ = self.cli(argv, env)
            except Exception as exc:
                evidence.update(list_exit_code=None, list_error=f"CLI unavailable ({type(exc).__name__})")
                return None
            evidence["list_exit_code"] = code
            if code != 0:
                evidence["list_error"] = f"CLI exited {code}"
                return None
            try:
                listed = json.loads(out)
            except (TypeError, ValueError):
                evidence["list_error"] = "invalid JSON"
                return None
            if not isinstance(listed, list) or any(
                not isinstance(p, dict) or not isinstance(p.get("id"), str)
                or not re.fullmatch(r"[a-z0-9]+", p["id"]) for p in listed
            ):
                evidence["list_error"] = "expected a list of pod objects with valid ids"
                return None
            # Only pod IDs are needed as evidence; omit unrelated provider fields.
            evidence["list"] = [{"id": p["id"]} for p in listed]
            return listed

        if not pods:  # a failed launch can leave a pod behind: sweep by this session's name
            listed = list_pods(session_only=True)
            if listed is None:
                raise SessionError("release: session pod listing unverified; reconciliation required")
            pods = [p["id"] for p in listed]
            evidence["pods"] = pods
        for pod in pods:
            code, _out, err = self.cli(["terminate", pod, "--yes"], env)
            if code != 0:
                raise SessionError(f"release: terminate {pod} failed (exit {code}); terminate it by hand: {safe_tail(err, 300)}")
        if pods:
            for attempt in range(12):
                statuses = [self._status(p) for p in pods]
                evidence["get"] = statuses[0] if len(statuses) == 1 else statuses
                listed = list_pods()
                if all(s == 404 for s in statuses) and listed == []:
                    break
                if attempt == 11:
                    raise SessionError(f"release: pod(s) {pods} not gone or absence unverified (GET {statuses}); terminate by hand")
                self.sleep(10)
        if unresolved:
            raise SessionError("release: claim outcome remains unresolved; reconcile the provider allocation")
        self.released = True
        if self.token_path is not None:
            try:
                self.token_path.unlink()
            except OSError:
                pass
        if pods:
            return {"pods": pods, "get": 404, "list": []}
        return evidence

    # -- progress -----------------------------------------------------------------------------
    def progress_document(self) -> bytes:
        return (json.dumps({"schema": "astrid.pod-session-progress.v1", "session": self.sid, "pod_id": self.pod_id,
                            "jobs": self.budget.count, "steps": self.records,
                            "storage_diagnostic": self.state.get("storage_diagnostic"),
                            "claim_attempt": self.state.get("claim_attempt")}, indent=2, sort_keys=True) + "\n").encode()

    def write_progress(self, *, strict: bool) -> None:
        local = self.workdir / "pod-session-progress.json"
        local.write_bytes(self.progress_document())
        if self.transport is None or self.released:
            return
        try:
            if self._held_doc is not None:
                # Generate progress after reserving its own charged record.
                job_id = self._reserve_held_job("progress")
                job = self.transport.detached_start(progress_script(self.progress_document()),
                                                    root=JOB_ROOT, job_id=job_id)
                if (not isinstance(job, dict) or job.get("id") != job_id
                        or job.get("dir") != JOB_ROOT + "/" + job_id):
                    raise SessionError("held state: progress launch identity differs")
            else:
                self.budget.charge("progress")  # the progress job counts itself in the document
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

    def continue_after_step_failure(self, step: str, exc: BaseException) -> bool:
        return False

    def should_run_step(self, step: str) -> bool:
        return True

    def run(self) -> int:
        lock = self.workdir / ".held-session.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.say("STOPPED: session workdir is in use")
                return 1
            if os.path.lexists(self._held_path()):
                self.say("STOPPED: workdir contains held custody; release or reconcile it first")
                return 1
            return self._run_session()
        finally:
            os.close(fd)

    def _run_session(self) -> int:
        # A reused work directory must not present a prior session observation.
        (self.workdir / "storage-mount-observation.json").unlink(missing_ok=True)
        failure: BaseException | None = None
        hold_eligible = False
        completed = False
        try:
            for step, operation in self.run_steps():
                if not self.should_run_step(step):
                    self.record(step, "skipped", {"reason": "earlier phase of this job failed"})
                    try:
                        self.write_progress(strict=True)
                    except BaseException as exc:
                        hold_eligible = False
                        self.record("progress", "failed", {"error": str(exc) or type(exc).__name__})
                        if failure is None:
                            failure = exc
                        self.say(f"STOPPED: {exc}" if isinstance(exc, SessionError) else
                                 f"STOPPED: {type(exc).__name__}: {exc}")
                        break
                    continue
                try:
                    detail = operation()
                except BaseException as exc:
                    detail = {"error": str(exc) or type(exc).__name__}
                    if step == "claim":
                        detail.update(self.state.get("claim", {}))
                    self.record(step, "failed", detail)
                    if failure is None:
                        failure = exc
                    if not self.continue_after_step_failure(step, exc):
                        hold_eligible = False
                        self.say(f"STOPPED: {exc}" if isinstance(exc, SessionError) else
                                 f"STOPPED: {type(exc).__name__}: {exc}")
                        break
                    hold_eligible = isinstance(exc, JobRowError)
                    try:
                        self.write_progress(strict=True)
                    except BaseException as progress_exc:
                        hold_eligible = False
                        self.record("progress", "failed", {"error": str(progress_exc) or type(progress_exc).__name__})
                        if failure is None:
                            failure = progress_exc
                        self.say(f"STOPPED: {progress_exc}" if isinstance(progress_exc, SessionError) else
                                 f"STOPPED: {type(progress_exc).__name__}: {progress_exc}")
                        break
                    continue
                self.record(step, "ok", detail)
                try:
                    self.write_progress(strict=True)
                except BaseException as exc:
                    hold_eligible = False
                    self.record("progress", "failed", {"error": str(exc) or type(exc).__name__})
                    if failure is None:
                        failure = exc
                    self.say(f"STOPPED: {exc}" if isinstance(exc, SessionError) else
                             f"STOPPED: {type(exc).__name__}: {exc}")
                    break
            else:
                completed = True
        except BaseException as exc:  # fail closed: stop, then release below
            hold_eligible = False
            if failure is None:
                failure = exc
            self.say(f"STOPPED: {exc}" if isinstance(exc, SessionError) else f"STOPPED: {type(exc).__name__}: {exc}")
        if (completed and hold_eligible and failure is not None
                and getattr(self.args, "hold_on_row_failure", False)):
            try:
                self.save_held_session()
                self.say("POD SESSION HELD after row failure")
                return 1
            except BaseException as exc:
                self.say(f"STOPPED: unable to persist held session ({type(exc).__name__})")
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    failure = exc
        if failure is not None and not isinstance(failure, BudgetExceeded):
            try:
                self.write_progress(strict=False)
            except Exception:
                pass
        released = True
        try:
            self.record("release", "ok", self.release())
        except Exception as exc:
            released = False
            self.record("release", "failed", {**self.state.get("release", {}), "error": str(exc), "pod_id": self.pod_id})
            self.say(f"RELEASE FAILED for pod {self.pod_id}: terminate it by hand" if self.pod_id else
                     f"RELEASE UNVERIFIED for session {self.name}: reconcile the provider allocation")
        try:
            (self.workdir / "pod-session-progress.json").write_bytes(self.progress_document())
        except OSError:
            pass
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        ok = failure is None and released
        self.say("POD SESSION " + ("COMPLETE" if ok else "FAILED") + f" (agent job records used: {self.budget.count})")
        return 0 if ok else 1

    def _held_path(self) -> Path:
        return self.workdir / "held-session.json"

    def _write_held(self, doc: dict, *, create: bool = False) -> None:
        path = self._held_path()
        if create and path.exists():
            raise SessionError("held state already exists; choose a fresh workdir")
        fd, temporary = tempfile.mkstemp(prefix=".held-session-", dir=self.workdir)
        created = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(doc, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if create:
                os.link(temporary, path)  # fail instead of replacing prior custody
                created = True
                os.unlink(temporary)
            else:
                os.replace(temporary, path)
            directory = os.open(self.workdir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save_held_session(self) -> None:
        if (not self.pod_id or not self.token_path or not self.transport
                or not self.state.get("storage") or not self.state.get("runtime_ready")):
            raise SessionError("held state: incomplete session")
        failed = [r for r in self.records if r["result"] == "failed"]
        if len(failed) != 1:
            raise SessionError("held state: primary failure is ambiguous")
        manifest = json.loads((self.workdir / "source-identity.json").read_text())
        ready = self.state["runtime_ready"]
        identity = ready.get("identity")
        if (not isinstance(identity, dict) or not identity
                or identity.get("agent_head") != manifest["heads"]["astrid-agent"]
                or identity.get("runtime_head") != manifest["heads"]["astrid-runtime"]
                or ready.get("pod_id") != self.pod_id or ready.get("volume_id") != self.state["storage"]["volume_id"]
                or ready.get("runtime_head") != manifest["heads"]["astrid-runtime"]
                or ready.get("runtime_lock_sha256") != manifest["runtime_lock_sha256"]
                or ready.get("ready") is not True or ready.get("stopped") is not True):
            raise SessionError("held state: Runtime identity differs")
        self.record("hold", "ok", {"primary_step": failed[0]["step"]})
        self.write_progress(strict=True)
        if ((self.workdir / "pod-session-progress.remote.json").read_bytes()
                != (self.workdir / "pod-session-progress.json").read_bytes()):
            raise SessionError("held state: remote progress differs")
        # Sync all local packet and progress evidence before making custody durable.
        evidence = [self.workdir / "pod-session-progress.json",
                    self.workdir / "pod-session-progress.remote.json",
                    self.workdir / "source-identity.json"]
        packet_root = self.workdir / "packets"
        for root, directories, files in os.walk(packet_root, topdown=False, followlinks=False):
            evidence.extend(Path(root) / name for name in files)
            evidence.extend(Path(root) / name for name in directories)
        evidence.extend((packet_root, self.workdir))
        for path in evidence:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(descriptor)
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise SessionError("held state: unsafe evidence")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        doc = {"schema": "astrid.pod-held-session.v1", "status": "held", "session": self.sid,
               "pod_id": self.pod_id, "volume_id": self.state["storage"]["volume_id"],
               "volume_gb": self.args.volume_gb, "source": manifest, "runtime_identity": identity,
               "primary_step": failed[0]["step"], "jobs": self.budget.count,
               "expires_at": time.time() + 3600,
               "progress_sha256": _file_sha256(self.workdir / "pod-session-progress.json"),
               "token_dir": str(Path(self.args.token_dir).resolve())}
        self._write_held(doc, create=True)
        try:
            self._read_held()  # reject an incomplete record before relinquishing custody
        except BaseException:
            self._held_path().unlink(missing_ok=True)
            raise

    def _read_held(self) -> dict:
        path = self._held_path()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise SessionError("held state: file protection invalid")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate held state key")
                    result[key] = value
                return result
            doc = json.load(handle, object_pairs_hook=unique)
        keys = {"schema", "status", "session", "pod_id", "volume_id", "volume_gb", "source",
                "primary_step", "jobs", "expires_at", "progress_sha256", "token_dir", "runtime_identity"}
        operation_keys = {"charged_jobs", "operation"}
        if (not isinstance(doc, dict) or set(doc) not in (keys, keys | operation_keys)
                or doc["schema"] != "astrid.pod-held-session.v1"
                or doc["status"] not in ("held", "in_use")
                or not isinstance(doc["session"], str) or not re.fullmatch(r"[0-9a-f]{8}", doc["session"])
                or not isinstance(doc["pod_id"], str) or not re.fullmatch(r"[a-z0-9]+", doc["pod_id"])
                or not isinstance(doc["volume_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", doc["volume_id"])
                or type(doc["volume_gb"]) is not int or doc["volume_gb"] <= 0
                or type(doc["jobs"]) is not int or not 0 <= doc["jobs"] < JOB_LIMIT
                or type(doc["expires_at"]) not in (float, int) or not math.isfinite(doc["expires_at"])
                or not isinstance(doc["primary_step"], str) or not doc["primary_step"]
                or not isinstance(doc["progress_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", doc["progress_sha256"])
                or not isinstance(doc["token_dir"], str) or not Path(doc["token_dir"]).is_absolute()
                or not isinstance(doc["runtime_identity"], dict) or not doc["runtime_identity"]
                or not isinstance(doc["source"], dict) or set(doc["source"]) != {"heads", "runtime_lock_sha256"}
                or not isinstance(doc["source"]["heads"], dict)
                or set(doc["source"]["heads"]) != set(BUNDLE_TREES)
                or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{40}", v) for v in doc["source"]["heads"].values())
                or not isinstance(doc["source"]["runtime_lock_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", doc["source"]["runtime_lock_sha256"])
                or doc["runtime_identity"].get("agent_head") != doc["source"]["heads"]["astrid-agent"]
                or doc["runtime_identity"].get("runtime_head") != doc["source"]["heads"]["astrid-runtime"]):
            raise SessionError("held state: malformed or incomplete")
        if "charged_jobs" in doc:
            operation = doc["operation"]
            if (type(doc["charged_jobs"]) is not int or not doc["jobs"] <= doc["charged_jobs"] < JOB_LIMIT
                    or (operation is not None and (
                        not isinstance(operation, dict)
                        or set(operation) != {"id", "label", "state", "exit_code"}
                        or not isinstance(operation["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", operation["id"])
                        or not isinstance(operation["label"], str) or not operation["label"]
                        or operation["state"] not in ("starting", "exited")
                        or (operation["state"] == "starting" and operation["exit_code"] is not None)
                        or (operation["state"] == "exited" and (type(operation["exit_code"]) is not int
                                                               or not 0 <= operation["exit_code"] <= 255))
                        or (doc["status"] == "held" and operation["state"] != "exited")))):
                raise SessionError("held state: malformed operation or charged-job count")
        return doc

    def _read_held_evidence(self, path: Path) -> bytes:
        """Read one local custody input without following links or accepting devices."""
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise SessionError("held state: evidence file protection invalid")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(fd)

    def renew_held(self) -> int:
        """Extend only a verified held admission; never run a job or release the pod."""
        lock = self.workdir / ".held-session.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            self.say("STOPPED: held session lock is unavailable")
            return 1
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.say("STOPPED: held session is in use")
                return 1
            try:
                doc = self._read_held()
                now = time.time()
                if (doc["status"] != "held" or doc["expires_at"] <= now
                        or doc["expires_at"] > now + 3600
                        or Path(self.args.token_dir).resolve() != Path(doc["token_dir"])
                        or self.args.datacenters != [DATACENTER]
                        or self.args.storage_name != STORAGE_NAME
                        or self.args.volume_gb != doc["volume_gb"]
                        or getattr(self.args, "storage_volume_id", None) != doc["volume_id"]):
                    raise SessionError("held state: stale, incomplete or incompatible")

                self.sid, self.pod_id = doc["session"], doc["pod_id"]
                self.name = f"astrid-pod-session-{self.sid}"
                token_path = Path(doc["token_dir"]) / f"pod-session-{self.sid}.token"
                from runpod_lifecycle.https import read_token_file
                # The loader enforces canonical path, ownership, mode, link count and token shape.
                # The bearer is deliberately neither logged nor retained in session state.
                token = read_token_file(token_path)
                del token

                progress_path = self.workdir / "pod-session-progress.json"
                progress_bytes = self._read_held_evidence(progress_path)
                if hashlib.sha256(progress_bytes).hexdigest() != doc["progress_sha256"]:
                    raise SessionError("held state: progress differs")
                progress = json.loads(progress_bytes)
                failed = [row for row in progress.get("steps", [])
                          if isinstance(row, dict) and row.get("result") == "failed"]
                if (progress.get("schema") != "astrid.pod-session-progress.v1"
                        or progress.get("session") != doc["session"]
                        or progress.get("pod_id") != doc["pod_id"]
                        or progress.get("jobs") != doc["jobs"]
                        or not isinstance(progress.get("steps"), list)
                        or len(failed) != 1 or failed[0].get("step") != doc["primary_step"]):
                    raise SessionError("held state: progress lineage differs")

                source = json.loads(self._read_held_evidence(self.workdir / "source-identity.json"))
                if source != doc["source"]:
                    raise SessionError("held state: source identity differs")
                runtime_lock = self._read_held_evidence(self.agent_root / "runtime.lock.json")
                if (hashlib.sha256(runtime_lock).hexdigest() != doc["source"]["runtime_lock_sha256"]
                        or json.loads(runtime_lock).get("source_commit") != doc["source"]["heads"]["astrid-runtime"]):
                    raise SessionError("held state: Runtime lock differs")

                # Keep every failed-row and custody field byte-for-byte/semantically intact;
                # only move the bounded admission expiry forward.
                old_doc = doc.copy()
                doc["expires_at"] = now + 3600
                try:
                    self._write_held(doc)
                    if self._read_held() != doc:
                        raise SessionError("held state: renewed document verification failed")
                except BaseException:
                    # If publication failed after replacement, make a best-effort durable
                    # restoration. Never delete held custody on this path.
                    try:
                        if self._read_held() != old_doc:
                            self._write_held(old_doc)
                    except Exception:
                        pass
                    raise
                self.say("HELD SESSION RENEWED; original row failure remains")
                return 0
            except BaseException as exc:
                detail = str(exc) if isinstance(exc, SessionError) else type(exc).__name__
                self.say(f"STOPPED: held session not renewed ({detail})")
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                return 1
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _verify_held_identity(self) -> None:
        code, out, _ = self.cli(["list", "--name-prefix", self.name, "--json"], self.cli_env)
        try:
            pods = json.loads(out)
        except (ValueError, TypeError):
            pods = None
        if (code or not isinstance(pods, list) or len(pods) != 1
                or not isinstance(pods[0], dict) or pods[0].get("id") != self.pod_id
                or pods[0].get("name") != self.name):
            raise SessionError("held state: pod identity is missing, conflicting or ambiguous")

    def _preserve_held_progress(self, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".held-progress-", dir=self.workdir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.workdir / "held-session-followup-progress.json")
            directory = os.open(self.workdir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _record_held_failure(self, detail: str, *, retryable: bool) -> None:
        path = self.workdir / "held-session-followup-failures.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise SessionError("held state: follow-up failure evidence protection invalid")
            operation = self._held_doc["operation"]
            event = {"session": self.sid, "pod_id": self.pod_id, "time": now(),
                     "retryable": retryable, "job_id": operation["id"] if operation else None,
                     "operation": operation, "charged_jobs": self._held_doc["charged_jobs"],
                     "error": detail[:500]}
            with os.fdopen(fd, "a", closefd=False) as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(self.workdir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _restore_held_progress(self, original_progress: bytes) -> None:
        doc = self._held_doc
        progress_path = self.workdir / "pod-session-progress.json"
        progress_path.write_bytes(original_progress)
        with progress_path.open("rb") as handle:
            os.fsync(handle.fileno())
        if _file_sha256(progress_path) != doc["progress_sha256"]:
            raise SessionError("held state: original progress could not be restored")

    def _restore_held_retry(self, original_progress: bytes) -> None:
        doc = self._held_doc
        if doc["operation"] is None or doc["operation"]["state"] != "exited":
            raise SessionError("held state: terminal proof is required for retry")
        self._restore_held_progress(original_progress)
        doc["status"] = "held"
        try:
            self._persist_held_operation()
        except BaseException:
            doc["status"] = "in_use"
            self._persist_held_operation()
            raise

    def run_held(self, command: str | None = None) -> int:
        """One serialized, bounded follow-up, or an explicit verified release."""
        lock = self.workdir / ".held-session.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            self.say("STOPPED: held session is in use")
            return 1
        self._strict_transport = True
        doc = None
        custody_verified = False
        release_attempted = False
        progress_path = self.workdir / "pod-session-progress.json"
        original_progress = None
        followup_started = False
        try:
            doc = self._read_held()
            self.sid, self.pod_id = doc["session"], doc["pod_id"]
            self.name = f"astrid-pod-session-{self.sid}"
            self.token_path = Path(doc["token_dir"]) / f"pod-session-{self.sid}.token"
            self._verify_held_identity()
            custody_verified = True
            if command is None:
                progress_path = self.workdir / "pod-session-progress.json"
                try:
                    if _file_sha256(progress_path) == doc["progress_sha256"]:
                        progress = json.loads(progress_path.read_text())
                        if (progress.get("session") == self.sid and progress.get("pod_id") == self.pod_id
                                and progress.get("jobs") == doc["jobs"] and isinstance(progress.get("steps"), list)):
                            self.records = progress["steps"]
                            self.budget.count = doc.get("charged_jobs", doc["jobs"])
                except (OSError, ValueError, TypeError):
                    pass
                doc["status"] = "in_use"
                self._write_held(doc)
                release_attempted = True
                result = self.release()
                if self.records:
                    self.record("release", "ok", result)
                    progress_path.write_bytes(self.progress_document())
                else:
                    # Preserve untrusted/invalid progress byte-for-byte.
                    release_path = self.workdir / "held-session-release.json"
                    descriptor = os.open(release_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w") as handle:
                        json.dump({"session": self.sid, "pod_id": self.pod_id, "release": result}, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                self._held_path().unlink()
                self.say(f"[release] ok {json.dumps(result)}")
                return 0
            if ((doc["status"] != "held" and doc.get("operation") is None)
                    or doc["expires_at"] <= time.time()
                    or doc["expires_at"] > time.time() + 3600
                    or Path(self.args.token_dir).resolve() != Path(doc["token_dir"])
                    or self.args.datacenters != [DATACENTER] or self.args.storage_name != STORAGE_NAME
                    or self.args.volume_gb != doc["volume_gb"]
                    or not command.strip() or not 1 <= self.args.followup_timeout <= 600):
                raise SessionError("held state: stale, incomplete or incompatible")
            if ("charged_jobs" not in doc
                    and (self.workdir / "held-session-followup-failures.jsonl").exists()):
                raise SessionError("held state: prior follow-up charges need reconciliation")
            original_progress = self._read_held_evidence(progress_path)
            if hashlib.sha256(original_progress).hexdigest() != doc["progress_sha256"]:
                if doc["status"] != "in_use":
                    raise SessionError("held state: progress differs")
                original_progress = self._read_held_evidence(self.workdir / "held-session-followup-progress.json")
                if hashlib.sha256(original_progress).hexdigest() != doc["progress_sha256"]:
                    raise SessionError("held state: preserved progress differs")
            progress = json.loads(original_progress)
            if (progress.get("schema") != "astrid.pod-session-progress.v1"
                    or progress.get("session") != self.sid or progress.get("pod_id") != self.pod_id
                    or progress.get("jobs") != doc["jobs"] or not isinstance(progress.get("steps"), list)
                    or len([r for r in progress["steps"] if r.get("result") == "failed"
                            and r.get("step") == doc["primary_step"]]) != 1
                    or json.loads((self.workdir / "source-identity.json").read_text()) != doc["source"]):
                raise SessionError("held state: progress or source identity differs")
            self.records = progress["steps"]
            self.budget.count = doc.get("charged_jobs", doc["jobs"])
            reconciling = doc["status"] == "in_use"
            if not reconciling:
                self._preserve_held_progress(original_progress)
                doc.update(charged_jobs=self.budget.count, operation=None)
            self._held_doc = doc
            # A crash after this durable transition cannot replay an uncertain operation.
            doc["status"] = "in_use"
            self._persist_held_operation()
            from runpod_lifecycle.https import read_token_file
            self.transport = self.transport_factory(self.pod_id, read_token_file(self.token_path))
            self.transport.wait_ready(timeout=600)
            if reconciling:
                if not self._reconcile_held_operation():
                    raise SessionError("held state: prior operation is not proven terminal")
                self._record_held_failure("prior operation reconciled; command was not replayed", retryable=True)
                self._restore_held_retry(original_progress)
                self.say("HELD SESSION RECONCILED; terminal proof recorded; a later resume may retry")
                return 1
            storage = self.verify_storage(recipe_footprint_gb())
            if storage["volume_id"] != doc["volume_id"]:
                raise SessionError("held state: volume identity differs")
            receipt, _ = self.runtime_readiness(doc["source"])
            if receipt.get("identity") != doc["runtime_identity"]:
                raise SessionError("held state: Runtime source identity differs")
            if json.loads(self.fetch(f"{SRC_DIR}/.pod-source.json")) != doc["source"]:
                raise SessionError("held state: transported source manifest differs")
            followup_started = True
            code, _, job = self.run_job("follow-up", command, self.args.followup_timeout)
            if code:
                raise SessionError(f"follow-up: job {job['id']} exited {code}")
            self.record("follow-up", "ok", {"job": job["id"]})
            self.write_progress(strict=True)
            if (self.workdir / "pod-session-progress.remote.json").read_bytes() != progress_path.read_bytes():
                raise SessionError("held state: remote progress differs")
            for path in (progress_path, self.workdir / "pod-session-progress.remote.json", self.workdir):
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            completed_doc = dict(doc, status="held", jobs=self.budget.count,
                                 progress_sha256=_file_sha256(progress_path))
            self._write_held(completed_doc)
            if self._read_held() != completed_doc:
                raise SessionError("held state: completed follow-up publication differs")
            self.say("POD SESSION HELD; original row failure remains")
            return 1
        except BaseException as exc:
            detail = str(exc) if isinstance(exc, SessionError) else type(exc).__name__
            self.say(f"STOPPED: held session unavailable ({detail})")
            recovered = False
            if custody_verified and followup_started and original_progress is not None and doc is not None:
                try:
                    doc["status"] = "in_use"
                    self._persist_held_operation()
                    terminal = (doc["operation"] is not None and doc["operation"]["state"] == "exited")
                    if not terminal:
                        try:
                            terminal = self._reconcile_held_operation()
                        except Exception:
                            terminal = False
                    self._record_held_failure(detail, retryable=terminal)
                    if terminal:
                        # Restore the digest-bound failure evidence, keeping every
                        # charged record separately from the original progress count.
                        self._restore_held_retry(original_progress)
                        recovered = True
                        self.say("HELD SESSION RETAINED; terminal follow-up failure is durable and may be retried")
                    else:
                        self._restore_held_progress(original_progress)
                except Exception as recovery_exc:
                    self.say(f"RECONCILIATION REQUIRED: held retry state could not be restored ({type(recovery_exc).__name__})")
            if not recovered:
                if release_attempted:
                    self.say(f"RECONCILIATION REQUIRED: explicit release outcome for pod {self.pod_id} is uncertain")
                else:
                    self.say("RECONCILIATION REQUIRED: verify the recorded held pod and custody evidence; no release was attempted")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return 1
        finally:
            self._held_doc = None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def run_steps(self):
        return [(step, getattr(self, step)) for step in STEPS[:-1]]


class JobListSession(Session):
    """Job-list policy on the existing claim, detached-job, transfer and release stack.

    Jobs share the existing bootstrapped H3 engine environment. A cmd job may invoke
    any installed command, including generate; this runner does not install a second runtime.
    """

    def __init__(self, args, **kwargs):
        self.jobs = load_jobs(Path(args.jobs))
        if getattr(args, "hold_on_row_failure", False) and len(self.jobs) != 1:
            raise SessionError("held sessions require exactly one job row")
        self._failed_rows: set[str] = set()
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
        code, stdout, handle = self.run_job(label, command, job["timeout"], want_stdout=True, strict_fetch=True)
        logs = job["packet"] / "logs"
        logs.mkdir(exist_ok=True)
        phase = "s0" if smoke else "job"
        (logs / f"{phase}.stdout").write_text(stdout, encoding="utf-8")
        (logs / f"{phase}.stderr").write_text(self.fetch(handle["dir"] + "/stderr"), encoding="utf-8")
        if code:
            raise JobRowError(f"{label}: job {handle['id']} exited {code}")
        if job["type"] == "h3":
            summary_text = self.fetch(f"{CLIP_DIR}/runs/{name}.summary.json")
            try:
                summary = parse_json_object(summary_text)
            except SessionError as exc:
                raise JobRowError(str(exc)) from exc
            if summary.get("ok") is not True or summary.get("out") != out:
                raise JobRowError(f"{label}: H3 action did not publish a successful output summary")
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
        try:
            local, digests = self.download_files(remote, job["packet"] / "outputs")
        except SessionError as exc:
            if str(exc).startswith(("download: the pod could not hash the expected outputs",
                                    "download: sha256 of the ")):
                raise JobRowError(str(exc)) from exc
            raise
        outputs = {}
        for path, local_path in local.items():
            try:
                outputs[path] = self.verify_output(local_path, specs[path])
            except (SessionError, ValueError, json.JSONDecodeError) as exc:
                raise JobRowError(str(exc) or type(exc).__name__) from exc
        if job["type"] == "h3":
            try:
                receipt = json.loads(local["receipt.json"].read_text(encoding="utf-8"))
            except (ValueError, json.JSONDecodeError) as exc:
                raise JobRowError(str(exc) or type(exc).__name__) from exc
            if not publication_verified(receipt):
                raise JobRowError("H3 downloaded receipt does not verify final composition publication")
            attested = attested_object_id(receipt)
            if attested and attested != "sha256:" + digests["final.mp4"]:
                raise JobRowError("H3 video differs from the receipt's published object id")
            try:
                check_video(outputs["final.mp4"])
            except SessionError as exc:
                raise JobRowError(str(exc) or type(exc).__name__) from exc
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

    def continue_after_step_failure(self, step, exc):
        if not isinstance(exc, Exception) or isinstance(exc, BudgetExceeded):
            return False
        for job in self.jobs:
            if (step in (f"job:{job['id']}", f"download:{job['id']}", f"h3:{job['id']}:s0")
                    and isinstance(exc, JobRowError)):
                self._failed_rows.add(job["id"])
                return True
        return False

    def should_run_step(self, step):
        for job_id in self._failed_rows:
            if step in (f"job:{job_id}", f"download:{job_id}",
                        f"packet:{job_id}", f"h3:{job_id}:s0"):
                return False
        return True

    def run_steps(self):
        # Install runs sanity after constructing the primary venv, before models.
        steps = [(step, getattr(self, step)) for step in ("claim", "bundle", "bootstrap", "install", "rerun")]
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
    p.add_argument("--storage-volume-id", help="require the uniquely observed existing network volume to have this ID")
    p.add_argument("--gpus", default=",".join(DEFAULT_GPUS), type=lambda s: [g.strip() for g in s.split(",") if g.strip()],
                   help="GPU names in order of preference (default: %(default)s)")
    p.add_argument("--sources-dir", required=True, help="directory holding runpod-python astrid-runtime runpod-lifecycle VibeComfy")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--request", help="the 9 s clip request JSON (requires --asset-map)")
    mode.add_argument("--jobs", help="version: 1 YAML job list (h3 request/assets or cmd command/input-files/expected-outputs)")
    mode.add_argument("--resume-held", metavar="COMMAND", help="run one bounded command on the recorded held pod")
    mode.add_argument("--release-held", action="store_true", help="terminate and verify the recorded held pod")
    mode.add_argument("--renew-held", action="store_true", help="extend a verified held admission without contacting the pod")
    p.add_argument("--hold-on-row-failure", action="store_true", help="hold a single-row session only after a recoverable row failure")
    p.add_argument("--followup-timeout", type=int, default=300, help="follow-up deadline in seconds (1–600)")
    p.add_argument("--asset-map", help="JSON map of request asset ids to local files")
    p.add_argument("--workdir", required=True, help="local directory for the bundle, downloads and progress")
    p.add_argument("--token-dir", default=str(Path.home() / ".astrid-pod-tokens"), help="directory for the per-claim token file")
    p.add_argument("--container-disk-gb", type=int, help="default: max(120, 1.5 x the engine install footprint)")
    p.add_argument("--volume-gb", type=int, default=10)
    p.add_argument("--image", help="default: the image in astrid/packs/runpod/environment.linux-cu130.json")
    p.add_argument("--wait-capacity", type=int, default=0,
                   help="seconds to retry known capacity failures for each GPU/datacenter placement; total time can exceed this across placements (0: immediate fallback)")
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
    if args.hold_on_row_failure and not args.jobs:
        parser.error("--hold-on-row-failure requires --jobs")
    if args.resume_held is not None and not 1 <= args.followup_timeout <= 600:
        parser.error("--followup-timeout must be between 1 and 600 seconds")
    if args.wait_capacity < 0:
        raise SessionError("--wait-capacity must be non-negative")
    source_paths = controller_source_paths(Path(args.sources_dir))
    sys.path[:0] = [str(p) for p in source_paths]
    binary = args.runpod_cli
    if binary is None:
        sibling = Path(sys.executable).parent / "runpod-lifecycle"
        binary = str(sibling) if sibling.exists() else "runpod-lifecycle"
    try:
        session = (JobListSession if args.jobs else Session)(
            args, cli=default_cli_runner(binary, source_paths), transport_factory=default_transport_factory,
            http_status=default_http_status, say=lambda message: print(message, flush=True))
    except (SessionError, ValueError) as exc:
        parser.error(str(exc))
    if args.renew_held:
        return session.renew_held()
    if args.resume_held is not None or args.release_held:
        return session.run_held(args.resume_held)
    return session.run()


if __name__ == "__main__":
    sys.exit(main())
