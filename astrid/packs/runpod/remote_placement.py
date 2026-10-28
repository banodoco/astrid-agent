"""Remote (RunPod) placement wrapper for ``h3_av.transform --placement runpod``.

One module, seven steps (ROUTE-DECISION section 4):

1. claim the pod         ``RemotePlacement._claim`` -> ``PodProvider.claim``
2. bootstrap if missing  ``RemotePlacement._bootstrap``
3. install if missing    ``RemotePlacement._install``
4. upload inputs         ``RemotePlacement._upload_inputs``
5. detached job + poll   ``RemotePlacement._run_job`` / ``_start_transform``
6. download and verify   ``RemotePlacement._download`` / ``_verify``
7. release, check 404    ``RemotePlacement._release``

Money rules, enforced here and pinned by tests:

* ``PodProvider.claim`` is called at most once per wrapper instance; nothing in
  this module retries a claim or allocates a second pod.
* Any failure after the claim cancels the running job (best effort) and releases
  the pod, unless the caller asked for ``leave_running`` and the run succeeded.
* The spend cap is checked against the claimed pod's hourly cost before any
  work is sent to it, and one wall-clock deadline covers every later step.

The provider boundary is the ``PodProvider``/``PodOps`` protocols. The default
``LifecyclePodProvider`` reuses the existing claim script (``claim_runpod_5090_backup``),
the exact-pod release helper in ``worker_preparation`` and the ``runpod-lifecycle``
HTTPS transport (``execute_command``, ``put``, ``get``, ``detached_*``); it adds no
provider path of its own. Tests stub the whole boundary.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shlex
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from astrid.core.contracts.errors import AstridError

PLACEMENT = "runpod"
ENGINE_ID = "vibecomfy-h3"
DATACENTER = "EUR-NO-1"
STORAGE_NAME = "storage"
POD_ROOT = "/workspace/astrid"
ENGINE_ROOT = f"{POD_ROOT}/engines/{ENGINE_ID}"
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
    "src/runpod-python",
    "src/astrid-runtime",
    "src/runpod-lifecycle",
    "src/astrid-agent",
    "src/VibeComfy",
)


class RemotePlacementError(AstridError):
    """A remote placement step failed. The pod has been released unless stated."""


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
    """Same digest ``scripts/pod_bootstrap.sh`` computes over the five source dirs."""
    lines: list[tuple[bytes, str]] = []
    for directory in SOURCE_DIRS:
        base = Path(source_root) / directory
        if not base.is_dir():
            raise RemotePlacementError(f"bootstrap source dir is missing: {directory}")
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for name in filenames:
                if name.endswith(".pyc"):
                    continue
                full = Path(dirpath) / name
                rel = full.relative_to(source_root).as_posix()
                lines.append((rel.encode(), sha256_file(full)))
    lines.sort(key=lambda item: item[0])
    listing = "".join(f"{digest}  {rel.decode()}\n" for rel, digest in lines)
    return hashlib.sha256(listing.encode()).hexdigest()[:16]


def _build_source_archive(source_root: Path, destination: Path) -> Path:
    with tarfile.open(destination, "w:gz") as archive:
        for directory in SOURCE_DIRS:
            archive.add(
                Path(source_root) / directory,
                arcname=directory,
                filter=lambda info: None
                if info.name.endswith(".pyc") or "/.git" in info.name or "__pycache__" in info.name
                else info,
            )
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
        self._verify_fn = verify_fn or _default_verify
        self._ffprobe_fn = ffprobe_fn or _default_ffprobe
        self._command_argv = command_argv
        self._clock = clock
        self._sleep = sleep
        self._claims_made = 0
        self._deadline_at: float | None = None

    # -- public ------------------------------------------------------------

    def run(
        self,
        *,
        request: Path,
        asset_map: Path,
        out: Path,
        project: str | None = None,
        editorial_approved: bool = False,
        execution_request: Path | None = None,
        cleanup_receipt: Path | None = None,
    ) -> dict[str, Any]:
        self._preflight(request, asset_map, execution_request, cleanup_receipt)
        out = Path(out).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        state = RemoteRun(run_id=run_id, remote_root=f"{RUNS_ROOT}/{run_id}", local_out=out)
        failure: BaseException | None = None
        result: dict[str, Any] | None = None
        try:
            self._claim(state)
            self._bootstrap(state)
            self._install(state)
            remote_args = self._upload_inputs(
                state, request, asset_map, execution_request
            )
            stdout = self._start_transform(
                state, remote_args, project=project, editorial_approved=editorial_approved
            )
            result = self._download(state, stdout)
            self._verify(state, result)
        except BaseException as exc:  # release on ANY error, then re-raise
            failure = exc
        release_error = self._release(state, failed=failure is not None)
        if failure is not None:
            if release_error is not None:
                failure.add_note(f"pod release also failed: {release_error}")
            raise failure
        if release_error is not None:
            raise release_error
        assert result is not None
        return self._finish(state, result)

    # -- preflight ---------------------------------------------------------

    def _preflight(self, request, asset_map, execution_request, cleanup_receipt) -> None:
        """Refuse before any claim: nothing here costs money."""
        for label, path in (("request", request), ("asset map", asset_map)):
            if not Path(path).is_file():
                raise RemotePlacementError(f"{label} is not a file: {path}")
        if cleanup_receipt is not None:
            raise RemotePlacementError(
                "--cleanup-receipt is not supported with --placement runpod; "
                "the wrapper records pod release in the result"
            )
        if execution_request is not None:
            document = json.loads(Path(execution_request).read_text(encoding="utf-8"))
            target = document.get("target") if isinstance(document, dict) else None
            if isinstance(target, Mapping) and target.get("kind") not in (None, "default"):
                raise RemotePlacementError(
                    "the remote H3 run takes a default execution-request target"
                )
        if not self.limits.max_spend_eur > 0 or not self.limits.deadline_seconds > 0:
            raise RemotePlacementError("spend and deadline caps must be positive")
        if self.source_root is not None:
            source_digest(self.source_root)  # fail early if a source dir is missing

    # -- deadline ----------------------------------------------------------

    def _check_deadline(self, step: str) -> None:
        if self._deadline_at is not None and self._clock() > self._deadline_at:
            raise RemotePlacementError(
                f"deadline of {self.limits.deadline_seconds}s exceeded during {step}"
            )

    def _remaining(self) -> float:
        assert self._deadline_at is not None
        return max(1.0, self._deadline_at - self._clock())

    # -- step 1 ------------------------------------------------------------

    def _claim(self, state: RemoteRun) -> None:
        if self._claims_made:
            raise RemotePlacementError("a second pod claim is not allowed")
        self._claims_made += 1
        spec = ClaimSpec(
            transport="https",
            storage_name=self.limits.storage_name,
            datacenter_id=self.limits.datacenter_id,
            max_spend_eur=self.limits.max_spend_eur,
            deadline_seconds=self.limits.deadline_seconds,
            pod_id=self.pod_id,
        )
        try:
            claim = self.provider.claim(spec)
        except BaseException as exc:
            partial = getattr(exc, "claim", None)
            if isinstance(partial, Claim):
                state.claim = partial  # custody exists: release it
            raise
        state.claim = claim
        self._deadline_at = self._clock() + self.limits.deadline_seconds
        if claim.transport != "https":
            raise RemotePlacementError(f"claim used transport {claim.transport!r}, expected https")
        if claim.datacenter_id != self.limits.datacenter_id:
            raise RemotePlacementError(
                f"claimed pod is in {claim.datacenter_id!r}, expected {self.limits.datacenter_id}"
            )
        if claim.storage_name != self.limits.storage_name:
            raise RemotePlacementError(
                f"claimed pod has storage {claim.storage_name!r}, expected {self.limits.storage_name}"
            )
        if claim.cost_per_hr_eur is None:
            raise RemotePlacementError("claimed pod has no hourly cost; refusing without a spend bound")
        worst_case = claim.cost_per_hr_eur * self.limits.deadline_seconds / 3600.0
        if worst_case > self.limits.max_spend_eur:
            raise RemotePlacementError(
                f"worst-case spend {worst_case:.2f} EUR exceeds the cap of "
                f"{self.limits.max_spend_eur:.2f} EUR"
            )
        state.note("claim", "done", pod_id=claim.pod_id)

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
        directory = str(started.get("dir") or "")
        stdout = stderr = ""
        if directory:
            _c, stdout, _e = ops.exec(f"cat {shlex.quote(directory)}/stdout", 120)
            _c, stderr, _e = ops.exec(f"tail -c 4000 {shlex.quote(directory)}/stderr", 120)
        if record.get("exit_code") != 0:
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
        script = Path(__file__).resolve().parents[3] / "scripts" / "pod_bootstrap.sh"
        self._exec_ok(state, f"mkdir -p {shlex.quote(work)}", "bootstrap directory")
        state.claim.ops.put(archive, f"{work}/sources.tar.gz")
        state.claim.ops.put(script, f"{work}/pod_bootstrap.sh")
        archive.unlink(missing_ok=True)
        command = (
            f"set -eu; cd {shlex.quote(work)}; tar -xzf sources.tar.gz -C . ; "
            f"mkdir -p src; for d in runpod-python astrid-runtime runpod-lifecycle astrid-agent VibeComfy; do "
            f'rm -rf "src/$d"; mv "$d" "src/$d"; done; bash pod_bootstrap.sh'
        )
        self._run_job(state, command, "bootstrap")
        code, _o, _e = self._exec(state, f"test -f {shlex.quote(marker)}", timeout=60)
        if code != 0:
            raise RemotePlacementError(f"bootstrap finished without marker {marker}")
        state.note("bootstrap", "done", digest=digest)

    # -- step 3 ------------------------------------------------------------

    def _engine_installed_healthy(self, state: RemoteRun) -> bool:
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
        command = (
            f"{POD_VENV}/bin/astrid engines install {ENGINE_ID} --placement local "
            f"--root {shlex.quote(ENGINE_ROOT)} --deadline {INSTALL_DEADLINE_SECONDS}"
        )
        self._run_job(state, command, "engine install")
        if not self._engine_installed_healthy(state):
            raise RemotePlacementError("engine install finished but status is not installed and healthy")
        state.note("install", "done")

    # -- step 4 ------------------------------------------------------------

    def _put_checked(self, state: RemoteRun, local: Path, remote: str) -> None:
        assert state.claim is not None
        expected = sha256_file(local)
        state.claim.ops.put(local, remote)
        code, stdout, _e = self._exec(state, f"sha256sum {shlex.quote(remote)}", timeout=300)
        actual = stdout.split()[0] if code == 0 and stdout.split() else ""
        if actual != expected:
            raise DigestMismatch(f"upload of {local.name} did not verify (expected {expected}, got {actual or 'none'})")

    def _upload_inputs(
        self, state: RemoteRun, request: Path, asset_map: Path, execution_request: Path | None
    ) -> dict[str, str]:
        self._exec_ok(state, f"mkdir -p {shlex.quote(state.remote_in)} {shlex.quote(state.remote_out)}", "run directory")
        remote_request = f"{state.remote_in}/request.json"
        self._put_checked(state, Path(request), remote_request)
        mapping = json.loads(Path(asset_map).read_text(encoding="utf-8"))
        if not isinstance(mapping, dict):
            raise RemotePlacementError("asset map must be a JSON object")
        remote_mapping: dict[str, Any] = {}
        for index, (asset_id, value) in enumerate(sorted(mapping.items())):
            local = Path(str(value)).expanduser()
            if not local.is_file():
                raise RemotePlacementError(f"declared input {asset_id!r} is not a file: {local}")
            remote = f"{state.remote_in}/assets/{index:03d}-{local.name}"
            self._exec_ok(state, f"mkdir -p {shlex.quote(state.remote_in + '/assets')}", "asset directory")
            self._put_checked(state, local, remote)
            remote_mapping[asset_id] = remote
        local_map = state.local_out / "remote-asset-map.json"
        local_map.write_text(json.dumps(remote_mapping, sort_keys=True), encoding="utf-8")
        remote_map = f"{state.remote_in}/asset-map.json"
        self._put_checked(state, local_map, remote_map)
        remote_args = {"request": remote_request, "asset_map": remote_map}
        if execution_request is not None:
            remote_exec = f"{state.remote_in}/execution-request.json"
            self._put_checked(state, Path(execution_request), remote_exec)
            remote_args["execution_request"] = remote_exec
        state.note("upload", "done", inputs=len(remote_mapping))
        return remote_args

    # -- step 5 ------------------------------------------------------------

    def transform_command(
        self,
        state: RemoteRun,
        remote_args: Mapping[str, str],
        *,
        project: str | None,
        editorial_approved: bool,
    ) -> str:
        """The same local command, with ``--placement local`` and pod paths."""
        argv = self._command_argv(
            remote_args, state.remote_out, project=project, editorial_approved=editorial_approved
        )
        line = " ".join(shlex.quote(part) for part in argv)
        return (
            "set -u\n"
            f"{line} > {shlex.quote(state.remote_root + '/result.json')}\n"
            f"rc=$?\n"
            f"if [ $rc -ne 0 ]; then exit $rc; fi\n"
            f"printf '%s' {shlex.quote(RESULT_MARK)}; tr -d '\\n' < {shlex.quote(state.remote_root + '/result.json')}; echo\n"
            f"{manifest_command(state.remote_out)}\n"
        )

    def _start_transform(self, state, remote_args, *, project, editorial_approved) -> str:
        command = self.transform_command(
            state, remote_args, project=project, editorial_approved=editorial_approved
        )
        stdout = self._run_job(state, command, "h3_av.transform")
        state.note("job", "done")
        return stdout

    # -- step 6 ------------------------------------------------------------

    def _local_for(self, state: RemoteRun, remote: str) -> Path:
        prefix = state.remote_out.rstrip("/") + "/"
        if not remote.startswith(prefix):
            raise RemotePlacementError(f"output path is outside the run directory: {remote}")
        relative = remote[len(prefix):]
        if ".." in Path(relative).parts:
            raise RemotePlacementError(f"output path escapes the run directory: {remote}")
        return state.local_out / relative

    def _download(self, state: RemoteRun, stdout: str) -> dict[str, Any]:
        assert state.claim is not None
        ops = state.claim.ops
        result_line = next((l for l in stdout.splitlines() if l.startswith(RESULT_MARK)), None)
        manifest_line = next((l for l in stdout.splitlines() if l.startswith(MANIFEST_MARK)), None)
        if result_line is None or manifest_line is None:
            raise RemotePlacementError("remote job did not report a result and manifest digest")
        try:
            remote_result = json.loads(result_line[len(RESULT_MARK):])
        except json.JSONDecodeError as exc:
            raise RemotePlacementError("remote result is not JSON") from exc
        if not isinstance(remote_result, dict):
            raise RemotePlacementError("remote result is not a JSON object")
        self._check_deadline("download")
        manifest_local = state.local_out / MANIFEST_NAME
        ops.get(f"{state.remote_out}/{MANIFEST_NAME}", manifest_local)
        if sha256_file(manifest_local) != manifest_line[len(MANIFEST_MARK):].strip():
            manifest_local.unlink(missing_ok=True)
            raise DigestMismatch("downloaded manifest does not match the digest the job reported")
        recorded = json.loads(manifest_local.read_text(encoding="utf-8")).get("files", {})
        # Declared outputs: every path in the result, the receipt and the journal.
        wanted: dict[str, Path] = {}
        local_result: dict[str, Any] = {}
        for key, value in remote_result.items():
            if isinstance(value, str) and value.startswith(state.remote_out + "/"):
                wanted[value] = self._local_for(state, value)
                local_result[key] = str(wanted[value])
            else:
                local_result[key] = value
        for name in FIXED_OUTPUTS:
            remote = f"{state.remote_out}/{name}"
            wanted.setdefault(remote, self._local_for(state, remote))
        for remote, local in sorted(wanted.items()):
            relative = remote[len(state.remote_out) + 1:]
            entry = recorded.get(relative)
            if not isinstance(entry, Mapping):
                raise DigestMismatch(f"{relative} has no digest in the pod manifest")
            local.parent.mkdir(parents=True, exist_ok=True)
            ops.get(remote, local)
            if sha256_file(local) != entry.get("sha256") or local.stat().st_size != entry.get("size"):
                local.unlink(missing_ok=True)
                raise DigestMismatch(f"downloaded {relative} does not match the pod manifest")
        self._check_receipt_digests(local_result)
        state.note("download", "done", files=len(wanted))
        return local_result

    @staticmethod
    def _check_receipt_digests(result: Mapping[str, Any]) -> None:
        """The composition manifest's candidate digest must match the downloaded candidate."""
        composition, candidate = result.get("composition"), result.get("candidate")
        if not composition or not candidate:
            return
        document = json.loads(Path(str(composition)).read_text(encoding="utf-8"))
        expected = (document.get("candidate") or {}).get("sha256")
        if expected is not None and expected != sha256_file(Path(str(candidate))):
            raise DigestMismatch("downloaded candidate does not match the composition receipt digest")

    def _verify(self, state: RemoteRun, result: Mapping[str, Any]) -> None:
        candidate = result.get("candidate")
        if candidate:
            self._ffprobe_fn(Path(str(candidate)))
        self._verify_fn(state.local_out, result)
        state.note("verify", "done")

    # -- step 7 ------------------------------------------------------------

    def _release(self, state: RemoteRun, *, failed: bool) -> RemotePlacementError | None:
        claim = state.claim
        if claim is None:
            return None
        if state.job_id is not None:
            try:
                claim.ops.job_cancel(state.job_id)
            except BaseException:  # best effort; release is what stops the spend
                pass
        if self.leave_running and not failed:
            state.note("release", "skipped", reason="leave_running")
            return None
        try:
            self.provider.release(claim)
            if self.provider.pod_exists(claim.pod_id):
                raise RemotePlacementError(
                    f"pod {claim.pod_id} still exists after release (GET did not return 404)"
                )
        except RemotePlacementError as exc:
            state.note("release", "failed", error=str(exc))
            return exc
        except BaseException as exc:
            state.note("release", "failed", error=type(exc).__name__)
            return RemotePlacementError(f"pod {claim.pod_id} release failed: {type(exc).__name__}: {exc}")
        state.released = True
        state.note("release", "done", pod_id=claim.pod_id)
        return None

    def _finish(self, state: RemoteRun, result: dict[str, Any]) -> dict[str, Any]:
        claim = state.claim
        assert claim is not None
        return {
            **result,
            "placement": PLACEMENT,
            "pod": {
                "pod_id": claim.pod_id,
                "released": state.released,
                "leave_running": self.leave_running,
                "claim": dict(claim.identity),
            },
            "remote_steps": list(state.steps),
        }


