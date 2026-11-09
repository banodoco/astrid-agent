"""RunPod pod custody helpers shared by the machine route, plus h3's legacy pod route.

Generation on RunPod runs as a remote machine (``astrid.packs.runpod.machine``,
the ``runpod`` contribution to ``generation.machine``). That route reuses this
module's pieces on an exact existing claim: ``RemotePlacement.prepare_claimed``
(``_bootstrap``, ``_install``, ``_canonical_installed``), ``_put_checked``,
``manifest_command``/``_MANIFEST_SCRIPT`` and ``connect_claimed_for_preparation``.

``RemotePlacement.run`` is h3_av's legacy ``--placement runpod`` route (claim,
bootstrap, install, upload, detached transform, download, release); it imports
no h3 code (its caller passes verify and ffprobe) and is deleted with its last
caller in Plan 32 T6b.

Money rules, enforced here and pinned by tests:

* ``PodProvider.claim`` is called at most once per wrapper instance; nothing in
  this module retries a claim or allocates a second pod.
* Any failure after the claim cancels the running job (best effort) and releases
  the pod, unless the caller asked for ``leave_running`` and the run succeeded.
* The spend cap is checked against the claimed pod's hourly cost before any
  work is sent to it, and one wall-clock deadline covers every later step.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import shlex
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from astrid.core.contracts.errors import AstridError

from .pod_bootstrap import SCRIPT as POD_BOOTSTRAP_SCRIPT

PLACEMENT = "runpod"
ENGINE_ID = "comfyui"
DATACENTER = "EUR-NO-1"
STORAGE_NAME = "storage"
# One pod storage root on the container disk. Attempts, outputs, engines, bootstrap and runs all derive
# from it, so the task-storage free-space guard measures the device the models and outputs live on.
# The network volume (mounted at /workspace, FUSE: it ignores mkdir's mode) is pinned by the storage probe.
# The engine install is model-free: align fetches only the job manifest's missing rows.
POD_ROOT = "/root/astrid"
# Engine custody (astrid.core.generation.engine_custody) needs owner-only directories, which the
# container disk honours; this is the scripts/pod_session.py route.
ENGINE_BASE = f"{POD_ROOT}/engines"
ENGINE_ROOT = f"{ENGINE_BASE}/{ENGINE_ID}"
BOOTSTRAP_ROOT = f"{POD_ROOT}/bootstrap"
RUNS_ROOT = f"{POD_ROOT}/runs"
POD_VENV = "/opt/astrid"
INSTALL_DEADLINE_SECONDS = 14400
DEFAULT_MAX_SPEND_EUR = 10.0
DEFAULT_DEADLINE_SECONDS = 6 * 3600
POLL_SECONDS = 15.0
MANIFEST_NAME = "wrapper-manifest.json"
MANIFEST_MARK = "WRAPPER_MANIFEST_SHA256="
RESULT_MARK = "WRAPPER_RESULT_JSON="
FIXED_OUTPUTS = ("08-final-receipt.json", "operation-state.json")
SOURCE_DIRS = (
    "src/astrid-runtime",
    "src/runpod-lifecycle",
    "src/astrid-agent",
    "src/VibeComfy",
)


class RemotePlacementError(AstridError):
    """A remote placement step failed. The pod has been released unless stated."""


class PreparationDeadlineExceeded(RemotePlacementError):
    """Preparation of a claimed pod ran past its disclosed preparation bound."""


class DigestMismatch(RemotePlacementError):
    """A transferred file does not match its recorded sha256."""


# ----------------------------------------------------------------------------
# Provider boundary
# ----------------------------------------------------------------------------


class PodOps(Protocol):
    """What the wrapper may do to one claimed pod (HTTPS agent surface)."""

    def exec(self, command: str, timeout: float = 600) -> tuple[int, str, str]: ...

    def put(self, local: Path, remote: str) -> None: ...

    def get(self, remote: str, local: Path) -> None: ...

    def job_start(self, command: str) -> Mapping[str, Any]: ...

    def job_poll(self, job_id: str) -> Mapping[str, Any]: ...

    def job_cancel(self, job_id: str) -> Mapping[str, Any]: ...


@dataclass
class Claim:
    pod_id: str
    ops: PodOps
    identity: dict[str, Any]
    cost_per_hr_eur: float | None = None
    datacenter_id: str | None = None
    transport: str = "https"
    storage_name: str | None = None
    handle: Any = None  # provider-private custody (claim handle, token file)


class PodProvider(Protocol):
    def claim(self, spec: "ClaimSpec") -> Claim: ...

    def release(self, claim: Claim) -> None: ...

    def pod_exists(self, pod_id: str) -> bool:
        """GET the pod; False means the provider answered 404."""
        ...


@dataclass(frozen=True)
class ClaimSpec:
    transport: str = "https"
    storage_name: str = STORAGE_NAME
    datacenter_id: str = DATACENTER
    max_spend_eur: float = DEFAULT_MAX_SPEND_EUR
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS
    pod_id: str | None = None  # attach to a given handle instead of allocating


@dataclass(frozen=True)
class RunPodInstallation:
    """Accepted canonical installation; validation happens before renting."""

    compatibility: Mapping[str, str]
    engine: str = "comfyui"
    platform: str = "linux-x86_64-cu130"
    root: str = f"{ENGINE_BASE}/comfyui"

    def plan(self):
        from astrid.core.generation import engine_recipe, engine_runner
        from .target_selection import RunPodLeaseCompatibility

        expected = RunPodLeaseCompatibility(**dict(self.compatibility)).as_mapping()
        if (self.engine not in {"comfyui"} or self.platform != "linux-x86_64-cu130"
                or self.root != f"{ENGINE_BASE}/{self.engine}"):
            raise RemotePlacementError("unsupported retained installation route")
        recipe = engine_recipe.load_recipe(self.engine, self.platform)
        plan = engine_runner.plan_install(recipe, root=self.root, placement="local")
        vibe = next(s for s in recipe.sources if s.name == "vibecomfy")
        runtime_lock = Path(__file__).resolve().parents[3] / "runtime.lock.json"
        if (expected["recipe_digest"] != "sha256:" + plan.recipe_sha256
                or expected["model_lock_digest"] != "sha256:" + plan.models_lock_sha256
                or expected["vibecomfy_commit"] != vibe.pin.revision
                or json.loads(runtime_lock.read_bytes())["source_commit"] != expected["runtime_commit"]):
            raise RemotePlacementError("installation differs from the accepted recipe/model/source tuple")
        return plan

    def model_bytes(self, manifest_rows: Sequence[Mapping[str, Any]] = ()) -> int:
        """Bytes the volume must have free: the job manifest's rows only, never the whole model lock.

        Plan 32 T5h: an install is model-free and align fetches the manifest's missing rows.
        """
        return max(1, sum(int(row.get("size") or row.get("bytes") or 0) for row in manifest_rows))

    def validate_sources(self, source_root: Path | None) -> str:
        self.plan()
        if source_root is None:
            raise RemotePlacementError("retained preparation requires pinned bootstrap sources")
        # A commit labels the source association; the bootstrap marker below
        # binds the actual archive bytes. Do not infer either from the other.
        for directory, commit in (
            ("astrid-runtime", self.compatibility["runtime_commit"]),
            ("VibeComfy", self.compatibility["vibecomfy_commit"]),
        ):
            root = source_root / "src" / directory
            result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, check=True)
            clean = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                                   capture_output=True, text=True, check=True)
            if result.stdout.strip() != commit or clean.stdout.strip():
                raise RemotePlacementError("bootstrap sources differ from accepted clean source commits")
        return source_digest(source_root)


@dataclass
class Limits:
    max_spend_eur: float = DEFAULT_MAX_SPEND_EUR
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS
    poll_seconds: float = POLL_SECONDS
    storage_name: str = STORAGE_NAME
    datacenter_id: str = DATACENTER

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Limits":
        env = os.environ if env is None else env
        limits = cls()
        if env.get("ASTRID_RUNPOD_MAX_SPEND_EUR"):
            limits.max_spend_eur = float(env["ASTRID_RUNPOD_MAX_SPEND_EUR"])
        if env.get("ASTRID_RUNPOD_DEADLINE_SECONDS"):
            limits.deadline_seconds = int(env["ASTRID_RUNPOD_DEADLINE_SECONDS"])
        return limits


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_digest(source_root: Path) -> str:
    """Same digest the pack's bootstrap payload computes over the four source dirs."""
    lines: list[tuple[bytes, str]] = []
    for directory in SOURCE_DIRS:
        base = Path(source_root) / directory
        if base.is_symlink() or not base.is_dir():
            raise RemotePlacementError(f"bootstrap source dir is missing: {directory}")
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            if any((Path(dirpath) / name).is_symlink() for name in (*dirnames, *filenames)):
                raise RemotePlacementError("bootstrap source closure contains a symlink")
            for name in filenames:
                if name.endswith(".pyc"):
                    continue
                full = Path(dirpath) / name
                rel = full.relative_to(source_root).as_posix()
                lines.append((rel.encode(), sha256_file(full)))
    lines.sort(key=lambda item: item[0])
    listing = "".join(f"{digest}  {rel.decode()}\n" for rel, digest in lines)
    return hashlib.sha256(listing.encode()).hexdigest()[:16]


