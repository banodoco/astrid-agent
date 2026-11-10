"""Reset the warm pod's one Runtime in place after each job (Plan 32 T5f).

A warm RunPod machine keeps one long-lived Runtime. After a job's outputs are
fetched (digest-verified) and before the lease settles warm, the controller runs
this module on the pod as a detached job:

1. restore the empty-realm template into a sibling candidate (``POST /v1/restore``),
   then ``POST /v1/replace`` it: the same daemon and realm id, the old root moved
   aside and deleted;
2. remove the job's run directory and its pod-agent job directory (the agent keeps
   at most 128), sweep the pack host's attempt folders and truncate logs;
3. one full ``ensure_runtime`` that writes the prepared connection and ``ready.json``,
   and deletes ``active-job.json``.

The first run on a fresh pod (no template yet) takes the template from the realm
``pod_bootstrap`` just created, so it is empty. The next job's generation command
takes the reset lock, requires ``ready.json`` with no ``active-job.json``, and opens
the prepared connection: no ``up`` and no ``connect`` per job.

Standard library plus the Agent SDK only; it runs under the pod's ``/opt/astrid``.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

POD_ROOT = Path("/root/astrid")
DATA_ROOT = POD_ROOT / "runtime-data"
SOURCE_MANIFEST = POD_ROOT / "source-manifest.json"
TEMPLATE = POD_ROOT / "realm-template"
READY = POD_ROOT / "ready.json"
PREPARED = POD_ROOT / "prepared-connection.json"
ACTIVE_JOB = POD_ROOT / "active-job.json"
LOCK = POD_ROOT / "reset.lock"
RUNS_ROOT = POD_ROOT / "runs"
#: The realm store's job tables; an empty realm (the template's source) has none of these rows.
JOB_TABLES = ("projects", "tasks", "runs", "events", "objects")
PREPARED_ENV = "ASTRID_PREPARED_CONNECTION"


class ResetError(RuntimeError):
    """The reset did not leave an empty, ready Runtime; the controller releases the pod."""

    def __init__(self, *args: object, reason: str = "unknown") -> None:
        super().__init__(*args)
        self.reason = reason


RESET_FAILURE_REASONS = frozenset({
    "prepare", "runtime_ensure", "realm_lookup", "template_restore", "template_create",
    "sweep", "ready_ensure", "ready_write", "unknown",
})


def reset_failure_reason(result: str | Mapping[str, Any]) -> str:
    """Read only the fixed failure protocol; raw pod output is never a diagnostic."""
    if isinstance(result, str):
        try:
            result = json.loads(result.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return "unknown"
    if not isinstance(result, Mapping) or set(result) != {"reset", "reason"} or result.get("reset") != "failed":
        return "unknown"
    reason = result.get("reason")
    return reason if isinstance(reason, str) and reason in RESET_FAILURE_REASONS else "unknown"


@contextlib.contextmanager
def reset_lock(path: Path = LOCK) -> Iterator[None]:
    """Exclusive: a reset never overlaps a job (the generation command holds it shared)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _post(endpoint: str, token: str, path: str, body: Mapping[str, Any], *, timeout: float = 600) -> Any:
    request = urllib.request.Request(
        endpoint.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Bearer {token}", "X-Request-ID": f"request-{uuid.uuid4().hex}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (loopback only)
        return json.loads(response.read() or b"null")


def realm_job_rows(root: Path) -> dict[str, int]:
    """Rows a job leaves in the realm store, plus its CAS files; all zero for an empty realm."""
    import sqlite3

    database = root / "realm.sqlite3"
    if not database.is_file():
        raise ResetError(f"no realm store at {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in JOB_TABLES if table in tables}
    finally:
        connection.close()
    cas = root / "cas" / "sha256"
    rows["cas"] = sum(1 for path in cas.rglob("*") if path.is_file()) if cas.is_dir() else 0
    return rows


def take_template(endpoint: str, token: str, template: Path = TEMPLATE, *, realm_root: Path,
                  active: Path = ACTIVE_JOB) -> Mapping[str, Any]:
    """Back up the empty realm once; later resets restore exactly this.

    Refused when a job may have written the realm: ``active-job.json`` is present,
    or the realm holds any job rows or CAS objects (a dirty template would be
    restored into every later job). The controller then releases the pod.
    """
    if template.exists():
        raise ResetError(f"template already exists: {template}")
    if active.exists():
        raise ResetError("a job was active on this realm; it cannot become the empty template")
    dirty = {name: count for name, count in realm_job_rows(realm_root).items() if count}
    if dirty:
        raise ResetError(f"the realm is not empty; it cannot become the template: {dirty}")
    return _post(endpoint, token, "/v1/backup", {"destination": str(template)})


def pending_dirs(active: Path = ACTIVE_JOB, *, runs_root: Path = RUNS_ROOT) -> tuple[Path | None, Path | None]:
    """The run and pod-agent job folders a job that never reached its reset recorded.

    Only a run folder directly under ``runs_root`` and an agent job folder holding
    its ``command.sh`` are returned; anything else in the file is ignored.
    """
    try:
        value = json.loads(active.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(value, Mapping):
        return None, None

    def folder(key: str) -> Path | None:
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.startswith("/") or ".." in Path(raw).parts:
            return None
        path = Path(raw)
        return None if path.is_symlink() or not path.is_dir() else path

    run_dir, job_dir = folder("run_dir"), folder("job_dir")
    if run_dir is not None and run_dir.parent != runs_root:
        run_dir = None
    if job_dir is not None and not (job_dir / "command.sh").is_file():
        job_dir = None
    return run_dir, job_dir


def runtime_data_root() -> Path:
    """The data root pod_bootstrap uses: ``ASTRID_RUNTIME_DATA_ROOT`` when set, else the pod default."""
    value = os.environ.get("ASTRID_RUNTIME_DATA_ROOT", "").strip()
    return Path(value) if value else DATA_ROOT


def realm_root(realm_id: str, data_root: Path = DATA_ROOT) -> Path:
    """The live realm's root, from the launcher's own catalog (the one source of truth).

    ``pod_bootstrap`` creates the workspace with ``--realm-root /root/astrid/realm``;
    ``astrid-runtime workspace create`` records that root as the catalog realm's
    ``data_root`` in ``<data root>/runtime/catalog.json``. No path is guessed: a
    catalog without this realm, or a root without its store, fails the reset.
    """
    catalog = data_root / "runtime" / "catalog.json"
    try:
        document = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResetError(f"cannot read the launcher catalog {catalog}: {exc}") from None
    realms = document.get("realms") if isinstance(document, Mapping) else None
    entry = next((row for row in realms or [] if isinstance(row, Mapping) and row.get("realm_id") == realm_id), None)
    raw = entry.get("data_root") if entry is not None else None
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise ResetError(f"the launcher catalog has no root for realm {realm_id!r}")
    root = Path(raw)
    if root.is_symlink() or not (root / "realm.sqlite3").is_file():
        raise ResetError(f"realm {realm_id!r} has no store at {root}")
    return root


def replace_realm(endpoint: str, token: str, *, realm_root: Path,
                  template: Path = TEMPLATE) -> Mapping[str, Any]:
    """Restore the template beside the realm and activate it in the same daemon."""
    if not template.is_dir():
        raise ResetError(f"no realm template at {template}")
    candidate = realm_root.parent / f".{realm_root.name}.candidate-{uuid.uuid4().hex}"
    _post(endpoint, token, "/v1/restore", {"backup": str(template), "destination": str(candidate)})
    result = _post(endpoint, token, "/v1/replace", {"candidate": str(candidate), "retain_superseded": False})
    if not isinstance(result, Mapping) or result.get("state") != "complete":
        raise ResetError(f"realm replacement did not complete: {result!r}")
    superseded = result.get("superseded_root")
    if result.get("cleanup_pending") or (superseded and Path(str(superseded)).exists()):
        raise ResetError("the replaced realm root was not removed")
    leftovers = [p for p in realm_root.parent.glob(f".{realm_root.name}.superseded-*")]
    if leftovers:
        raise ResetError(f"superseded realm roots remain: {leftovers}")
    return result


def sweep(*, run_dir: Path | None, job_dir: Path | None, data_root: Path = DATA_ROOT) -> list[str]:
    """Remove the job's folders and attempt scratch; truncate logs. Returns what was removed."""
    removed = []
    for path in (run_dir, job_dir):
        if path is not None and path.exists():
            shutil.rmtree(path, ignore_errors=False)
            removed.append(str(path))
    # The pack host's attempt scratch: ``<support root>/host-output/attempts`` (generic_host).
    folders = {data_root / "runtime" / "attempts", *data_root.glob("**/host-output/attempts")}
    for attempts in sorted(folder for folder in folders if folder.is_dir() and not folder.is_symlink()):
        for child in attempts.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed.append(str(child))
    for log in data_root.glob("**/*.log"):
        if log.is_file() and not log.is_symlink():
            with log.open("r+") as handle:
                handle.truncate(0)
    return removed


def write_prepared(result: Mapping[str, Any], *, prepared: Path = PREPARED, ready: Path = READY,
                   active: Path = ACTIVE_JOB) -> dict[str, Any]:
    """The prepared connection (no secret: a path to the owner-only credential file) and ready.json."""
    connection = {key: result.get(key) for key in ("endpoint", "credential_file", "realm_id", "actor_id")}
    if not all(isinstance(value, str) and value for value in connection.values()):
        raise ResetError("ensure_runtime did not return a complete connection")
    _write_private(prepared, connection)
    marker = {"schema": "astrid.pod-ready.v1", "realm_id": connection["realm_id"], "at": time.time(),
              "prepared": str(prepared)}
    _write_private(ready, marker)
    active.unlink(missing_ok=True)
    return marker


def _write_private(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(dict(value), handle, sort_keys=True)
    os.replace(partial, path)


def open_prepared(path: Path | str | None = None, *, opener: Callable[..., Any] | None = None) -> Any:
    """Open the prepared connection: the warm job's only way in (no up, no connect)."""
    location = Path(path or os.environ.get(PREPARED_ENV) or PREPARED)
    value = json.loads(location.read_text(encoding="utf-8"))
    if opener is None:
        from astrid.sdk.client import AstridClient
        opener = AstridClient.open
    return opener(endpoint=value["endpoint"], credential=Path(value["credential_file"]),
                  realm_id=value["realm_id"], actor_id=value["actor_id"], client_name="astrid",
                  client_version="stage1", protocol_version="workspace.v1")


def _owner_token(data_root: Path) -> str:
    # Backup/restore/replace are admin operations. The launcher's ordinary
    # credential_file is the scoped Astrid connection saved for generation;
    # lifecycle control uses the Runtime's existing owner credential instead.
    from astrid_runtime.paths import RuntimePaths
    from astrid.sdk.workspace_client import _read_credential

    paths = RuntimePaths.current(data_root=data_root)
    token = _read_credential(paths.runtime_support / "credentials" / "owner.token")
    if not token.isascii() or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                                  for character in token):
        raise ResetError("Runtime owner credential is invalid")
    return token


def reset(*, run_dir: Path | None = None, job_dir: Path | None = None,
          ensure: Callable[[], Mapping[str, Any]] | None = None, data_root: Path | None = None,
          template: Path = TEMPLATE, lock: Path = LOCK, prepared: Path = PREPARED, ready: Path = READY,
          active: Path = ACTIVE_JOB, runs_root: Path = RUNS_ROOT) -> dict[str, Any]:
    """The whole pod reset under the exclusive lock; raises ResetError on any failure.

    The realm root comes from the launcher's catalog under ``data_root`` only;
    ``data_root`` defaults to ``ASTRID_RUNTIME_DATA_ROOT`` when set (as pod_bootstrap
    and the launcher read it), else ``/root/astrid/runtime-data``.
    """
    reason = "prepare"
    try:
        data_root = Path(data_root) if data_root is not None else runtime_data_root()
        if ensure is None:
            from astrid.sdk.autobootstrap import ensure_runtime
            ensure = ensure_runtime
        with reset_lock(lock):
            ready.unlink(missing_ok=True)  # a failed reset leaves the pod not ready
            if run_dir is None and job_dir is None:
                run_dir, job_dir = pending_dirs(active, runs_root=runs_root)
            reason = "runtime_ensure"
            current = ensure()
            reason = "realm_lookup"
            root = realm_root(str(current["realm_id"]), data_root)
            steps: dict[str, Any] = {}
            if template.exists():
                reason = "template_restore"
                steps["replace"] = replace_realm(current["endpoint"], _owner_token(data_root), template=template,
                                                 realm_root=root)
            else:
                reason = "template_create"
                steps["template"] = take_template(current["endpoint"], _owner_token(data_root), template,
                                                  realm_root=root, active=active)
            reason = "sweep"
            steps["removed"] = sweep(run_dir=run_dir, job_dir=job_dir, data_root=data_root)
            # Replacement rotates credentials; ensure writes the connection the next job opens.
            reason = "ready_ensure"
            current = ensure()
            reason = "ready_write"
            steps["ready"] = write_prepared(current, prepared=prepared, ready=ready, active=active)
            reason = "prepare"  # releasing the reset lock is also part of preparation
            return steps
    except Exception as exc:
        raise ResetError(str(exc), reason=reason) from exc



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m astrid.packs.runpod.pod_reset")
    parser.add_argument("command", choices=("reset",))
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--job-dir", type=Path)
    args = parser.parse_args(argv)
    os.environ.setdefault("ASTRID_RUNTIME_DATA_ROOT", str(DATA_ROOT))
    os.environ.setdefault("ASTRID_RUNTIME_SOURCE_MANIFEST", str(SOURCE_MANIFEST))
    os.environ.setdefault("ASTRID_RUNTIME_DOOR_PORT", "0")
    try:
        steps = reset(run_dir=args.run_dir, job_dir=args.job_dir)
    except Exception as exc:  # noqa: BLE001 (the controller releases the pod on a non-zero exit)
        reason = reset_failure_reason({"reset": "failed", "reason": getattr(exc, "reason", None)})
        print(json.dumps({"reset": "failed", "reason": reason}))
        return 1
    print(json.dumps({"reset": "ok", "removed": len(steps["removed"]),
                      "template": "taken" if "template" in steps else "restored"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