# ----------------------------------------------------------------------------
# Default verification on the downloaded copies
# ----------------------------------------------------------------------------


def _default_ffprobe(path: Path) -> None:
    from astrid.packs.h3_av.actions.verify.verify import _probe_strict

    _probe_strict(path)


def _default_verify(out: Path, result: Mapping[str, Any]) -> None:
    from astrid.packs.h3_av.actions.verify.verify import verify_candidate

    preparation = json.loads(Path(str(result["preparation"])).read_text(encoding="utf-8"))
    composition = json.loads(Path(str(result["composition"])).read_text(encoding="utf-8"))
    verify_candidate(
        preparation=preparation,
        composition=composition,
        candidate=Path(str(result["candidate"])),
    )


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

    def job_start(self, command: str) -> Mapping[str, Any]:
        return self._call("detached_start", command)

    def job_poll(self, job_id: str) -> Mapping[str, Any]:
        return self._call("detached_poll", job_id)

    def job_cancel(self, job_id: str) -> Mapping[str, Any]:
        return self._call("detached_cancel", job_id)


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
        from runpod_lifecycle import RunPodConfig, api

        config = RunPodConfig.from_env(transport="https")
        return api.get_pod_status(pod_id, config.api_key) is not None


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


def run_remote_placement(
    args: Any,
    *,
    provider: PodProvider | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Run ``h3_av.transform`` args on a claimed RunPod pod and return the local result shape."""
    env = os.environ if env is None else env
    out = Path(args.out).expanduser().resolve()
    source = env.get("ASTRID_RUNPOD_SOURCE_ROOT")
    wrapper = RemotePlacement(
        provider or LifecyclePodProvider(out / "runpod-claims"),
        limits=Limits.from_env(env),
        source_root=Path(source) if source else None,
        leave_running=env.get("ASTRID_RUNPOD_LEAVE_RUNNING", "") in ("1", "true", "yes"),
        pod_id=env.get("ASTRID_RUNPOD_POD_ID") or None,
        **overrides,
    )
    return wrapper.run(
        request=args.request,
        asset_map=args.asset_map,
        out=out,
        project=getattr(args, "project", None),
        editorial_approved=bool(getattr(args, "editorial_approved", False)),
        execution_request=getattr(args, "execution_request", None),
        cleanup_receipt=getattr(args, "cleanup_receipt", None),
    )
