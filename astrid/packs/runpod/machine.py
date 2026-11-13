"""RunPod as a remote machine: the ``runpod`` contribution to ``generation.machine`` (Plan 32 T5).

The controller rents the pod, ships the job, runs the same local command there
(``astrid generate <entry> --placement local``), copies the job's progress file
back on each poll, copies the outputs back under a digest manifest and settles
the pod idle when verification succeeds. Cancel is ``job_cancel``. Nothing about the pod's models or
nodes travels back to the controller: the pod aligns its own disk.

Money rules (pinned by ``tests/packs/runpod/test_machine.py``):

* one claim per job, under the existing paid-consent and lease owner
  (``RunPodLeaseOwner``); this module never retries a claim;
* a failed job settles idle only after exact job stop, installation verification
  and in-place reset; unknown custody or preparation releases the exact pod;
* the detached job id is checkpointed in the lease journal before the job
  starts, so a crashed host's job is cancelled by ``recover()``.
* the pod keeps one long-lived Runtime (T5f): after the digest-verified fetch a
  detached reset (``pod_reset``) restores the empty-realm template in place,
  before the lease settles warm; a failed reset releases the pod. The next job's
  command opens the prepared connection: no ``up`` and no ``connect`` per job.

Warm reuse is keyed on one machine-custody digest of the account, the observed
GPU type, the image, the pinned volume (id, mount path, cache device, mount id)
and the shipped source digest. Any difference refuses reuse.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shlex
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .failure_receipt import checkpoint_refusal
from .remote_placement import (
    MANIFEST_MARK,
    MANIFEST_NAME,
    POD_VENV,
    RESULT_MARK,
    RUNS_ROOT,
    Claim,
    DigestMismatch,
    RemotePlacement,
    RemotePlacementError,
    RemoteRun,
    manifest_command,
    sha256_file,
)

KIND = "runpod"
POLL_SECONDS = 15.0
PROGRESS_NAME = "progress.json"
JOB_NAME = "job.json"
MAX_PROGRESS_BYTES = 64 * 1024
PROGRESS_KEYS = frozenset({"phase", "percent", "current", "total"})
CUSTODY_SCHEMA = "astrid.runpod.machine-custody.v1"
#: The local private engine footprint the storage probe requires besides the volume.
LOCAL_FOOTPRINT_BYTES = 20 * 10**9


class MachineError(RemotePlacementError):
    """A remote-machine step failed; retention follows exact custody checks."""


class InstallationUnverified(MachineError):
    """A completed job did not attest the observed installation."""


class MachineCancelled(MachineError):
    """The local run was cancelled; the remote job was stopped."""


class CustodyChanged(MachineError):
    """The warm machine's custody differs from this job's; warm reuse is refused."""


def _installation_claim(owner: Any, lease: Any) -> dict[str, Any]:
    """Keep the owner's exact fence and explain which checkpoint fence refused."""
    from .worker_owner import RunPodWorkerOwnerError

    try:
        return owner.preparation_claim(lease.lease)
    except RunPodWorkerOwnerError as error:
        try:
            record = owner._read() or {}
        except (RunPodWorkerOwnerError, OSError, ValueError, KeyError, TypeError):
            raise checkpoint_refusal(error, "checkpoint_custody_changed") from None
        info = record.get("lease") or {}
        if not record or not info:
            reason = "checkpoint_custody_changed"
        elif owner.clock() >= info.get("paid_expires_at", float("inf")):
            reason = "checkpoint_paid_expired"
        elif (owner.epoch != lease.lease.owner_epoch
              or info.get("owner_epoch") != lease.lease.owner_epoch):
            reason = "checkpoint_epoch_changed"
        elif (record.get("operation_id") != lease.lease.operation_id
              or info.get("generation") != lease.lease.generation
              or info.get("invocation_id") != lease.lease.invocation_id):
            reason = "checkpoint_invocation_changed"
        else:
            reason = "checkpoint_custody_changed"
        raise checkpoint_refusal(error, reason)


# ----------------------------------------------------------------------------
# Machine custody (warm-reuse key)
# ----------------------------------------------------------------------------


def machine_custody(*, account: str, gpu_type: str, image: str, volume: Mapping[str, Any],
                    source_digest: str) -> dict[str, Any]:
    """The one record a warm machine is reused against. ``gpu_type`` is the observed one."""
    for name, value in (("account", account), ("gpu_type", gpu_type), ("image", image),
                        ("source_digest", source_digest)):
        if not isinstance(value, str) or not value:
            raise MachineError(f"machine custody needs a {name}")
    pinned = {key: volume.get(key) for key in ("id", "mount_path", "cache_device", "mount_id")}
    body = {"schema": CUSTODY_SCHEMA, "account": account, "gpu_type": gpu_type, "image": image,
            "volume": pinned, "source_digest": source_digest}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                       allow_nan=False).encode()).hexdigest()
    return {**body, "digest": "sha256:" + digest}