def pod_source_manifest(source_root: Path, pins: Mapping[str, tuple[str, str]] | None = None) -> bytes:
    """The one transported identity record, in exactly the shape pod_source_identity checks.

    Heads come from the pinned (repository, sha) of each source dir; the lock digest is of the
    astrid-agent runtime.lock.json that is shipped in the same archive.
    """
    if pins is None:
        from .managed_sources import _pins
        pins = _pins()
    heads = {Path(directory).name: pins[directory][1] for directory in SOURCE_DIRS}
    lock = Path(source_root) / "src/astrid-agent" / "runtime.lock.json"
    manifest = {"heads": heads, "runtime_lock_sha256": sha256_file(lock)}
    return (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()


def _root_owned(info: tarfile.TarInfo) -> tarfile.TarInfo:
    # The pod extracts as root with plain tar. Local uid/gid (macOS or non-root Linux users)
    # would leave .git owned by a foreign uid and fail git's safe.directory check on the pod.
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def _build_source_archive(source_root: Path, destination: Path,
                          pins: Mapping[str, tuple[str, str]] | None = None) -> Path:
    # Each source dir ships with its own .git (depth-1 clean detached managed clone): the pod
    # verifier reads git state. Only bytecode is dropped. Every member is root-owned so the
    # archive is deterministic and extracts safely as root regardless of the local owner.
    manifest = pod_source_manifest(source_root, pins)
    with tarfile.open(destination, "w:gz") as archive:
        for directory in SOURCE_DIRS:
            archive.add(
                Path(source_root) / directory,
                arcname=directory,
                filter=lambda info: None
                if info.name.endswith(".pyc") or "__pycache__" in info.name
                else _root_owned(info),
            )
        info = _root_owned(tarfile.TarInfo("src/.pod-source.json"))
        info.size, info.mode, info.mtime = len(manifest), 0o644, 0
        archive.addfile(info, io.BytesIO(manifest))
    return destination


# The ONE place the pod job's command is defined. Swap in the CLIP-COMMAND
# (task 93) argv by changing this function, or pass ``command_argv=`` to
# ``RemotePlacement``. The module CLI (python -m astrid.packs.h3_av...) refuses
# direct invocation and must not be used.
def clip_command_argv(
    remote_args: Mapping[str, str],
    remote_out: str,
    *,
    project: str | None = None,
    editorial_approved: bool = False,
) -> list[str]:
    argv = [
        f"{POD_VENV}/bin/astrid", "action", "run", "h3_av.transform",
        "--request", remote_args["request"],
        "--asset-map", remote_args["asset_map"],
        "--placement", "local",
        "--out", remote_out,
    ]
    if "execution_request" in remote_args:
        argv += ["--execution-request", remote_args["execution_request"]]
    if project:
        argv += ["--project", project]
    if editorial_approved:
        argv.append("--editorial-approved")
    return argv


# Runs on the pod after the transform: digests every file under the out dir.
_MANIFEST_SCRIPT = r'''
import hashlib, json, os, sys
root = sys.argv[1]
files = {}
for dirpath, _dirs, names in os.walk(root):
    for name in names:
        path = os.path.join(dirpath, name)
        rel = os.path.relpath(path, root)
        if rel == "%(manifest)s" or os.path.islink(path):
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        files[rel] = {"sha256": digest.hexdigest(), "size": os.path.getsize(path)}
body = json.dumps({"schema_version": 1, "files": files}, sort_keys=True, indent=2) + "\n"
with open(os.path.join(root, "%(manifest)s"), "w") as stream:
    stream.write(body)
print("%(mark)s" + hashlib.sha256(body.encode()).hexdigest())
''' % {"manifest": MANIFEST_NAME, "mark": MANIFEST_MARK}


def manifest_command(out_dir: str, python: str = f"{POD_VENV}/bin/python") -> str:
    encoded = base64.b64encode(_MANIFEST_SCRIPT.encode()).decode()
    return f"echo {encoded} | base64 -d | {python} - {shlex.quote(out_dir)}"


@dataclass
class RemoteRun:
    """Mutable bookkeeping for one wrapper run."""

    run_id: str
    remote_root: str
    local_out: Path
    claim: Claim | None = None
    job_id: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    released: bool = False
    installation: RunPodInstallation | None = None
    checkpoint: Callable[[Mapping[str, Any]], None] | None = None
    installed: Mapping[str, Any] | None = None

    @property
    def remote_in(self) -> str:
        return f"{self.remote_root}/in"

    @property
    def remote_out(self) -> str:
        return f"{self.remote_root}/out"

    def note(self, step: str, status: str, **detail: Any) -> None:
        self.steps.append({"step": step, "status": status, **detail})


# ----------------------------------------------------------------------------
# The wrapper
# ----------------------------------------------------------------------------


class RemotePlacement:
    def __init__(
        self,
        provider: PodProvider,
        *,
        limits: Limits | None = None,
        source_root: Path | None = None,
        leave_running: bool = False,
        pod_id: str | None = None,
        verify_fn: Callable[[Path, Mapping[str, Any]], None] | None = None,
        ffprobe_fn: Callable[[Path], None] | None = None,
        command_argv: Callable[..., Sequence[str]] = clip_command_argv,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.limits = limits or Limits()
        self.source_root = source_root
        self.leave_running = leave_running
        self.pod_id = pod_id
        # The h3 caller passes its own verify/ffprobe; this module imports no h3 code.
        self._verify_fn = verify_fn
        self._ffprobe_fn = ffprobe_fn
        self._command_argv = command_argv
        self._clock = clock
        self._sleep = sleep
        self._claims_made = 0
        self._deadline_at: float | None = None
        self._preparation_seconds: float | None = None

    # -- public ------------------------------------------------------------

    def begin_claimed(self, claim: Claim, *, out: Path, deadline_seconds: float) -> RemoteRun:
        """Bookkeeping for commands on an exact existing claim, under one preparation bound."""
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise RemotePlacementError("prepared claim has no remaining paid lifetime")
        self._deadline_at = self._clock() + deadline_seconds
        self._preparation_seconds = deadline_seconds
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        return RemoteRun(run_id, f"{RUNS_ROOT}/{run_id}", out, claim=claim)

    def prepare_claimed(self, claim: Claim, *, out: Path, deadline_seconds: float,
                        installation: RunPodInstallation | None = None,
                        checkpoint: Callable[[Mapping[str, Any]], None] | None = None,
                        reuse_snapshot: Mapping[str, Any] | None = None) -> RemoteRun:
        """Bootstrap/install an exact existing claim for ordinary prepared work.

        Claim/admission/release stay with their existing journal and Runtime
        owners. This method never claims a pod or invokes the H3 transform.
        The install is model-free; models arrive through align on the pod, row by row.
        """
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise RemotePlacementError("prepared claim has no remaining paid lifetime")
        self._deadline_at = self._clock() + deadline_seconds
        self._preparation_seconds = deadline_seconds
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        if (installation is None) != (checkpoint is None):
            raise RemotePlacementError("retained installation requires its lease checkpoint")
        state = RemoteRun(run_id, f"{RUNS_ROOT}/{run_id}", out, claim=claim,
                          installation=installation, checkpoint=checkpoint)
        try:
            if reuse_snapshot is not None:
                marker = f"{BOOTSTRAP_ROOT}/{source_digest(self.source_root)}/ok"
                code, _, _ = self._exec(state, f"test -f {shlex.quote(marker)}", timeout=60)
                if code != 0 or not self._canonical_installed(state) or state.installed != reuse_snapshot:
                    raise RemotePlacementError("retained installed snapshot changed; exact lease requires cleanup")
                state.note("bootstrap", "skipped")
                state.note("install", "skipped")
            else:
                self._bootstrap(state)
                self._install(state)
            self._check_deadline("prepared install")
        except BaseException:
            if state.job_id is not None:
                try:
                    stopped = claim.ops.job_cancel(state.job_id)
                    if checkpoint is not None and stopped.get("state") in {"exited", "cancelled"}:
                        checkpoint({"id": state.job_id, "state": stopped["state"]})
                except Exception:
                    pass
            raise
        return state


    # -- preflight ---------------------------------------------------------


    # -- deadline ----------------------------------------------------------

    def _check_deadline(self, step: str) -> None:
        if self._deadline_at is None:
            return
        now = self._clock()
        if self._preparation_seconds is not None:
            # The pod-side install derives its --deadline from this one bound,
            # so reaching it is the preparation deadline, not a job failure.
            if now >= self._deadline_at:
                raise PreparationDeadlineExceeded(
                    f"preparation deadline of {self._preparation_seconds:g}s exceeded during {step}"
                )
        elif now > self._deadline_at:
            raise RemotePlacementError(
                f"deadline of {self.limits.deadline_seconds}s exceeded during {step}"
            )

    def _remaining(self) -> float:
        assert self._deadline_at is not None
        return max(1.0, self._deadline_at - self._clock())

    # -- step 1 ------------------------------------------------------------


    # -- remote command helper ----------------------------------------------

    def _exec(self, state: RemoteRun, command: str, *, timeout: float = 600) -> tuple[int, str, str]:
        self._check_deadline("exec")
        assert state.claim is not None
        return state.claim.ops.exec(command, min(timeout, self._remaining()))

    def _exec_ok(self, state: RemoteRun, command: str, what: str, *, timeout: float = 600) -> str:
        code, stdout, stderr = self._exec(state, command, timeout=timeout)
        if code != 0:
            raise RemotePlacementError(f"{what} failed (exit {code}): {(stderr or stdout)[-800:]}")
        return stdout

    def _run_job(self, state: RemoteRun, command: str, what: str) -> str:
        """Step 5 mechanism: detached job, then poll the durable status."""
        assert state.claim is not None
        ops = state.claim.ops
        self._check_deadline(what)
        if state.checkpoint is not None:
            # Lifecycle accepts the caller's exact detached identity. Flush it
            # before launch; a lost ACK must never invent or retry another job.
            state.job_id = uuid.uuid4().hex
            state.checkpoint({"id": state.job_id, "state": "starting"})
            started = ops.job_start(command, job_id=state.job_id)
            if started.get("id") != state.job_id:
                raise RemotePlacementError("detached job differs from persisted preparation custody")
            state.checkpoint({"id": state.job_id, "state": "running"})
        else:
            started = ops.job_start(command)
        job_id = str(started["id"])
        state.job_id = job_id
        while True:
            self._check_deadline(what)
            record = ops.job_poll(job_id)
            if record.get("state") == "exited":
                break
            if record.get("state") in ("missing", "cancelled"):
                raise RemotePlacementError(f"{what} job {job_id} is {record.get('state')}")
            self._sleep(self.limits.poll_seconds)
        state.job_id = None
        if state.checkpoint is not None:
            state.checkpoint({"id": job_id, "state": "exited"})
        directory = str(started.get("dir") or "")
        stdout = stderr = ""
        if directory:
            _c, stdout, _e = ops.exec(f"cat {shlex.quote(directory)}/stdout", 120)
            _c, stderr, _e = ops.exec(f"tail -c 4000 {shlex.quote(directory)}/stderr", 120)
        if record.get("exit_code") != 0:
            self._check_deadline(what)
            raise RemotePlacementError(
                f"{what} job {job_id} exited {record.get('exit_code')}: {stderr[-800:]}"
            )
        return stdout

    # -- step 2 ------------------------------------------------------------

    def _bootstrap(self, state: RemoteRun) -> None:
        if self.source_root is None:
            # No archives to ship: the pod must already be bootstrapped.
            code, out, _ = self._exec(
                state, f"ls {BOOTSTRAP_ROOT}/*/ok 2>/dev/null | head -n 1", timeout=60
            )
            if not out.strip():
                raise RemotePlacementError(
                    "pod is not bootstrapped and no source archives were provided"
                )
            state.note("bootstrap", "skipped", reason="marker present")
            return
        digest = source_digest(self.source_root)
        marker = f"{BOOTSTRAP_ROOT}/{digest}/ok"
        code, _o, _e = self._exec(state, f"test -f {shlex.quote(marker)}", timeout=60)
        if code == 0:
            state.note("bootstrap", "skipped", digest=digest)
            return
        assert state.claim is not None
        work = f"{BOOTSTRAP_ROOT}/upload-{digest}"
        archive = state.local_out / "bootstrap-sources.tar.gz"
        _build_source_archive(self.source_root, archive)
        script = state.local_out / "pod_bootstrap.sh"
        script.write_text(POD_BOOTSTRAP_SCRIPT, encoding="utf-8")
        try:
            self._exec_ok(state, f"mkdir -p {shlex.quote(work)}", "bootstrap directory")
            state.claim.ops.put(archive, f"{work}/sources.tar.gz")
            state.claim.ops.put(script, f"{work}/pod_bootstrap.sh")
        finally:
            archive.unlink(missing_ok=True)
            script.unlink(missing_ok=True)
        command = (
            f"set -eu; cd {shlex.quote(work)}; tar -xzf sources.tar.gz -C . ; "
            "bash pod_bootstrap.sh"
        )
        self._run_job(state, command, "bootstrap")
        code, _o, _e = self._exec(state, f"test -f {shlex.quote(marker)}", timeout=60)
        if code != 0:
            raise RemotePlacementError(f"bootstrap finished without marker {marker}")
        state.note("bootstrap", "done", digest=digest)

    # -- step 3 ------------------------------------------------------------

    def _engine_installed_healthy(self, state: RemoteRun) -> bool:
        if state.installation is not None:
            return self._canonical_installed(state)
        command = (
            f"{POD_VENV}/bin/astrid engines status {ENGINE_ID} --placement local "
            f"--root {shlex.quote(ENGINE_ROOT)}"
        )
        code, stdout, _err = self._exec(state, command, timeout=120)
        if code != 0:
            return False
        try:
            receipt = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
        except json.JSONDecodeError:
            return False
        if not isinstance(receipt, Mapping) or receipt.get("status") != "installed":
            return False
        health = receipt.get("health")
        return isinstance(health, Mapping) and health.get("status") == "healthy"

    def _install(self, state: RemoteRun) -> None:
        if self._engine_installed_healthy(state):
            state.note("install", "skipped")
            return
        installation = state.installation
        command = (
            f"{POD_VENV}/bin/astrid engines install {installation.engine} "
            f"--platform {installation.platform} --placement local --root {shlex.quote(installation.root)} "
            f"--deadline {min(INSTALL_DEADLINE_SECONDS, self._remaining())}"
        ) if installation is not None else (
            f"{POD_VENV}/bin/astrid engines install {ENGINE_ID} --placement local "
            f"--root {shlex.quote(ENGINE_ROOT)} --deadline {INSTALL_DEADLINE_SECONDS}"
        )
        self._run_job(state, command, "engine install")
        if not self._engine_installed_healthy(state):
            raise RemotePlacementError("engine install finished but status is not installed and healthy")
        state.note("install", "done")

    def _canonical_installed(self, state: RemoteRun) -> bool:
        from astrid.core.generation import engine_runner

        installation = state.installation
        plan = installation.plan()
        command = (f"{POD_VENV}/bin/astrid engines status {installation.engine} "
                   f"--platform {installation.platform} --placement local "
                   f"--root {shlex.quote(installation.root)} --deadline {min(120, self._remaining())}")
        code, stdout, _ = self._exec(state, command, timeout=120)
        if code != 0 or stdout.strip() in {"", "null"}:
            return False
        raw = json.loads(stdout)
        if raw.get("status") != "installed":
            return False
        # Status re-verifies the actual source/model/environment/start/health
        # through the canonical runner. Parse with that same receipt boundary.
        live = engine_runner._parse_receipt(raw, plan)
        path = state.local_out / "installed.json"
        try:
            state.claim.ops.get(installation.root + "/.astrid-engine/install.json", path)
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise RemotePlacementError("invalid canonical installation receipt file")
            data = path.read_bytes()
            saved = engine_runner._parse_receipt(json.loads(data), plan)
        finally:
            path.unlink(missing_ok=True)
        if (saved.status != "installed" or live.receipt_id != saved.receipt_id
                or live.source_commits != saved.source_commits
                or live.requirements_locks != saved.requirements_locks
                or live.health.object_info_sha256 != saved.health.object_info_sha256):
            raise RemotePlacementError("live installation differs from its canonical receipt")
        state.installed = {
            "schema": "astrid.runpod.installed-snapshot.v1", "engine": installation.engine,
            "platform": installation.platform, "root": installation.root,
            "compatibility": dict(installation.compatibility), "receipt_id": saved.receipt_id,
            "receipt_sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        }
        return True

    # -- step 4 ------------------------------------------------------------

    def _put_checked(self, state: RemoteRun, local: Path, remote: str) -> None:
        assert state.claim is not None
        expected = sha256_file(local)
        state.claim.ops.put(local, remote)
        code, stdout, _e = self._exec(state, f"sha256sum {shlex.quote(remote)}", timeout=300)
        actual = stdout.split()[0] if code == 0 and stdout.split() else ""
        if actual != expected:
            raise DigestMismatch(f"upload of {local.name} did not verify (expected {expected}, got {actual or 'none'})")


    # -- step 5 ------------------------------------------------------------



    # -- step 6 ------------------------------------------------------------





    # -- step 7 ------------------------------------------------------------




# ----------------------------------------------------------------------------
# Default provider: the existing claim script, release helper and HTTPS transport
# ----------------------------------------------------------------------------


class _HttpsOps:
    def __init__(self, pod_id: str, token: str) -> None:
        self.pod_id, self.token = pod_id, token

    def _transport(self):
        from runpod_lifecycle.https import HttpsPodTransport

        return HttpsPodTransport(self.pod_id, self.token)

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        transport = self._transport()
        try:
            return getattr(transport, name)(*args, **kwargs)
        finally:
            transport.disconnect()

    def exec(self, command: str, timeout: float = 600) -> tuple[int, str, str]:
        return self._call("execute_command", command, timeout)

    def put(self, local: Path, remote: str) -> None:
        self._call("put", local, remote)

    def get(self, remote: str, local: Path) -> None:
        self._call("get", remote, local)

    def job_start(self, command: str, *, job_id: str | None = None) -> Mapping[str, Any]:
        return self._call("detached_start", command, **({"job_id": job_id} if job_id is not None else {}))

    def job_poll(self, job_id: str) -> Mapping[str, Any]:
        return self._call("detached_poll", job_id)

    def job_cancel(self, job_id: str) -> Mapping[str, Any]:
        return self._call("detached_cancel", job_id)


def connect_claimed_for_preparation(record: Mapping[str, Any]) -> Claim:
    """Connect existing R02 custody without a provider lookup or another claim."""
    from .prepared_task import _claim_target

    if record.get("storage_mode") == "ephemeral":
        from .worker_preparation import _release_contract_from_record, validate_lease_preparation
        if (not isinstance(record.get("lease"), Mapping)
                or record["lease"].get("schema") != "astrid.runpod.lease.v1"
                or not isinstance(record.get("provider_account_ref"), str)
                or not record["provider_account_ref"]):
            raise RemotePlacementError("ephemeral preparation requires exact A3 lease custody")
        _release_contract_from_record(record)
        validate_lease_preparation(record)
    else:
        _claim_target(record)
    operation = record.get("operation_id")
    if (record.get("state") != "claimed" or record.get("transport") != "https"
            or record.get("acquisition_mode") != "allocate"
            or record.get("release_authorized") is not True
            or record.get("target_identity_verified") is not True
            or not isinstance(operation, str)
            or record.get("request_name") != "astrid-product-" + operation
            or record.get("name") != record["request_name"]):
        raise RemotePlacementError("ordinary preparation requires verified journal-created HTTPS custody")
    return Claim(
        pod_id=record["pod_id"], ops=_HttpsOps(record["pod_id"], _read_token(record)),
        identity={key: record.get(key) for key in (
            "operation_id", "pod_id", "provider_account_ref", "worker_image",
            "network_volume_id", "volume_mount_path",
        )},
        transport="https", storage_name=record.get("network_volume_name"),
        datacenter_id=record.get("network_volume_datacenter_id"), handle=dict(record),
    )


class LifecyclePodProvider:
    """Claims through ``scripts/claim_runpod_5090_backup.py`` and releases the exact pod."""

    def __init__(self, handle_dir: Path, *, provider_account_ref: str | None = None) -> None:
        self.handle_dir = Path(handle_dir)
        self.provider_account_ref = provider_account_ref or os.environ.get("ASTRID_RUNPOD_ACCOUNT_REF")

    @staticmethod
    def _claim_module():
        import importlib.util

        path = Path(__file__).resolve().parents[3] / "scripts" / "claim_runpod_5090_backup.py"
        spec = importlib.util.spec_from_file_location("astrid_claim_runpod_5090_backup", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def claim(self, spec: ClaimSpec) -> Claim:
        import asyncio

        module = self._claim_module()
        self.handle_dir.mkdir(parents=True, exist_ok=True)
        handle_path = self.handle_dir / f"claim-{uuid.uuid4().hex}.json"
        argv = [
            "--transport", "https",
            "--storage-name", spec.storage_name,
            "--handle-path", str(handle_path),
            "--release-root", module.CANONICAL_RELEASE_ROOT,
            "--max-wait-seconds", str(min(spec.deadline_seconds, module.DEFAULT_MAX_WAIT_SECONDS)),
            "--on-failure", "release",
        ]
        if self.provider_account_ref:
            argv += ["--provider-account-ref", self.provider_account_ref]
        if spec.pod_id:
            argv += ["--pod-id", spec.pod_id, "--allow-pod-release"]
        args = module._parser().parse_args(argv)
        handle = asyncio.run(module._claim(args))
        token = _read_token(handle)
        cost = _lookup_cost(handle)
        return Claim(
            pod_id=str(handle["pod_id"]),
            ops=_HttpsOps(str(handle["pod_id"]), token),
            identity={
                key: handle.get(key)
                for key in ("operation_id", "request_name", "handle_path", "acquisition_mode",
                            "network_volume_id", "claimed_at")
            },
            cost_per_hr_eur=cost,
            datacenter_id=handle.get("network_volume_datacenter_id"),
            transport=str(handle.get("transport", "https")),
            storage_name=handle.get("network_volume_name") or handle.get("storage_name"),
            handle=handle,
        )

    def release(self, claim: Claim) -> None:
        import asyncio

        from runpod_lifecycle import RunPodConfig

        from astrid.packs.runpod.worker_preparation import (
            _release_exact_target_after_quiescence,
            unactivated_target_quiescence,
        )

        handle = dict(claim.handle)
        config = RunPodConfig.from_env(
            transport="https", https_token=_read_token(handle),
            storage_name=handle.get("storage_name"), attach_only=True,
        )

        async def quiesce(record):
            return unactivated_target_quiescence(record)

        asyncio.run(_release_exact_target_after_quiescence(
            config, handle_path=Path(handle["handle_path"]), attempt=handle,
            quiesce_worker=quiesce,
        ))

    def pod_exists(self, pod_id: str) -> bool:
        from runpod_lifecycle import RunPodConfig
        from .worker_preparation import read_exact_pod

        config = RunPodConfig.from_env(transport="https")
        status, record = read_exact_pod(config.api_key, pod_id)
        if status == 404 and record is None:
            return False
        if status == 200 and isinstance(record, dict) and record.get("id") == pod_id:
            return True
        raise RemotePlacementError("exact pod GET did not establish presence or 404")


def _read_token(handle: Mapping[str, Any]) -> str:
    from runpod_lifecycle.https import read_token_file

    return read_token_file(handle["https_token_file"])


def _lookup_cost(handle: Mapping[str, Any]) -> float | None:
    from runpod_lifecycle import RunPodConfig, api

    status = api.get_pod_status(str(handle["pod_id"]), RunPodConfig.from_env(transport="https").api_key)
    value = (status or {}).get("cost_per_hr")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# Entry used by h3_av.transform
# ----------------------------------------------------------------------------