def custody_differences(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> list[str]:
    """Fields that refuse warm reuse; empty means the warm machine is this job's machine."""
    if not isinstance(previous, Mapping) or previous.get("schema") != CUSTODY_SCHEMA:
        return ["custody"]
    names = [name for name in ("account", "gpu_type", "image", "source_digest")
             if previous.get(name) != current.get(name)]
    before, after = previous.get("volume") or {}, current.get("volume") or {}
    names += [f"volume.{key}" for key in ("id", "mount_path", "cache_device", "mount_id")
              if before.get(key) != after.get(key)]
    if not names and previous.get("digest") != current.get("digest"):
        names.append("digest")
    return names


# ----------------------------------------------------------------------------
# Storage pin (the volume mounted as the model cache)
# ----------------------------------------------------------------------------


def storage_probe_command(request: Mapping[str, Any]) -> str:
    """The pack's standard-library probe, shipped as source (as scripts/pod_session.py does)."""
    source = (Path(__file__).with_name("storage_probe.py")).read_text(encoding="utf-8")
    blob = base64.b64encode(json.dumps(dict(request), sort_keys=True).encode()).decode()
    return "python3 -c " + shlex.quote(source) + " " + shlex.quote(blob)


def pin_storage(placement: RemotePlacement, state: RemoteRun, *, pod_id: str, volume_id: str,
                mount_path: str, model_cache_root: str, engine_root: str, required_volume_bytes: int,
                observe_mounts: Callable[[str, str], Mapping[str, Any]]) -> dict[str, Any]:
    """Prove the saved volume is the writable mount before any install writes to it.

    The provider mapping comes from ``runpod_lifecycle.discovery.observe_pod_mounts``;
    the pod-side probe then joins it to ``/proc/self/mountinfo``. The receipt's
    cache device and mount id are pinned in machine custody.
    """
    from .storage_probe import strict_json_object, validate_provider_mapping

    nonce = secrets.token_hex(16)
    try:
        provider = dict(observe_mounts(pod_id, nonce))
        validate_provider_mapping(provider, pod_id=pod_id, volume_id=volume_id, nonce=nonce, mount=mount_path)
    except (ValueError, RuntimeError) as exc:
        raise MachineError(f"volume mount mapping refused before install: {exc}") from None
    request = {"pod_id": pod_id, "volume_id": volume_id, "nonce": nonce, "mount": mount_path,
               "model_cache_root": model_cache_root, "engine_root": engine_root,
               "required_volume_bytes": max(1, int(required_volume_bytes)),
               "required_local_bytes": LOCAL_FOOTPRINT_BYTES, "provider_mapping": provider}
    code, stdout, _stderr = placement._exec(state, storage_probe_command(request), timeout=120)
    if code != 0:
        raise MachineError("mounted storage verification failed before install")
    receipt = strict_json_object(stdout.strip().splitlines()[-1] if stdout.strip() else "")
    expected = {key: value for key, value in request.items() if key != "provider_mapping"}
    if (any(receipt.get(key) != value for key, value in expected.items())
            or type(receipt.get("cache_device")) is not int or receipt["cache_device"] < 0
            or type(receipt.get("cache_mount_id")) is not int or receipt["cache_mount_id"] <= 0):
        raise MachineError("storage evidence differs from the requested mount")
    state.note("storage", "pinned")
    return {"mode": "volume", "id": volume_id, "mount_path": mount_path,
            "cache_device": receipt["cache_device"], "mount_id": receipt["cache_mount_id"]}


# ----------------------------------------------------------------------------
# Pod-side commands
# ----------------------------------------------------------------------------


SELECTION_NAME = "selection.tar.gz"
SELECTION_MANIFEST = "selection.json"


def selection_env(remote_root: str) -> str:
    """Point the pod-side run at the shipped selection only (no pod-local packs or entries)."""
    root = f"{remote_root}/selection"
    return " ".join(f"{name}={shlex.quote(value)}" for name, value in (
        ("ASTRID_PACKS_PATH", f"{root}/packs"),
        ("VIBECOMFY_ENTRIES_DIR", f"{root}/entries"),
        ("ASTRID_SOURCE_STATE", f"{root}/no-managed-sources.json"),
        ("ASTRID_USER_PACKS", f"{root}/no-user-packs"),
    ))


def extract_selection_command(remote_root: str) -> str:
    root = shlex.quote(f"{remote_root}/selection")
    return (f"mkdir -p {root}/packs {root}/entries {root}/no-user-packs && "
            f"tar -xzf {shlex.quote(f'{remote_root}/{SELECTION_NAME}')} -C {root}")


# The pod's one long-lived Runtime and its in-place reset (T5f): see pod_reset.py.
POD_STATE_MARK = "pod-state:"
#: Bound on one detached reset (restore + replace + one ensure) before the pod is released.
RESET_SECONDS = 600.0
FAILURE_VERIFY_SECONDS = 120.0


def pod_paths() -> dict[str, str]:
    from . import pod_reset

    return {"root": str(pod_reset.POD_ROOT), "data_root": str(pod_reset.DATA_ROOT),
            "source_manifest": str(pod_reset.SOURCE_MANIFEST), "ready": str(pod_reset.READY),
            "prepared": str(pod_reset.PREPARED), "active": str(pod_reset.ACTIVE_JOB),
            "lock": str(pod_reset.LOCK), "prepared_env": pod_reset.PREPARED_ENV}


def pod_state_command() -> str:
    """``ready`` (ready.json and a prepared connection, no active job), ``pending`` or ``absent``."""
    p = {key: shlex.quote(value) for key, value in pod_paths().items()}
    return (f"s=absent; if [ -f {p['ready']} ] && [ -f {p['prepared']} ]; then s=ready; fi; "
            f"if [ -e {p['active']} ]; then s=pending; fi; echo {POD_STATE_MARK}$s")


def reset_command(*, run_dir: str | None = None, job_dir: str | None = None) -> str:
    """The detached in-place reset: template restore + replace, sweep, one ensure, ready.json."""
    argv = [f"{POD_VENV}/bin/python", "-m", "astrid.packs.runpod.pod_reset", "reset"]
    if run_dir:
        argv += ["--run-dir", run_dir]
    if job_dir:
        argv += ["--job-dir", job_dir]
    return " ".join(shlex.quote(part) for part in argv)


def parse_reset(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, Mapping) and "reset" in value:
            return dict(value)
    return {"reset": "failed", "error": "the reset printed no result"}


def ensure_pod_ready(placement: RemotePlacement, state: RemoteRun) -> dict[str, Any]:
    """At the claim: a ready warm pod is opened as is; otherwise the reset runs first.

    A fresh pod (no ready.json) takes its empty-realm template here; a pod left
    with ``active-job.json`` (a job that never reached its reset) is reset again.
    """
    code, stdout, stderr = placement._exec(state, pod_state_command(), timeout=60)
    line = next((row for row in stdout.splitlines() if row.startswith(POD_STATE_MARK)), None)
    if code != 0 or line is None:
        raise MachineError(f"pod readiness check failed (exit {code}): {(stderr or stdout)[-400:]}")
    found = line[len(POD_STATE_MARK):].strip()
    if found == "ready":
        state.note("pod_ready", "ready")
        return {"entry": None, "ready": True, "found": found, "reset": "skipped"}
    from . import pod_reset

    result = parse_reset(placement._run_job(state, reset_command(), "pod reset"))
    if result.get("reset") != "ok":
        raise MachineError(f"pod reset failed: {pod_reset.reset_failure_reason(result)}")
    state.note("pod_ready", "reset", found=found)
    return {"entry": None, "ready": True, "found": found, "reset": result.get("template") or "restored"}


_ENTRY_CHECK = (
    "import json,sys\n"
    "rows=json.load(open(sys.argv[1],encoding='utf-8'));entry=sys.argv[2] or None;mode=sys.argv[3] or None\n"
    "if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows): sys.exit('the pod runtime did not list entries as JSON')\n"
    "if entry is None: sys.exit(0)\n"
    "rows=[r for r in rows if r.get('id')==entry]\n"
    "if not rows: sys.exit(f\"entry {entry!r} is not listed by the pod's runtime\")\n"
    "rows=[r for r in rows if r.get('backend')=='local' and (mode is None or r.get('mode')==mode)]\n"
    "if not rows: sys.exit(f\"entry {entry!r} has no local declaration for the requested mode\")\n"
    "if any(not isinstance(r.get('mode'),str) or not r['mode'] or type(r.get('ready')) is not bool for r in rows): sys.exit('the pod runtime listed an invalid entry declaration')\n"
    "if mode is None and len({r['mode'] for r in rows})!=1: sys.exit('the pod entry requires an explicit mode')\n"
    # Listing readiness probes attached targets before the managed job starts
    # its engine and aligns missing models. Declaration availability gates the
    # selected local route here; the actual job owns execution readiness.
    "if any(r.get('availability')!='available' for r in rows): sys.exit(f\"entry {entry!r} is unavailable on the pod's runtime\")\n"
)


#: One line: writes active-job.json atomically, adding the job's own agent folder.
_ACTIVE_WRITE = (
    "import json,os,sys; value=json.loads(sys.argv[1]); value['job_dir']=os.path.abspath(sys.argv[2]); "
    "tmp=sys.argv[3]+'.partial'; open(tmp,'w',encoding='utf-8').write(json.dumps(value,sort_keys=True)); "
    "os.replace(tmp,sys.argv[3])"
)


def build_selection(entry: str | None, staging: Path, *, sources: Any = None,
                    entries_dir: Path | None = None) -> tuple[Path, dict[str, Any]]:
    """The job's shipped selection: every active installed pack (Plan 06), plus the user entry folder.

    Built-in packs are already on the pod (the pinned Agent source). Each installed
    pack ships whole, pinned by its revision and tree digest; a ``--as-entry`` user
    entry ships its own folder from ``VIBECOMFY_ENTRIES_DIR``. ``selection.json``
    records both, with a sha256 per shipped file.
    """
    import tarfile

    if sources is None:
        from astrid.core.pack.source_setup import active_sources
        sources = active_sources(verify=True)
    members: list[tuple[str, Path]] = []
    packs = []
    for source in sources:
        root = Path(source.pack_root)
        files = _tree_files(root)
        packs.append({"pack_id": source.pack_id, "revision": source.revision, "repository": source.repository,
                      "tree_sha256": source.tree_sha256, "manifest_sha256": source.manifest_sha256,
                      "files": {rel: sha256_file(path) for rel, path in files}})
        members += [(f"packs/{source.pack_id}/{rel}", path) for rel, path in files]
    shipped_entries = []
    folder = _user_entry_folder(entry, entries_dir)
    if folder is not None:
        files = _tree_files(folder)
        shipped_entries.append({"id": entry, "folder": folder.name,
                                "files": {rel: sha256_file(path) for rel, path in files}})
        members += [(f"entries/{folder.name}/{rel}", path) for rel, path in files]
    manifest = {"schema": "astrid.machine-selection.v1", "entry": entry, "packs": packs, "entries": shipped_entries}
    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / SELECTION_NAME
    body = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    with tarfile.open(archive, "w:gz") as handle:
        for name, path in members:
            info = handle.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            with path.open("rb") as stream:
                handle.addfile(info, stream)
        import io
        info = tarfile.TarInfo(SELECTION_MANIFEST)
        info.size, info.mode, info.mtime = len(body), 0o644, 0
        handle.addfile(info, io.BytesIO(body))
    return archive, manifest


def _tree_files(root: Path) -> list[tuple[str, Path]]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", "__pycache__"))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if name.endswith(".pyc") or path.is_symlink():
                continue
            files.append((path.relative_to(root).as_posix(), path))
    return files


def _user_entry_folder(entry: str | None, entries_dir: Path | None = None) -> Path | None:
    if not entry:
        return None
    try:
        from vibecomfy.entries import entries_dir as default_dir
        root = Path(entries_dir) if entries_dir is not None else Path(default_dir())
    except Exception:
        return None
    for path in sorted(root.glob("*/entry.json")):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("id") == entry:
                return path.parent
        except (OSError, ValueError):
            continue
    return None


def generation_argv(entry: str | None, remote_root: str, *, mode: str | None) -> list[str]:
    """The ONE pod-side invocation: the same local run the controller would do.

    The job form runs the shipped job through the local generation command.
    A job with no named entry runs its shipped bundle: ``astrid generate workflow``.
    """
    from astrid.core.execution.machine_run import WORKFLOW_FILE_ENTRY

    if entry == WORKFLOW_FILE_ENTRY:
        raise MachineError("a workflow-file job has no entry; pass entry=None")
    argv = [f"{POD_VENV}/bin/astrid", "generate", entry or WORKFLOW_FILE_ENTRY, "--placement", "local",
            "--job", f"{remote_root}/{JOB_NAME}", "--out", f"{remote_root}/out", "--json"]
    if mode:
        argv += ["--mode", mode]
    return argv


def generation_command(entry: str | None, remote_root: str, *, mode: str | None,
                       active_job: Mapping[str, Any] | None = None) -> str:
    """The job's one pod command: the entry check and the run on the prepared connection.

    It holds the reset lock shared, requires ready.json with no pending reset,
    records ``active-job.json`` (deleted by the next reset), and opens the
    Runtime through the prepared connection: no ``up`` and no ``connect``.
    """
    from astrid.core.execution.machine_run import WORKFLOW_FILE_ENTRY

    p = {key: shlex.quote(value) for key, value in pod_paths().items()}
    line = " ".join(shlex.quote(part) for part in generation_argv(entry, remote_root, mode=mode))
    result = shlex.quote(f"{remote_root}/result.json")
    listing = shlex.quote(f"{remote_root}/listing.json")
    active = json.dumps({**dict(active_job or {"run_id": remote_root.rsplit("/", 1)[-1]}), "run_dir": remote_root},
                        sort_keys=True)
    check_entry = entry if entry is not None and entry != WORKFLOW_FILE_ENTRY else ""
    finish = finish_command(remote_root)
    return (
        "set -u\n"
        f"finish_logs() {{ {finish}; }}\n"
        f"exec 9>>{p['lock']}\n"
        "flock -s 9 || exit 75\n"
        f"if [ ! -f {p['ready']} ] || [ ! -f {p['prepared']} ] || [ -e {p['active']} ]; then "
        "echo 'the pod is not ready: a reset is pending' >&2; exit 75; fi\n"
        # active-job.json names this job's run folder and its own agent job folder
        # (the agent runs `bash "$d/command.sh"`), so a redone reset can remove both.
        f"{POD_VENV}/bin/python -c {shlex.quote(_ACTIVE_WRITE)} {shlex.quote(active)} \"$(dirname \"$0\")\" "
        f"{p['active']} || exit 75\n"
        f"export {p['prepared_env']}={p['prepared']} ASTRID_RUNTIME_DATA_ROOT={p['data_root']} "
        f"ASTRID_RUNTIME_SOURCE_MANIFEST={p['source_manifest']} ASTRID_RUNTIME_DOOR_PORT=0\n"
        f"export ASTRID_PROGRESS_PATH={shlex.quote(f'{remote_root}/{PROGRESS_NAME}')}\n"
        f"export {selection_env(remote_root)}\n"
        f"{extract_selection_command(remote_root)} || {{ finish_logs; exit 3; }}\n"
        f"{POD_VENV}/bin/astrid generate list --json > {listing} || {{ finish_logs; exit 3; }}\n"
        f"{POD_VENV}/bin/python -c {shlex.quote(_ENTRY_CHECK)} {listing} {shlex.quote(check_entry)} {shlex.quote(mode or '')} || {{ finish_logs; exit 3; }}\n"
        f"{line} > {result}\n"
        "rc=$?\n"
        "finish_logs\n"
        f"if [ $rc -ne 0 ]; then {POD_VENV}/bin/python -c "
        f"{shlex.quote(Path(__file__).with_name('failure_diagnostics.py').read_text(encoding='utf-8'))} "
        f"{result} {shlex.quote(f'{remote_root}/{JOB_NAME}')}; exit $rc; fi\n"
        f"printf '%s' {shlex.quote(RESULT_MARK)}; tr -d '\\n' < {result}; echo\n"
        f"{manifest_command(f'{remote_root}/out')}\n"
    )


def finish_command(remote_root: str, *, python: str = f"{POD_VENV}/bin/python") -> str:
    """Best-effort declaration, collection and transfer manifest for pod job logs."""
    from astrid.core._shared import run_logs

    source = shlex.quote(Path(run_logs.__file__).read_text(encoding="utf-8"))
    interpreter = shlex.quote(python)
    out = f"{remote_root}/out"
    jobdir = '"$(dirname "$0")"'
    commands = [
        f"{interpreter} -c {source} declare {shlex.quote(out)} --name job.stdout --role stdout --path {jobdir}/stdout",
        f"{interpreter} -c {source} declare {shlex.quote(out)} --name job.stderr --role stderr --path {jobdir}/stderr",
        f"{interpreter} -c {source} declare {shlex.quote(out)} --name generation.stdout --role stdout --path {shlex.quote(f'{remote_root}/result.json')}",
        f"{interpreter} -c {source} collect {shlex.quote(out)}",
        f"{manifest_command(f'{out}/logs', interpreter)} | sed 's/^{MANIFEST_MARK}/LOGS_MANIFEST_SHA256=/'",
    ]
    return "( " + "; ".join(commands) + " ) 2>/dev/null || true"


LOGS_MANIFEST_MARK = "LOGS_MANIFEST_SHA256="


def _last_marked(text: str, mark: str) -> str | None:
    """The last line of ``text`` that starts with ``mark``."""
    return next((line for line in reversed(text.splitlines()) if line.startswith(mark)), None)


def _stamp(timing: Any, name: str, **kw: Any) -> None:
    from astrid.core.execution.phase_timing import stamp
    stamp(timing, name, **kw)


# ----------------------------------------------------------------------------
# The machine
# ----------------------------------------------------------------------------


@dataclass
class MachineLease:
    """One job's custody of a claimed (or warm) pod."""

    lease: Any  # RunPodLease
    claim: Claim
    state: RemoteRun
    custody: Mapping[str, Any]
    warm: bool
    released: bool = False
    #: The job's run directory and detached job directory, removed by the reset.
    run_dir: str | None = None
    job_dir: str | None = None
    node_ids: frozenset[str] = frozenset()
    #: Node class / input names the submitted graph itself uses (never values).
    node_names: frozenset[str] = frozenset()
    #: This job's phase-timing collector (``astrid.core.execution.phase_timing``), if the host supplied one.
    timing: Any = None
    #: What release decided, for the failure receipt: the exact-release reason, and whether the pod was kept.
    release_reason: str | None = None
    kept: bool | None = None
    # Runtime metadata attached during machine execution and settlement.
    unsafe_job_stop: bool = False
    failure_diagnostic: Mapping[str, Any] | None = None
    failure_exit_code: int | None = None
    failure_logs: Mapping[str, Any] | None = None
    failure_install_receipt: str | None = None
    expires_at: float | None = None
    cost_usd: float | None = None
    preparation_state: str | None = None


@dataclass
class MachineRun:
    lease: MachineLease
    job_id: str
    directory: str
    remote_root: str
    progress_path: Path
    scratch: Path
    progress: list[dict[str, Any]] = field(default_factory=list)
    state: str = "running"
    task_id: str | None = None
    attempt_id: str | None = None
    #: The same host callback used during start, also checked during polling.
    cancelled: Callable[[], bool] | None = None


@contextmanager
def _cancellable(
    lease: MachineLease, cancelled: Callable[[], bool] | None,
) -> Iterator[Callable[[], None]]:
    """Interrupt work with the host's callback, then restore transport for cleanup."""
    from astrid.core.execution.machine_run import MachineRunCancelled

    def check_cancelled() -> None:
        if cancelled is not None and cancelled():
            raise MachineRunCancelled("generation cancelled during remote machine work")

    original_ops = lease.claim.ops
    try:
        check_cancelled()
        factory = getattr(original_ops, "cancellable", None)
        if cancelled is not None and factory is not None:
            lease.claim.ops = factory(cancelled)
        yield check_cancelled
        check_cancelled()
    except BaseException as error:
        if not isinstance(error, MachineRunCancelled):
            # A launch may have committed but lost its acknowledgement while
            # the abort arrived. Release reconciles the durable exact job id.
            check_cancelled()
        raise
    finally:
        lease.claim.ops = original_ops


def _job_manifest_rows(entry: str | None, values: Any, *, mode: str | None = None,
                       execution: Mapping[str, Any] | None = None) -> tuple[Mapping[str, Any], ...]:
    """The job's model manifest rows: the entry's requirements, as align() reads them on the pod."""
    from astrid.core.generation import entries as catalog

    if not entry or entry == "workflow":
        from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path

        execution = execution or {}
        with staged_workflow_path(**{name: execution.get(name) for name in
                                     ("workflow", "python", "companion", "source")}) as (path, _):
            workflow = catalog._load_entry_workflow({"bundle": str(path)})
            return tuple(catalog.requirements({"workflow": workflow}, values).get("models", ()))

    described = catalog.find_entry(entry)
    if described is None:
        from astrid.core.model_catalog.registry import ModelRegistry

        model = ModelRegistry.load_default().get(entry)
        selected_mode = mode or (next(iter(model.modes)) if len(model.modes) == 1 else None)
        selected = model.modes.get(selected_mode)
        backend = selected.backends.get("local") if selected is not None else None
        if backend is None or not backend.template:
            raise MachineError(f"entry {entry!r} has no local workflow for mode {selected_mode!r}")
        from astrid.core.generation.backends.vibecomfy import _load_pinned_catalog_workflow

        described = {"workflow": _load_pinned_catalog_workflow(backend)}
    return tuple(catalog.requirements(described, values).get("models", ()))


class RunPodMachine:
    """``claim``/``start``/``poll``/``cancel``/``fetch``/``release`` for one host-owned adapter.

    The registered contribution (``MACHINE``) has no adapter; the host builds one
    per retained provider owner with ``for_host``.
    """

    def __init__(self, adapter: Any = None, *, begin: Callable[[Any], None] | None = None,
                 done: Callable[[Any], None] | None = None,
                 observe_mounts: Callable[[str, str], Mapping[str, Any]] | None = None) -> None:
        self.adapter = adapter
        self._begin, self._done = begin, done
        self._observe_mounts = observe_mounts

    def for_host(self, adapter: Any, *, begin: Callable[[Any], None], done: Callable[[Any], None],
                 observe_mounts: Callable[[str, str], Mapping[str, Any]] | None = None) -> "RunPodMachine":
        return type(self)(adapter, begin=begin, done=done,
                          observe_mounts=observe_mounts or getattr(adapter, "observe_mounts", None))

    def _require(self) -> Any:
        if self.adapter is None:
            raise MachineError("the runpod machine has no host-owned lease owner")
        return self.adapter

    # -- claim ---------------------------------------------------------------

    def claim(self, job: Any, *, cancelled: Callable[[], bool] | None = None) -> MachineLease:
        """Attach ``job`` to a claimed pod; retain only verified preparation."""
        from astrid.core.generation.interfaces import RunPodPreparationRequest
        from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path

        from .adapter import prevalidate_task
        from .generation import prepare_retained_generation
        from .target_selection import PaidSelectionError

        adapter = self._require()
        policy, inputs = job.policy, job.inputs
        reason = unshipped_entry_reason(job.entry)
        if reason is not None:
            # Before the claim: the pod only gets the pinned source tuple and this job directory.
            raise MachineError(reason)
        prevalidate_task(dict(policy), dict(inputs), scratch=job.scratch)
        owner = adapter.lease_owner
        if policy.get("kind") != KIND or policy.get("engine") != adapter.installation.engine:
            raise PaidSelectionError("target policy differs from the retained owner")
        pinned = policy.get("machine")

        def check_pin():
            if pinned is None:
                return
            record = owner.selection._read()
            lease = record.get("lease", {}) if record else {}
            live = ([record["pod_id"]] if record and record.get("state") == "claimed"
                    and lease.get("state") in {"active", "idle"}
                    and owner.clock() < lease.get("paid_expires_at", 0)
                    and (lease["state"] != "idle" or owner.clock() < lease.get("idle_expires_at", 0))
                    and record.get("pod_id") else [])
            if pinned not in live:
                ended = next((row for row in lease_rows(owner) if row["id"] == pinned), None)
                reason = f" (ended: {ended['ended']})" if ended and ended.get("ended") else ""
                raise PaidSelectionError(f"no live runpod lease for machine {pinned!r}{reason}; "
                                         f"live leases: {', '.join(live) or 'none'}")

        check_pin()
        consent = owner.job_limits(job.invocation_id, policy)
        # Observe only after local admission: absent or expired claims never
        # reach the provider. Preserve the detailed pin diagnostic if it ended.
        owner.observe_liveness(observe=getattr(adapter, "observe_pod", None))
        check_pin()
        values = json.loads(inputs.get("workflow_inputs") or "{}")
        # The volume is sized by this job's manifest rows (the list align fetches on the pod), not the lock.
        manifest_rows = _job_manifest_rows(job.entry, values, mode=job.mode, execution=inputs)
        acquired: list[Any] = []

        timing = getattr(job, "timing", None)

        def on_acquired(lease: Any) -> None:
            acquired.append(lease)
            _stamp(timing, "claimed_at")
            _stamp(timing, "preparation_started_at")
            if self._begin is not None:
                self._begin(lease)

        def annotate_claim_failure(error: BaseException) -> None:
            if not acquired:
                return
            record = owner.selection._read() or {}
            info = record.get("lease") or {}
            setattr(error, "machine_failure", {
                "id": record.get("pod_id"), "lease": record.get("operation_id"),
                "generation": info.get("generation"), "device": record.get("gpu_type"),
                "after": ("released" if record.get("state") == "cleanup_complete" else
                          "kept" if info.get("state") == "idle" else "unknown"),
                "release_reason": info.get("ended") or info.get("release_reason") or "preparation_failed",
                "expires_at": info.get("idle_expires_at"),
                "cost_usd": record.get("estimated_cost_usd", 0) + max(0, owner.clock() - info.get("accounted_at", info.get("started_at", owner.clock()))) * info.get("hourly_usd", 0) / 3600,
                "preparation_state": (info.get("preparation") or {}).get("state"),
            })

        with staged_workflow_path(workflow=None, python=inputs["python"], companion=inputs["companion"],
                                  source=inputs["source"], scratch=job.scratch) as (workflow, _):
            request = RunPodPreparationRequest({"engine": adapter.installation.engine},
                                               workflow.with_name("source.json"), workflow, values,
                                               consent.consent, consent.max_usd, consent.deadline)
            owner.selection.wait_hook = lambda: _stamp(timing, "capacity_wait_started_at")
            try:
                prepared = prepare_retained_generation(
                    request, consent=consent, lease_owner=owner, placement=adapter.placement,
                    installation=adapter.installation, validate=adapter.validate,
                    requested_idle_seconds=consent.idle_seconds, on_acquired=on_acquired,
                    entry=None, pod_ready=True, observe_mounts=self._observe_mounts,
                    manifest_rows=manifest_rows,
                    **({"cancelled": cancelled} if cancelled is not None else {}),
                )
                _stamp(timing, "preparation_ended_at")
                try:
                    if timing is not None:
                        setattr(timing, "paid_started_at", float(prepared.lease.claim["lease"]["started_at"]))
                except Exception:
                    pass
                # Legacy execution estimates remain timeouts. Without one, use
                # the claim's remaining lifetime; jobs never extend that expiry.
                adapter.placement.begin_execution(deadline_seconds=min(
                    consent.execution_seconds or float("inf"),
                    prepared.lease.claim["lease"]["paid_expires_at"] - owner.clock(),
                ))
            except BaseException as exc:
                if acquired:
                    setattr(exc, "claim_context", {"acquired": True, "preparation_job": False})
                    try:
                        # Read by the failure receipt: a bootstrap/install job was started if it was checkpointed.
                        journal = owner.selection._read() or {}
                        getattr(exc, "claim_context")["preparation_job"] = bool(
                            ((journal.get("lease") or {}).get("preparation") or {}).get("job"))
                    except Exception:
                        pass
                    # Only a fully attested preparation can be reused. Failure
                    # before or during installation leaves unknown pod state.
                    self._finish(acquired[0])
                    try:
                        with owner.lock:
                            record = owner.preparation_claim(acquired[0])
                            prepared = record["lease"].get("preparation") or {}
                            safe = (not owner.closed and prepared.get("state") in {"prepared", "stale_after_failure"}
                                    and (prepared.get("state") == "stale_after_failure" or isinstance(prepared.get("installed"), dict))
                                    and record.get("pod_id") == acquired[0].claim.get("pod_id"))
                    except Exception:
                        safe = False
                    if safe:
                        from .failure_diagnostics import host_summary
                        try:
                            owner.settle(acquired[0], outcome="failed",
                                         failure=host_summary(exc, stage="preparation"))
                        except Exception:
                            owner.release(reason="preparation_settlement_unverified",
                                          _expected_operation=acquired[0].operation_id,
                                          expected_machine_id=acquired[0].claim.get("pod_id"))
                            raise
                    else:
                        from .failure_diagnostics import host_summary
                        try:
                            owner.note_failed_job(acquired[0], host_summary(exc, stage="preparation"))
                        except Exception:
                            pass
                        try:
                            owner.release(reason="preparation_unverified",
                                          _expected_operation=acquired[0].operation_id,
                                          expected_machine_id=acquired[0].claim.get("pod_id"))
                        except BaseException as cleanup_error:
                            annotate_claim_failure(cleanup_error)
                            raise
                else:
                    # acquire failed before on_acquired. Its locked failure
                    # snapshot names only an exact journal-created pod; this
                    # caller is outside generation's preparation lock.
                    identity = getattr(exc, "release_identity", None)
                    if identity is not None:
                        owner.release(reason="custody_changed", _expected_operation=identity[0],
                                      expected_machine_id=identity[1])
                annotate_claim_failure(exc)
                raise
            finally:
                owner.selection.wait_hook = None
        return MachineLease(prepared.lease, prepared.claim, prepared.state, prepared.custody, prepared.reused,
                            timing=timing)

    def describe(self, lease: MachineLease) -> dict[str, Any]:
        """Which machine runs this job: recorded on the attempt when the machine is claimed."""
        record = lease.lease.claim
        return {"placement": KIND, "machine": "runpod.runpod", "id": lease.claim.pod_id,
                "lease": lease.lease.operation_id, "generation": lease.lease.generation,
                "device": lease.custody.get("gpu_type") or record.get("gpu_type"), "warm": lease.warm,
                "idle_timeout_s": record["lease"]["idle_seconds"]}

    # -- start ---------------------------------------------------------------

    def start(self, lease: MachineLease, job: Any, *,
              cancelled: Callable[[], bool] | None = None) -> MachineRun:
        """Ship the job directory, checkpoint the job id, then start the pod-side command."""
        with _cancellable(lease, cancelled) as check_cancelled:
            return self._start(lease, job, cancelled=cancelled, check_cancelled=check_cancelled)

    def _start(self, lease: MachineLease, job: Any, *, cancelled: Callable[[], bool] | None,
               check_cancelled: Callable[[], None]) -> MachineRun:
        adapter = self._require()
        placement, state, ops = adapter.placement, lease.state, lease.claim.ops
        run_id = uuid.uuid4().hex
        remote_root = f"{RUNS_ROOT}/{run_id}"
        placement._exec_ok(state, f"mkdir -p {shlex.quote(remote_root + '/in')} {shlex.quote(remote_root + '/out')}",
                           "job directory")
        check_cancelled()
        staging = Path(job.scratch) / "machine"
        staging.mkdir(parents=True, exist_ok=True)
        shipped: dict[str, Any] = {}
        for name, value in sorted(job.inputs.items()):
            check_cancelled()
            if name == "entry_inputs":
                continue  # this controller-side envelope becomes the job's public inputs below
            if name in job.files:
                local = Path(str(value))
                if local.is_symlink() or not local.is_file():
                    raise MachineError(f"job input {name!r} is not a regular file")
                remote = f"{remote_root}/in/{name}{''.join(local.suffixes)[-16:]}"
                placement._put_checked(state, local, remote)
                check_cancelled()
                shipped[name] = remote
            else:
                shipped[name] = value
        public_inputs = {}
        if job.entry and job.inputs.get("entry_inputs"):
            from astrid.core.generation.job_inputs import read_archive
            def ship(local):
                check_cancelled()
                remote = f"{remote_root}/in/entry-{local.name}"
                placement._put_checked(state, local, remote)
                check_cancelled()
                return remote
            public_inputs = read_archive(Path(str(job.inputs["entry_inputs"])), staging / "entry-inputs", map_file=ship)
        bindings = json.loads(shipped.pop("workflow_inputs", "{}") or "{}")
        if not isinstance(bindings, dict):
            raise MachineError("job entry bindings must be an object")
        body = {"schema": "astrid.machine-job.v1", "entry": job.entry, "mode": job.mode, "align": job.align,
                "task_identity": job.task_id, "attempt_identity": job.attempt_id,
                "idempotency_key": f"remote:{job.task_id}:{job.attempt_id}",
                "inputs": public_inputs, "bindings": bindings, "execution": shipped}
        local_job = staging / JOB_NAME
        local_job.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        from .failure_diagnostics import graph_facts
        lease.node_ids, lease.node_names = graph_facts(job.inputs.get("source"))
        check_cancelled()
        placement._put_checked(state, local_job, f"{remote_root}/{JOB_NAME}")
        check_cancelled()
        # The active pack selection and the user entry ship with the job; the
        # pod-side run resolves its entry from them only.
        archive, selection = build_selection(job.entry, staging)
        check_cancelled()
        placement._put_checked(state, archive, f"{remote_root}/{SELECTION_NAME}")
        check_cancelled()
        archive.unlink(missing_ok=True)
        lease.state.note("selection", "shipped", packs=[row["pack_id"] for row in selection["packs"]],
                         entries=[row["id"] for row in selection["entries"]])
        # The entry check runs inside the job's one command, on the prepared connection.
        command = generation_command(job.entry, remote_root, mode=job.mode, active_job={
            "run_id": run_id, "task": job.task_id, "attempt": job.attempt_id})
        lease.run_dir = remote_root
        job_id = uuid.uuid4().hex
        check_cancelled()
        # The exact detached identity is durable before launch; a lost ACK never
        # invents or retries another job, and recover() can cancel this one.
        self._checkpoint(lease, {"id": job_id, "state": "starting"})
        started = ops.job_start(command, job_id=job_id)
        check_cancelled()
        if started.get("id") != job_id:
            raise MachineError("detached job differs from the checkpointed job custody")
        self._checkpoint(lease, {"id": job_id, "state": "running"})
        _stamp(getattr(lease, "timing", None), "job_started_at")
        lease.job_dir = str(started.get("dir") or "") or None
        state.note("job", "started", job_id=job_id)
        return MachineRun(lease, job_id, str(started.get("dir") or ""), remote_root,
                          Path(job.progress_path), staging, task_id=job.task_id, attempt_id=job.attempt_id,
                          cancelled=cancelled)

    # -- poll ----------------------------------------------------------------

    def poll(self, run: MachineRun) -> dict[str, Any]:
        """One status read, then copy the job's progress file into the local attempt's."""
        with _cancellable(run.lease, run.cancelled):
            adapter = self._require()
            adapter.placement._check_deadline("job poll")
            record = run.lease.claim.ops.job_poll(run.job_id)
            progress = self._copy_progress(run)
            state = record.get("state")
            timing = getattr(run.lease, "timing", None)
            if timing is not None and isinstance(progress, Mapping):
                try:
                    timing.pod_phase(progress.get("phase"))
                except Exception:
                    pass
            if state == "exited":
                _stamp(timing, "job_ended_at", first=True)
                run.state = "exited"
            elif state in {"missing", "cancelled"}:
                run.state = str(state)
            return {"state": run.state, "exit_code": record.get("exit_code"), "progress": progress}

    def _copy_progress(self, run: MachineRun) -> dict[str, Any] | None:
        landing = run.scratch / ("progress-" + run.job_id + ".json")
        try:
            run.lease.claim.ops.get(f"{run.remote_root}/{PROGRESS_NAME}", landing)
        except Exception:
            return None  # the run has not written a phase yet
        try:
            if landing.is_symlink() or landing.stat().st_size > MAX_PROGRESS_BYTES:
                return None
            payload = json.loads(landing.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        finally:
            landing.unlink(missing_ok=True)
        if not isinstance(payload, Mapping):
            return None
        progress = {str(key): value for key, value in payload.items() if str(key) in PROGRESS_KEYS}
        if not progress:
            return None
        # Same file the local child writes; the host already sends it in its heartbeat.
        partial = run.progress_path.with_name(run.progress_path.name + ".machine")
        partial.write_text(json.dumps(progress, sort_keys=True), encoding="utf-8")
        os.replace(partial, run.progress_path)
        if not run.progress or run.progress[-1] != progress:
            run.progress.append(progress)
        return progress

    # -- cancel --------------------------------------------------------------

    def cancel(self, run: MachineRun) -> dict[str, Any]:
        """Stop the remote job and checkpoint its verified stop."""
        stopped = run.lease.claim.ops.job_cancel(run.job_id)
        if stopped.get("id") == run.job_id and stopped.get("state") in {"exited", "cancelled"}:
            self._checkpoint(run.lease, {"id": run.job_id, "state": stopped["state"]})
            run.state = "cancelled"
        else:
            run.lease.unsafe_job_stop = True
            raise MachineError("exact remote job stop could not be verified")
        return dict(stopped)

    # -- fetch ---------------------------------------------------------------

    def fetch(self, run: MachineRun, destination: Path) -> dict[str, Any]:
        """Copy the job's ``out`` directory back, every file against the pod's digest manifest."""
        adapter = self._require()
        placement, state, ops = adapter.placement, run.lease.state, run.lease.claim.ops
        record = ops.job_poll(run.job_id)
        self._checkpoint(run.lease, {"id": run.job_id, "state": "exited"})
        timing = getattr(run.lease, "timing", None)
        _stamp(timing, "job_ended_at", first=True)
        _stamp(timing, "collection_started_at")
        from .failure_diagnostics import MAX_FRAME_BYTES, parse_frame
        failed = type(record.get("exit_code")) is not int or record["exit_code"] != 0
        stdout = ""
        if run.directory:
            command = (f"tail -c {MAX_FRAME_BYTES + 1}" if failed else "cat")
            try:
                _c, stdout, _e = ops.exec(f"{command} {shlex.quote(run.directory)}/stdout", 120)
            except Exception:
                if failed:
                    raise MachineError("remote failure diagnostic unavailable") from None
                raise
        if failed:
            diagnostic = parse_frame(stdout, node_ids=getattr(run.lease, "node_ids", frozenset()),
                                     node_names=getattr(run.lease, "node_names", frozenset()))
            run.lease.failure_diagnostic = diagnostic
            exit_code = record.get("exit_code")
            safe_exit = exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else "unknown"
            run.lease.failure_exit_code = safe_exit if type(safe_exit) is int else None
            run.lease.failure_logs = self._fetch_failure_logs(ops, run, stdout, destination)
            _stamp(timing, "collection_ended_at")
            raise MachineError(f"remote generation failed (exit {safe_exit}; "
                               f"category={diagnostic['category']}; code={diagnostic['code']})")
        lines = stdout.splitlines()
        result_line = next((line for line in lines if line.startswith(RESULT_MARK)), None)
        manifest_line = next((line for line in lines if line.startswith(MANIFEST_MARK)), None)
        if result_line is None or manifest_line is None:
            raise MachineError("remote job did not report a result and manifest digest")
        try:
            result = json.loads(result_line[len(RESULT_MARK):])
        except ValueError:
            raise MachineError("remote result is not JSON") from None
        placement._check_deadline("fetch")
        files = self._fetch_verified(ops, f"{run.remote_root}/out", destination, run.scratch,
                                     manifest_line[len(MANIFEST_MARK):].strip())
        self._checkpoint_installed(run, files)
        state.note("fetch", "done", files=len(files))
        _stamp(timing, "collection_ended_at")
        pid = str(record.get("pid") or "").strip()
        return {"files": files, "result": result if isinstance(result, Mapping) else {},
                "manifest_sha256": manifest_line[len(MANIFEST_MARK):].strip(),
                "progress": list(run.progress),
                # The remote detached job's own process evidence (its pid on the pod).
                "process_id": int(pid) if pid.isdigit() else None, "returncode": 0}

    def _fetch_failure_logs(self, ops: Any, run: MachineRun, stdout: str, destination: Path) -> dict[str, Any]:
        """Bring the failed job's declared logs back, digest-verified, before the reset.

        The logs digest normally sits in the stdout tail already read; when it
        does not (an oversized tail), one bounded grep reads just that marker
        line. Never masks the job's failure: any problem is recorded as
        ``{"unavailable": <exception type>}`` and the original error is raised.
        """
        try:
            line = _last_marked(stdout, LOGS_MANIFEST_MARK)
            if line is None and getattr(run, "directory", None) and hasattr(ops, "exec"):
                command = f"grep -a '^{LOGS_MANIFEST_MARK}' {shlex.quote(run.directory)}/stdout | tail -n 1"
                _code, output, _error = ops.exec(command, 120)
                line = _last_marked(output, LOGS_MANIFEST_MARK)
            if line is not None:
                from astrid.core._shared import run_logs

                local = Path(destination) / "logs"
                verified = self._fetch_verified(ops, f"{run.remote_root}/out/logs", local, run.scratch,
                                                line[len(LOGS_MANIFEST_MARK):].strip())
                rows = run_logs.read_index(Path(destination))
                for row in rows:
                    if row.get("role") == "run" and row.get("file") in verified:
                        try:
                            payload = json.loads(Path(verified[row["file"]]).read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            continue
                        report = payload.get("managed_engine_preparation") if isinstance(payload, dict) else None
                        installation = report.get("installation") if isinstance(report, dict) else None
                        if isinstance(installation, dict) and "receipt_sha256" in installation:
                            run.lease.failure_install_receipt = "sha256:" + str(installation["receipt_sha256"])
                return {"files": [{**row, "path": (str(local / row["file"])
                                                   if not row.get("missing") else None)} for row in rows]}
        except Exception as exc:  # noqa: BLE001 - log collection must never mask the job error
            return {"unavailable": type(exc).__name__}
        return {"unavailable": "not_reported"}

    @staticmethod
    def _fetch_verified(ops: Any, remote_dir: str, destination: Path, scratch: Path,
                        manifest_digest: str) -> dict[str, str]:
        """Copy one pod folder back, every file against its digest manifest.

        The manifest is downloaded and checked against the digest the pod
        printed, then each file is downloaded over the same pod-agent transport,
        rejecting unsafe paths and any digest or size mismatch. Returns
        ``{relative path: local path}``. Shared by the output fetch and any
        later folder fetch (the failure folder).
        """
        manifest_local = scratch / MANIFEST_NAME
        ops.get(f"{remote_dir}/{MANIFEST_NAME}", manifest_local)
        if sha256_file(manifest_local) != manifest_digest:
            manifest_local.unlink(missing_ok=True)
            raise DigestMismatch("downloaded manifest does not match the digest the job reported")
        recorded = json.loads(manifest_local.read_text(encoding="utf-8")).get("files", {})
        if not isinstance(recorded, Mapping) or not recorded:
            raise MachineError("remote job reported no output files")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        for relative, entry in sorted(recorded.items()):
            parts = Path(relative).parts
            if (not isinstance(entry, Mapping) or Path(relative).is_absolute() or not parts
                    or ".." in parts or relative != Path(relative).as_posix()):
                raise DigestMismatch(f"pod manifest names an unsafe path: {relative!r}")
            local = destination / relative
            local.parent.mkdir(parents=True, exist_ok=True)
            ops.get(f"{remote_dir}/{relative}", local)
            if sha256_file(local) != entry.get("sha256") or local.stat().st_size != entry.get("size"):
                local.unlink(missing_ok=True)
                raise DigestMismatch(f"downloaded {relative} does not match the pod manifest")
            files[relative] = str(local)
        return files

    @staticmethod
    def _save_installation(owner, lease, preparation, installed, *, job=None):
        """Save an observed installation only while the exact invocation still owns it."""
        with owner.lock:
            observed = _installation_claim(owner, lease)
            reason = None
            if observed["lease"].get("preparation") != preparation:
                reason = "checkpoint_installation_changed"
            elif job is not None and observed["lease"].get("job") != job:
                reason = "checkpoint_job_changed"
            elif owner.clock() >= observed["lease"]["paid_expires_at"]:
                reason = "checkpoint_paid_expired"
            elif observed.get("pod_id") != lease.claim.pod_id or owner.closed:
                reason = "checkpoint_custody_changed"
            if reason is not None:
                raise checkpoint_refusal(CustodyChanged("retained installation changed during verification"), reason)
            owner._save(observed, {**observed["lease"],
                                  "preparation": {**preparation, **installed}})

    def _checkpoint_installed(self, run: MachineRun, files: Mapping[str, str]) -> None:
        """Retain only the canonical receipt attested by this successful job.

        Managed alignment legitimately refreshes launch/health receipt bytes.
        Revalidate them before the reset removes the producer evidence; the next
        claim still compares the complete snapshot, including its receipt hash.
        """
        from astrid.core.generation.engine_contracts import EngineError

        from .worker_owner import RunPodWorkerOwnerError

        adapter = self._require()
        owner, placement, state = adapter.lease_owner, adapter.placement, run.lease.state
        with owner.lock:
            current = _installation_claim(owner, run.lease)
            preparation = current["lease"]["preparation"]
            previous = dict(preparation["installed"])
            if previous != state.installed or preparation["state"] != "prepared":
                raise checkpoint_refusal(MachineError("retained installation checkpoint changed during generation"),
                                         "checkpoint_installation_changed")
            if owner.closed or current.get("pod_id") != run.lease.claim.pod_id:
                raise checkpoint_refusal(CustodyChanged("retained installation custody changed"),
                                         "checkpoint_custody_changed")
            job = {"id": run.job_id, "state": "exited"}
            if current["lease"].get("job") != job:
                raise checkpoint_refusal(CustodyChanged("retained installation checkpoint job changed during generation"),
                                         "checkpoint_job_changed")
            # Sample monotonic time first so translating the paid wall-clock
            # expiry cannot grant observation any extra time.
            started = time.monotonic()
            remaining = current["lease"]["paid_expires_at"] - owner.clock()
            deadline = started + min(120, remaining)
            try:
                observer_key, abort_event = owner.begin_failure_observation(run.lease.lease, run.lease.claim.pod_id)
            except RunPodWorkerOwnerError as registration_error:
                # Registration uses the same exact lease fence. Explain an
                # expiry that races the entry checks through that same path.
                _installation_claim(owner, run.lease)
                raise checkpoint_refusal(registration_error, "checkpoint_custody_changed")
        original_ops = run.lease.claim.ops
        try:
            bounded = getattr(original_ops, "bounded", None)
            if bounded is None:
                raise checkpoint_refusal(MachineError("checkpoint transport cannot be bounded"),
                                         "checkpoint_observer_unavailable")
            transport_deadline = getattr(original_ops, "deadline", None)
            if transport_deadline is not None:
                deadline = min(deadline, transport_deadline)
            run.lease.claim.ops = bounded(deadline, abort_event=abort_event)
            try:
                verified = placement._canonical_installed(
                    state, verification_seconds=max(0, deadline - time.monotonic()))
            except (RemotePlacementError, EngineError, ValueError, TypeError, KeyError, OSError) as observation_error:
                why = getattr(observation_error, "install_unverified", None)
                if why is None:
                    transport_reason = getattr(observation_error, "reason", None)
                    why = ("checkpoint_observation_interrupted" if abort_event.is_set()
                           else "status_deadline_exhausted" if time.monotonic() >= deadline
                           or getattr(transport_reason, "code", None) == "deadline_exceeded"
                           else "checkpoint_observation_failed")
                raise checkpoint_refusal(observation_error, why)
        finally:
            try:
                unreaped = getattr(run.lease.claim.ops, "unreaped_child", None)
                if unreaped is not None:
                    from .worker_preparation import AcquisitionUnresolvedError
                    owner.fence_unreaped_observer(run.lease.lease, run.lease.claim.pod_id, unreaped)
                    # The owner now holds the handle; failure release must not
                    # discover and transfer it a second time.
                    run.lease.claim.ops = original_ops
                    raise checkpoint_refusal(AcquisitionUnresolvedError("bounded pod observer remains alive"),
                                             "checkpoint_observer_unreaped")
                run.lease.claim.ops = original_ops
            finally:
                owner.end_failure_observation(observer_key)

        if not verified:
            why = state.install_unverified or "unclassified"
            raise checkpoint_refusal(MachineError("completed job has no verified canonical installation"), why)
        expected = previous
        if state.installed != previous:
            try:
                manifest = json.loads(Path(files["manifest.json"]).read_text(encoding="utf-8"))
                payload = manifest["managed_generation_result"]
                report = payload["evidence"]["producer"]["attempt"]["record"]["managed_engine_preparation"]
                installed = report["installation"]
                if (report["schema"] != "astrid.managed-engine-preparation.v1"
                        or any(value.get("task_id") != run.task_id or value.get("attempt_id") != run.attempt_id
                               for value in (payload, report))
                        or not run.task_id or not run.attempt_id
                        or installed["root"] != previous["root"]
                        or "sha256:" + installed["recipe_sha256"] != previous["compatibility"]["recipe_digest"]):
                    raise ValueError
                expected = {**previous, "receipt_sha256": "sha256:" + installed["receipt_sha256"]}
            except (KeyError, TypeError, ValueError, OSError):
                raise checkpoint_refusal(InstallationUnverified("changed installation has no matching completed-job attestation"),
                                         "checkpoint_attestation_missing") from None
            if state.installed != expected:
                raise checkpoint_refusal(InstallationUnverified("live installation differs from completed-job attestation"),
                                         "checkpoint_attestation_mismatch")
        # Recheck the same owner, generation and invocation after observation.
        if time.monotonic() >= deadline:
            raise checkpoint_refusal(MachineError("checkpoint observation deadline expired"),
                                     "status_deadline_exhausted")
        self._save_installation(owner, run.lease, preparation, {"installed": dict(expected)}, job=job)

    # -- release -------------------------------------------------------------

    def release(self, lease: MachineLease, warm: bool, *, reason: str = "task_failed",
                failure: BaseException | None = None, stage: str = "execution") -> None:
        owner = self._require().lease_owner
        _stamp(getattr(lease, "timing", None), "release_started_at", first=True)
        observer_key = abort_event = None
        if failure is not None and not lease.released:
            try:
                observer_key, abort_event = owner.begin_failure_observation(lease.lease, lease.claim.pod_id)
            except Exception:
                pass  # _release_impl still fences unsafe custody and reconciles exact release.
        try:
            self._release_impl(lease, warm, reason=reason, failure=failure, stage=stage,
                               abort_event=abort_event, observer_key=observer_key)
        finally:
            _stamp(getattr(lease, "timing", None), "released_at")
            if observer_key is not None:
                owner.end_failure_observation(observer_key)

    def _release_impl(self, lease: MachineLease, warm: bool, *, reason: str,
                      failure: BaseException | None, stage: str,
                      abort_event: Any = None, observer_key: Any = None) -> None:
        """Reset a verified job's Runtime, then settle idle; release unsafe custody."""
        from .worker_preparation import AcquisitionUnresolvedError

        adapter = self._require()
        if lease.released:
            return
        owner = adapter.lease_owner
        original_ops = lease.claim.ops
        paid_expiry = None
        verify_deadline = None
        if failure is not None:
            try:
                with owner.lock:
                    initial = owner.preparation_claim(lease.lease)
                    paid_expiry = initial["lease"]["paid_expires_at"]
                remaining = max(0, paid_expiry - owner.clock())
                bounded = getattr(lease.claim.ops, "bounded", None)
                if remaining > 0 and bounded is not None:
                    # The first cancellation is part of the same one-shot
                    # observation budget as status, receipt and reset.
                    verify_deadline = time.monotonic() + min(FAILURE_VERIFY_SECONDS, remaining)
                    lease.claim.ops = bounded(verify_deadline, abort_event=abort_event)
            except Exception:
                pass  # Exact release below reconciles any unresolved job.
        # Poll/transport failures can leave a job running. Stop that exact
        # checkpointed job before deciding whether warm reuse is possible.
        try:
            with owner.lock:
                current = owner.preparation_claim(lease.lease)
                pending = current["lease"].get("job")
            if (pending is not None and pending.get("state") in {"starting", "running"}
                    and (failure is None or verify_deadline is not None)):
                stopped = lease.claim.ops.job_cancel(pending["id"])
                if stopped.get("id") == pending["id"] and stopped.get("state") in {"exited", "cancelled"}:
                    self._checkpoint(lease, {"id": pending["id"], "state": stopped["state"]})
        except Exception:
            pass  # Unknown stop is classified unsafe below; exact release reconciles it.
        # Snapshot custody before remote verification, then fence it again before saving.
        rejection = None
        stale = False
        with owner.lock:
            try:
                record = owner.preparation_claim(lease.lease)
                job = record["lease"].get("job")
                preparation = record["lease"].get("preparation") or {}
                expected_install = dict(preparation.get("installed") or {})
                paid_expiry = record["lease"]["paid_expires_at"]
                if (owner.closed or record.get("pod_id") != lease.claim.pod_id
                        or preparation.get("state") != "prepared" or not expected_install
                        or lease.state.installed != expected_install):
                    rejection = "custody_changed"
                elif job is not None and job.get("state") not in {"exited", "cancelled"}:
                    rejection = "job_stop_unverified"
            except Exception:
                rejection = "custody_changed"
                # preparation_claim also refuses an expired lease. Distinguish
                # that case only when the exact invocation still owns the pod.
                try:
                    record = owner._read()
                    info = record["lease"]
                    invocation = lease.lease
                    if (failure is not None and not owner.closed and record["state"] == "claimed"
                            and record.get("pod_id") == lease.claim.pod_id
                            and record["operation_id"] == invocation.operation_id
                            and info.get("state") == "active"
                            and info.get("owner_epoch") == invocation.owner_epoch == owner.epoch
                            and info.get("generation") == invocation.generation
                            and info.get("invocation_id") == invocation.invocation_id
                            and owner.clock() >= info["paid_expires_at"]):
                        paid_expiry = info["paid_expires_at"]
                        rejection = "paid_limit_exhausted"
                except Exception:  # noqa: BLE001 (unknown custody remains unsafe)
                    pass
        if getattr(lease, "unsafe_job_stop", False):
            rejection = "job_stop_unverified"
        if isinstance(failure, CustodyChanged):
            rejection = "custody_changed"
        elif isinstance(failure, DigestMismatch):
            rejection = "output_digest_mismatch"
        elif isinstance(failure, InstallationUnverified):
            rejection = "installation_unverified"
        if failure is not None and owner.clock() >= (paid_expiry or 0):
            rejection = rejection or "paid_limit_exhausted"
        elif failure is not None and verify_deadline is None:
            rejection = rejection or "engine_unreachable"
        if rejection is None and failure is not None and job is not None:
            assert verify_deadline is not None
            try:
                remaining = min(paid_expiry - owner.clock(), verify_deadline - time.monotonic())
                if remaining <= 0:
                    if owner.clock() >= paid_expiry:
                        rejection = "paid_limit_exhausted"
                    else:
                        self._save_installation(owner, lease, preparation, {"state": "stale_after_failure"}, job=job)
                        reason = "verification_timeout"
                else:
                    try:
                        verified = adapter.placement._canonical_installed(
                            lease.state, verification_seconds=remaining)
                    except Exception as exc:
                        from .remote_placement import InstallationReceiptChanged
                        if isinstance(exc, InstallationReceiptChanged):
                            verified = False
                        else:
                            raise
                    if owner.clock() >= paid_expiry:
                        rejection = "paid_limit_exhausted"
                    elif time.monotonic() >= verify_deadline:
                        self._save_installation(owner, lease, preparation, {"state": "stale_after_failure"}, job=job)
                        reason = "verification_timeout"
                    elif not verified:
                        why = getattr(lease.state, "install_unverified", None) or ""
                        if why.startswith("status_exit_") or why in {"status_no_receipt", "status_deadline_exhausted"}:
                            rejection = "engine_unreachable"
                        else:
                            stale = True
                    elif lease.state.installed is None:
                        rejection = "installation_unverified"
                    elif {**lease.state.installed, "receipt_sha256": expected_install.get("receipt_sha256")} != expected_install:
                        rejection = "installation_unverified"
                    else:
                        attested = getattr(lease, "failure_install_receipt", None)
                        if attested is not None and attested != lease.state.installed.get("receipt_sha256"):
                            rejection = "installation_unverified"
                        else:
                            self._save_installation(owner, lease, preparation,
                                                    {"installed": dict(lease.state.installed)}, job=job)
            except CustodyChanged:
                rejection = "custody_changed"
            except Exception:
                if owner.clock() >= paid_expiry:
                    rejection = "paid_limit_exhausted"
                elif time.monotonic() >= verify_deadline:
                    self._save_installation(owner, lease, preparation, {"state": "stale_after_failure"}, job=job)
                    reason = "verification_timeout"
                else:
                    rejection = "engine_unreachable"
        if rejection is not None:
            warm, reason = False, rejection
        elif failure is not None:
            warm = True
        if warm and job is not None and reason != "verification_timeout":
            try:
                self._reset(lease, verify_deadline=verify_deadline, paid_expiry=paid_expiry)
                if (verify_deadline is not None and time.monotonic() >= verify_deadline) or owner.clock() >= paid_expiry:
                    raise MachineError("failure reset exceeded the original paid limit")
            except Exception as exc:  # noqa: BLE001 (any failed reset releases the pod)
                lease.state.note("reset", "failed", error=type(exc).__name__)
                warm, reason = False, "reset_failed"
        if warm and stale:
            self._save_installation(owner, lease, preparation, {"state": "stale_after_failure"})
        unreaped = getattr(lease.claim.ops, "unreaped_child", None)
        if unreaped is not None:
            owner.fence_unreaped_observer(lease.lease, lease.claim.pod_id, unreaped)
            lease.claim.ops = original_ops
            raise AcquisitionUnresolvedError("bounded pod observer remains alive; exact custody is pending")
        from astrid.core.execution.machine_run import MachineRunCancelled

        from .failure_diagnostics import host_summary
        summary = host_summary(failure, getattr(lease, "failure_diagnostic", None),
                               stage=stage, node_ids=lease.node_ids,
                               node_names=lease.node_names) if failure else None
        outcome = ("cancelled" if isinstance(failure, MachineRunCancelled) else
                   "failed" if failure else "success")
        if not warm and summary is not None:
            try:
                owner.note_failed_job(lease.lease, summary, outcome=outcome, node_ids=lease.node_ids,
                                      node_names=lease.node_names)
            except Exception:
                pass  # Unsafe release still has priority over a diagnostic read/write.
        self._finish(lease.lease)
        if warm:
            try:
                owner.settle(lease.lease, outcome=outcome, failure=summary, node_ids=lease.node_ids,
                             node_names=lease.node_names)
            except Exception:
                if observer_key is not None:
                    owner.end_failure_observation(observer_key)
                owner.release(reason="settlement_unverified", _expected_operation=lease.lease.operation_id,
                              expected_machine_id=lease.claim.pod_id)
                raise
            lease.released = True
            lease.kept, lease.release_reason = True, reason if reason == "verification_timeout" else None
            record = owner._read()
            info = record["lease"]
            lease.expires_at = info.get("idle_expires_at")
            lease.cost_usd = record.get("estimated_cost_usd", 0) + max(0, owner.clock() - info.get("accounted_at", info["started_at"])) * info.get("hourly_usd", 0) / 3600
            lease.preparation_state = (info.get("preparation") or {}).get("state")
            return
        try:
            if observer_key is not None:
                owner.end_failure_observation(observer_key)
            lease.kept, lease.release_reason = False, reason
            owner.release(reason=reason, _expected_operation=lease.lease.operation_id,
                          expected_machine_id=lease.claim.pod_id)
        except AcquisitionUnresolvedError:
            # A deadline or explicit release may already own the one terminal
            # transition while it waits for this exact drain.
            with owner.lock:
                record = owner._read()
                if not (owner._release_inflight and record is not None
                        and record.get("operation_id") == lease.lease.operation_id
                        and record.get("lease", {}).get("state") == "releasing"):
                    raise
        lease.released = True

    def _reset(self, lease: MachineLease, *, verify_deadline: float | None = None,
               paid_expiry: float | None = None) -> dict[str, Any]:
        """The detached in-place reset of the pod's one Runtime, checkpointed like a job."""
        import time

        ops = lease.claim.ops
        if (verify_deadline is not None and time.monotonic() >= verify_deadline) or (
                paid_expiry is not None and self._require().lease_owner.clock() >= paid_expiry):
            raise MachineError("failure verification exceeded the original paid limit")
        job_id = uuid.uuid4().hex
        self._checkpoint(lease, {"id": job_id, "state": "starting"})
        started = ops.job_start(reset_command(run_dir=lease.run_dir, job_dir=lease.job_dir), job_id=job_id)
        if started.get("id") != job_id:
            raise MachineError("reset job differs from the checkpointed job custody")
        self._checkpoint(lease, {"id": job_id, "state": "running"})
        limit = time.monotonic() + RESET_SECONDS
        poll = min(POLL_SECONDS, float(os.environ.get("ASTRID_MACHINE_POLL_SECONDS") or 1.0))
        while True:
            if (verify_deadline is not None and time.monotonic() >= verify_deadline) or (
                    paid_expiry is not None and self._require().lease_owner.clock() >= paid_expiry):
                raise MachineError("failure reset exceeded the original paid limit")
            record = ops.job_poll(job_id)
            if record.get("id") != job_id:
                raise MachineError("reset poll differs from the checkpointed job custody")
            if record.get("state") == "exited":
                break
            if record.get("state") in {"missing", "cancelled"}:
                raise MachineError(f"reset job is {record.get('state')}")
            if time.monotonic() > limit:
                stopped = ops.job_cancel(job_id)
                if stopped.get("id") != job_id or stopped.get("state") not in {"exited", "cancelled"}:
                    raise MachineError("exact reset job stop could not be verified")
                self._checkpoint(lease, {"id": job_id, "state": stopped["state"]})
                raise MachineError(f"reset did not finish in {RESET_SECONDS:.0f}s")
            sleep_for = poll
            if verify_deadline is not None:
                sleep_for = min(sleep_for, max(0, verify_deadline - time.monotonic()))
            if paid_expiry is not None:
                sleep_for = min(sleep_for, max(0, paid_expiry - self._require().lease_owner.clock()))
            if sleep_for <= 0:
                raise MachineError("failure reset exceeded the original paid limit")
            time.sleep(sleep_for)
        self._checkpoint(lease, {"id": job_id, "state": "exited"})
        directory = str(started.get("dir") or "")
        stdout = ""
        if directory:
            _c, stdout, _e = ops.exec(f"cat {shlex.quote(directory)}/stdout", 120)
        result = parse_reset(stdout)
        exit_code = record.get("exit_code")
        if type(exit_code) is not int or exit_code != 0 or result.get("reset") != "ok":
            from .pod_reset import reset_failure_reason

            safe_code = exit_code if type(exit_code) is int and 0 <= exit_code <= 255 else "unknown"
            raise MachineError(f"pod reset failed (exit {safe_code}): {reset_failure_reason(result)}")
        if directory:
            ops.exec(f"rm -rf {shlex.quote(directory)}", 120)  # the agent keeps at most 128 job folders
        lease.state.note("reset", "done", removed=result.get("removed"))
        return result

    # -- helpers -------------------------------------------------------------

    def _finish(self, lease: Any) -> None:
        if self._done is not None:
            self._done(lease)

    def _checkpoint(self, lease: MachineLease, job: Mapping[str, Any]) -> None:
        owner = self._require().lease_owner
        with owner.lock:
            record = owner.preparation_claim(lease.lease)
            owner._save(record, {**record["lease"], "job": dict(job)})


def _agent_packs() -> Path:
    import astrid

    return (Path(astrid.__file__).resolve().parent / "packs")


def _active_sources() -> tuple[Any, ...]:
    try:
        from astrid.core.pack.source_setup import active_sources
        return tuple(active_sources(verify=False))
    except Exception:
        return ()


def _active_roots() -> list[Path]:
    return [Path(source.pack_root).resolve() for source in _active_sources()]


def _active_pack_ids() -> set[str]:
    return {source.pack_id for source in _active_sources()}


def unshipped_entry_reason(entry: str | None, *, registry: Any = None,
                           action_rows: Any = None) -> str | None:
    """Why ``entry`` cannot run on a rented machine, or None when its code ships.

    The pod receives the pinned source tuple (``remote_placement.SOURCE_DIRS``: the
    Agent with its built-in packs, the Runtime, runpod-lifecycle, VibeComfy with its
    ready templates) plus the job directory, which carries the active pack selection
    (Plan 06 ``active_sources``) and the user entry folder (``build_selection``). An
    entry from a pack outside that selection (user or project scope, ``--pack-root``,
    ``ASTRID_PACKS_PATH``) is not shipped and is refused before the claim.
    """
    if not entry:
        return None  # a bundle-only job: the bundle ships in the job directory
    packs = _agent_packs()
    if registry is None:
        try:
            from astrid.core.extensions.registry import ExtensionRegistry
            registry = ExtensionRegistry().load()
        except Exception:
            registry = None
    if registry is not None:
        from astrid.core.generation.workflows import POINT as WORKFLOWS
        for contribution in registry.contributions(WORKFLOWS):
            ids = {row.get("id") for row in (contribution.fields.get("entries") or []) if isinstance(row, Mapping)}
            if entry not in ids:
                continue
            path = Path(contribution.path).resolve()
            shipped = ((contribution.scope == "built-in" and path.is_relative_to(packs))
                       or any(path.is_relative_to(root) for root in _active_roots()))
            if not shipped:
                return (f"entry {entry!r} comes from {contribution.id} ({contribution.scope} pack at {path.parent}), "
                        "which is outside the active pack selection shipped to a rented machine; install it "
                        "with astrid packs (an active source), or run it locally")
            return None
    if action_rows is None:
        try:
            from astrid.core.generation.entries import action_entries
            action_rows = action_entries()
        except Exception:
            action_rows = []
    for row in action_rows:
        if row.get("id") == entry and row.get("action"):
            pack = str(row["action"]).split(".", 1)[0]
            if not (packs / pack / "pack.yaml").is_file() and pack not in _active_pack_ids():
                return (f"entry {entry!r} is an action of pack {pack!r}, which is outside the active pack "
                        "selection shipped to a rented machine; run it locally")
            return None
    return None


def lease_rows(owner: Any) -> list[dict[str, Any]]:
    """This account's lease (live or most recently ended), as ``astrid machines`` lists it."""
    record = owner.selection._read() if hasattr(owner, "selection") else owner
    return [lease_row(record)] if isinstance(record, Mapping) and record.get("pod_id") else []


def lease_row(record: Mapping[str, Any]) -> dict[str, Any]:
    from .failure_diagnostics import safe_stored_summary, stored_names, stored_node_ids
    lease = record.get("lease") or {}
    last = lease.get("last_job")
    last = last if isinstance(last, Mapping) else {}
    outcome = last.get("outcome") if last.get("outcome") in {"success", "failed", "cancelled"} else None
    safe_last = ({"outcome": outcome, "generation": last.get("generation") if type(last.get("generation")) is int else None,
                  "failure": safe_stored_summary(last.get("failure"),
                                                 node_ids=stored_node_ids(last.get("structural_node_ids")),
                                                 node_names=stored_names(last.get("structural_names")))
                  if outcome != "success" else None}
                 if outcome else None)
    # Live means custody still needs release, not that the pod is ready for reuse.
    # Only an exact cleanup receipt ends custody; failed preparation may still bill.
    live = bool(record.get("pod_id")) and record.get("state") != "cleanup_complete"
    if not live:
        state = "ended"
    elif lease.get("state") == "releasing":
        state = "releasing"
    elif record.get("state") == "claimed" and lease.get("state") in {"active", "idle", "preparing"}:
        state = lease["state"]
    else:
        state = "unresolved"
    now = time.time()
    hourly_usd = lease.get("hourly_usd")
    cost_usd = float(record.get("estimated_cost_usd", lease.get("estimated_cost_usd")) or 0)
    release_at = None
    minutes_left = None
    if live:
        release_at = (lease.get("idle_expires_at") if state == "idle"
                      else lease.get("paid_expires_at"))
        accounted_at = lease.get("accounted_at", lease.get("started_at"))
        if isinstance(hourly_usd, (int, float)) and isinstance(accounted_at, (int, float)):
            cost_usd += max(0, now - accounted_at) * hourly_usd / 3600
        if isinstance(release_at, (int, float)):
            minutes_left = max(0.0, round((release_at - now) / 60, 1))
    return {"id": record.get("pod_id"), "kind": KIND, "lease": record.get("operation_id"),
            "gpu": record.get("gpu_type"), "state": state,
            "live": live, "idle_timeout_s": lease.get("idle_seconds"),
            "idle_expires_at": lease.get("idle_expires_at") if state == "idle" else None,
            "release_at": release_at, "minutes_left": minutes_left,
            "cost_usd": round(cost_usd, 4), "hourly_usd": hourly_usd,
            "preparation_state": (lease.get("preparation") or {}).get("state"),
            "release_reason": lease.get("release_reason") if state == "releasing" else None,
            "ended": None if live else (lease.get("ended") or lease.get("release_reason")),
            "last_job": safe_last,
            "self_stop": bool((record.get("self_stop") or {}).get("acknowledged")) if live else None}


def journal_leases(account: str | None = None) -> list[dict[str, Any]]:
    """Read-only listing from the account's lease journal (no lock, no provider call)."""
    from .target_selection import canonical_claim_path, read_runpod_preferences
    from .worker_owner import ended_leases_path

    try:
        account = account or read_runpod_preferences().provider_account_ref
        path = canonical_claim_path(account)
        record = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        ended_path = ended_leases_path(path)
        ended = json.loads(ended_path.read_text(encoding="utf-8")) if ended_path.is_file() else []
    except Exception:  # no preferences or no journal: no leases
        return []
    current = [lease_row(record)] if isinstance(record, Mapping) and record.get("pod_id") else []
    seen = {row["lease"] for row in current}
    # The live (or latest) lease first, then recently ended ones, newest first.
    return with_failure_details(
        current + [row for row in reversed(ended) if isinstance(row, dict) and row.get("lease") not in seen])


def with_failure_details(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the local failure receipt's reason and log folder to each failed last job."""
    from .failure_receipt import kept_failure_details

    for row in rows:
        last = row.get("last_job")
        if isinstance(last, dict) and last.get("outcome") == "failed" and isinstance(last.get("failure"), dict):
            last["failure"] = {**last["failure"], **kept_failure_details(row.get("lease"), last.get("generation"))}
    return rows


def observe_mounts_with(api_key: str) -> Callable[[str, str], Mapping[str, Any]]:
    """The typed provider mapping, from runpod-lifecycle's exact-pod observation."""
    def observe(pod_id: str, nonce: str) -> Mapping[str, Any]:
        from runpod_lifecycle.discovery import observe_pod_mounts

        from .worker_owner import _run_sync

        return _run_sync(observe_pod_mounts(pod_id, api_key, nonce))
    return observe


#: The registered ``generation.machine`` contribution (no host owner until ``for_host``).
MACHINE = RunPodMachine()

__all__ = ["MACHINE", "journal_leases", "with_failure_details", "unshipped_entry_reason", "lease_row", "lease_rows", "CustodyChanged", "MachineCancelled", "MachineError", "MachineLease", "MachineRun",
           "RunPodMachine", "custody_differences", "generation_argv", "machine_custody", "pin_storage",
           "ensure_pod_ready", "generation_command", "pod_state_command", "reset_command"]
