"""Read-only dev identity and clean, owner-bound source promotion."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

PROFILE_NAME = "astrid"
TERMINAL_TASK_STATES = frozenset({"completed", "succeeded", "cancelled", "canceled", "expired", "failed"})
GIT_TIMEOUT_SECONDS = 30.0
READY_WAIT_SECONDS = 60.0
READY_POLL_SECONDS = 0.5
NPM_CI_TIMEOUT_SECONDS = 1800.0
NODE_MODULES_STAMP = ".astrid-lockfile.sha256"
REMOTION_CHROME_KEEP = ".astrid-remotion-chrome-keep"


class DevOperatorError(RuntimeError):
    """A refused or failed operator action, with the one next step to take."""

    def __init__(self, message: str, *, next_action: str | None = None, state: str = "refused") -> None:
        super().__init__(message)
        self.next_action = next_action
        self.state = state


def dev_checkout() -> Path:
    """The checkout this client's ``astrid`` package was imported from (the dev tree)."""
    import astrid

    return Path(astrid.__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DevOperatorError(f"git {' '.join(args)} failed in {repo}: {exc}", state="error") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-300:]
        raise DevOperatorError(f"git {' '.join(args)} failed in {repo}: {detail}", state="error")
    return proc.stdout.strip()


def _git_branch(repo: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:  # detached HEAD: symbolic-ref exits 1
        return None
    return proc.stdout.strip() or None


def _git_state(repo: Path) -> dict[str, Any]:
    status = _git(repo, "status", "--porcelain")
    return {
        "checkout": str(repo),
        "head": _git(repo, "rev-parse", "HEAD"),
        "branch": _git_branch(repo),
        "dirty": bool(status),
        "dirty_paths": len(status.splitlines()) if status else 0,
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def served_profile(data_root: Path) -> dict[str, Any]:
    """The source profile the runtime boots the pack host from."""
    path = Path(data_root) / "runtime" / "source-profiles" / f"{PROFILE_NAME}.json"
    value = _read_json(path)
    if value is None or not value.get("source_checkout"):
        raise DevOperatorError(
            f"no readable source profile at {path}",
            next_action="banodoco-local up --profile astrid --source-manifest <profile.json>",
            state="unavailable",
        )
    return {
        "profile_path": str(path),
        "source_checkout": str(Path(str(value["source_checkout"])).expanduser().resolve()),
        "runtime_checkout": value.get("runtime_checkout"),
    }


def host_state(runtime_dir: Path) -> dict[str, Any]:
    """The pack host recorded by the runtime, with pid identity and readiness."""
    from astrid.sdk.host_bootstrap import _host_identity_matches, _host_pid_alive

    state = _read_json(runtime_dir / "generic-host.json")
    ready = _read_json(runtime_dir / "generic-host.ready.json")
    if state is None:
        return {"recorded": False, "pid": None, "alive": False, "identity_verified": False, "ready": False,
                "ready_status": None, "source_checkout": None, "source_checkout_digest": None,
                "source_closure_digest": None}
    pid = state.get("pid")
    alive = bool(_host_pid_alive(pid))
    verified = bool(alive and _host_identity_matches(state))
    ready_ok = bool(verified and ready and ready.get("status") == "ready"
                    and str(ready.get("pid")) == str(pid)
                    and ready.get("process_birth_id") == state.get("process_birth_id")
                    and ready.get("source_closure_digest") == state.get("source_closure_digest")
                    and ready.get("runtime_instance_id") == state.get("runtime_instance_id"))
    return {
        "recorded": True,
        "pid": pid,
        "alive": alive,
        "identity_verified": verified,
        "ready": ready_ok,
        "ready_status": (ready or {}).get("status"),
        "ready_file": state.get("ready_file"),
        "source_checkout": state.get("source_checkout"),
        "source_checkout_digest": state.get("source_checkout_digest"),
        "source_closure_digest": state.get("source_closure_digest"),
    }


def _default_digests() -> tuple[Callable[[Path], str], Callable[[Path], str]]:
    from astrid.core.execution.generic_host import (
        source_checkout_closure_digest,
        source_checkout_digest,
    )

    return source_checkout_digest, source_checkout_closure_digest


def pair_report(
    *,
    served: Path,
    client: Path,
    host: Mapping[str, Any],
    digest_fn: Callable[[Path], str] | None = None,
    closure_fn: Callable[[Path], str] | None = None,
) -> dict[str, Any]:
    """Compare this client's code with the served tree and with the host's attestation.

    ``paired`` only when the client's tree and the served tree have the same pack
    digest and closure digest, and the running host attested that same served
    digest. Anything else is ``drift``; a tree that cannot be measured is ``unknown``.
    """
    default_digest, default_closure = _default_digests()
    digest_fn = digest_fn or default_digest
    closure_fn = closure_fn or default_closure
    try:
        served_digest, served_closure = digest_fn(served), closure_fn(served)
        client_digest, client_closure = digest_fn(client), closure_fn(client)
    except (OSError, ValueError) as exc:
        return {"status": "unknown", "reason": f"cannot measure a checkout: {exc}",
                "client_checkout": str(client), "served_checkout": str(served)}
    same_tree = client_digest == served_digest and client_closure == served_closure
    host_attested = host.get("source_checkout_digest")
    host_matches = host_attested == served_digest if host.get("recorded") else None
    if same_tree and host_matches is not False:
        status, reason = "paired", "client code matches the served tree"
        if host_matches is None:
            reason += "; no host recorded yet"
    elif not same_tree:
        status = "drift"
        reason = ("dev tree differs from the served tree (uncommitted or unpromoted work); "
                  "commit, then `python -m astrid dev promote`")
    else:
        status = "drift"
        reason = "the running host attested a different served tree than the one now on disk; restart it"
    return {
        "status": status,
        "reason": reason,
        "client_checkout": str(client),
        "served_checkout": str(served),
        "client_digest": client_digest,
        "served_digest": served_digest,
        "client_closure_digest": client_closure,
        "served_closure_digest": served_closure,
        "host_attested_digest": host_attested,
        "host_matches_served": host_matches,
    }


def run_status(*, data_root: Path, client: Path | None = None,
               digest_fn: Callable[[Path], str] | None = None,
               closure_fn: Callable[[Path], str] | None = None) -> dict[str, Any]:
    """Read-only report: served checkout, dev tree, host state, and the pair check."""
    client = Path(client).resolve() if client is not None else dev_checkout()
    profile = served_profile(data_root)
    served = Path(profile["source_checkout"])
    dev = _git_state(client)
    served_state = _git_state(served) if served.is_dir() else None
    host = host_state(Path(data_root) / "runtime")
    pair = pair_report(served=served, client=client, host=host, digest_fn=digest_fn, closure_fn=closure_fn)
    same_head = bool(served_state and served_state["head"] == dev["head"])
    return {
        "ok": True,
        "state": "reported",
        "profile": profile,
        "served": served_state,
        "dev": dev,
        "dev_matches_served_head": same_head,
        "host": host,
        "pair": pair,
    }


def _flatten_rows(value: Any) -> list[dict[str, Any]]:
    """Collect dict rows from the nested ``data`` lists a Runtime listing returns."""
    if isinstance(value, Mapping):
        return [dict(value)] if ("state" in value or "task_id" in value or "slug" in value or "project_id" in value) else []
    if isinstance(value, (list, tuple)):
        rows: list[dict[str, Any]] = []
        for item in value:
            rows.extend(_flatten_rows(item))
        return rows
    return []


def _listing_data(result: Any) -> Any:
    if isinstance(result, Mapping):
        if result.get("ok") is False:
            raise DevOperatorError(f"runtime listing failed: {result.get('error')}", state="unavailable")
        return result.get("data")
    if getattr(result, "ok", True) is False:
        raise DevOperatorError(f"runtime listing failed: {getattr(result, 'error', None)}", state="unavailable")
    return getattr(result, "data", result)


def _page_cursor(result: Any) -> str | None:
    """The next-page cursor a Runtime listing carries as a string item in its data."""
    data = _listing_data(result)
    if isinstance(data, (list, tuple)):
        for item in data:
            if isinstance(item, str) and item:
                return item
    return None


def _all_pages(fetch: Callable[[str | None], Any], *, max_pages: int = 1000) -> list[dict[str, Any]]:
    """Follow a Runtime listing to its last page. A listing that stops early can hide live work."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(max_pages):
        result = fetch(cursor)
        rows.extend(_flatten_rows(_listing_data(result)))
        cursor = _page_cursor(result)
        if cursor is None or cursor in seen:
            return rows
        seen.add(cursor)
    raise DevOperatorError("runtime listing did not end; cannot prove the host is idle", state="unavailable")


def in_flight_tasks(client: Any) -> list[str]:
    """Task ids the Runtime reports as not yet terminal, across every project and page."""
    projects = _all_pages(lambda cursor: client.projects.list(cursor=cursor) if cursor else client.projects.list())
    busy: list[str] = []
    for project in projects:
        project_ref = project.get("slug") or project.get("project_id")
        if not project_ref:
            continue
        tasks = _all_pages(
            lambda cursor, ref=str(project_ref): client.tasks.list(ref, cursor=cursor) if cursor
            else client.tasks.list(ref)
        )
        for task in tasks:
            if str(task.get("state", "")).lower() not in TERMINAL_TASK_STATES:
                busy.append(str(task.get("task_id") or task.get("id") or task.get("state")))
    return busy


def _last_error_line(log: Path) -> str | None:
    try:
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - 200_000))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    errors = [line for line in lines if "Error" in line]
    return errors[-1][-400:] if errors else None


def _require_linked_serve_worktree(dev: Path, serve: Path) -> None:
    if serve == dev:
        raise DevOperatorError(
            "the runtime serves the dev tree itself; promote needs a separate serve worktree",
            next_action="create a detached serve worktree from the selected revision, then point the source profile to it",
            state="unavailable",
        )
    common_dev = Path(_git(dev, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    try:
        common_serve = Path(_git(serve, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    except DevOperatorError as exc:
        raise DevOperatorError(f"served checkout {serve} is not a git worktree: {exc}", state="unavailable") from exc
    if common_dev != common_serve:
        raise DevOperatorError(
            f"served checkout {serve} belongs to a different repository than the dev tree {dev}",
            state="unavailable",
        )


def _run_npm_ci(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
                          timeout=NPM_CI_TIMEOUT_SECONDS, check=False)


def _installed_matches_lockfile(project_dir: Path, lock_packages: Any) -> bool:
    """npm's hidden lockfile (node_modules/.package-lock.json) lists what is installed; same packages means in sync."""
    try:
        installed = json.loads((project_dir / "node_modules" / ".package-lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(installed, dict) or not isinstance(installed.get("packages"), dict):
        return False
    if not isinstance(lock_packages, dict):
        return False
    return ({k: v for k, v in installed["packages"].items() if k != ""}
            == {k: v for k, v in lock_packages.items() if k != ""})


@contextmanager
def _keep_remotion_chrome(project_dir: Path) -> Iterator[dict[str, Any]]:
    """Move Remotion's managed Chrome (``node_modules/.remotion``) aside while ``npm ci`` rewrites node_modules, then put it back.

    ``npm ci`` deletes all of node_modules, and the Chrome that captures use lives there. The move is a rename on one
    filesystem, so it costs nothing and downloads nothing. If the directory is absent nothing is created. A leftover
    copy from an interrupted run is put back first when node_modules has no Chrome of its own.
    """
    chrome = project_dir / "node_modules" / ".remotion"
    keep = project_dir / REMOTION_CHROME_KEEP
    if keep.is_dir() and not chrome.exists():
        chrome.parent.mkdir(parents=True, exist_ok=True)
        keep.rename(chrome)
    state: dict[str, Any] = {"found": False, "restored": False}
    if chrome.is_dir() and not keep.exists():
        chrome.rename(keep)
        state["found"] = True
    try:
        yield state
    finally:
        if state["found"] and keep.exists():
            if not chrome.exists():
                chrome.parent.mkdir(parents=True, exist_ok=True)
                keep.rename(chrome)
                state["restored"] = True


def sync_remotion_node_modules(project_dir: Path, *, run: Callable[[list[str], Path, dict[str, str]], subprocess.CompletedProcess] | None = None,
                               resolve_node: Callable[[Path], Any] | None = None) -> dict[str, Any]:
    """Bring ``<project_dir>/node_modules`` in line with ``package-lock.json`` by ``npm ci`` under the pinned Node.

    The stamp file records the lockfile digest that node_modules was last installed from. A tree with
    no stamp counts as in sync when npm's hidden lockfile already lists the same packages.
    """
    project_dir = Path(project_dir)
    lockfile = project_dir / "package-lock.json"
    if not lockfile.is_file():
        return {"synced": False, "reason": "no package-lock.json"}
    raw = lockfile.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    stamp = project_dir / "node_modules" / NODE_MODULES_STAMP
    try:
        before: str | None = stamp.read_text(encoding="utf-8").strip() or None
    except OSError:
        before = None
    if before == digest:
        return {"synced": False, "reason": "in sync", "lockfile": digest[:12]}
    if before is None:
        try:
            lock_packages = json.loads(raw.decode("utf-8")).get("packages")
        except (ValueError, AttributeError):
            lock_packages = None
        if _installed_matches_lockfile(project_dir, lock_packages):
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(digest + "\n", encoding="utf-8")
            return {"synced": False, "reason": "in sync", "lockfile": digest[:12]}

    if resolve_node is None:
        from astrid.core.rendering.node_pin import resolve_pinned_node

        resolve_node = resolve_pinned_node
    resolved = resolve_node(project_dir)
    if not resolved.ok or resolved.path is None:
        raise DevOperatorError(
            f"no pinned Node for {project_dir}, so npm ci cannot run: {resolved.problem or 'no usable Node found'}",
            next_action="python -m astrid doctor --json (it shows where the pinned Node is expected)",
            state="unavailable",
        )
    bin_dir = Path(resolved.path).parent
    npm = bin_dir / "npm"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    started = time.monotonic()
    with _keep_remotion_chrome(project_dir) as chrome:
        try:
            proc = (run or _run_npm_ci)([str(npm), "ci"], project_dir, env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DevOperatorError(
                f"npm ci failed in {project_dir}: {exc}", state="error",
                next_action=f"cd {project_dir} && PATH={bin_dir}:$PATH npm ci",
            ) from exc
        if proc.returncode != 0:
            lines = [line for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
            tail = lines[-1].strip()[-300:] if lines else f"exit {proc.returncode}"
            raise DevOperatorError(
                f"npm ci failed in {project_dir}: {tail}", state="error",
                next_action=f"cd {project_dir} && PATH={bin_dir}:$PATH npm ci  (read the full npm log, fix, then rerun)",
            )
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(digest + "\n", encoding="utf-8")
    if not chrome["found"]:
        chrome_state = "absent"
    elif chrome["restored"]:
        chrome_state = "preserved"
    else:
        chrome_state = f"kept aside at {project_dir / REMOTION_CHROME_KEEP} (node_modules/.remotion was recreated)"
    return {
        "synced": True,
        "command": "npm ci",
        "lockfile_before": before[:12] if before else None,
        "lockfile": digest[:12],
        "seconds": round(time.monotonic() - started, 1),
        "remotion_chrome": chrome_state,
    }


# Chromium and Remotion bundle processes Astrid starts for renders and captures.
# A process is an orphan when its parent is launchd (pid 1): its parent died and
# nothing reaped it. Only these two shapes are candidates, and only when the
# command shows they came from this checkout's remotion tree.
_CHROME_SHELL_MARK = "/.remotion/chrome-headless-shell/"
_BUNDLE_OUT_DIR = re.compile(r"--out-dir (/\S+)")
_TEMP_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
_BUNDLE_TEMP_NAME = re.compile(r"^(bundle-[0-9a-f]{16,64}\.tmp-|astrid-remotion-frame-bundle-)")


def _ps_listing() -> str:
    completed = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True,
                               timeout=30, check=False)
    return completed.stdout or ""


def _parse_ps(listing: str) -> dict[int, tuple[int, str]]:
    rows: dict[int, tuple[int, str]] = {}
    for line in listing.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), parts[2])
    return rows


def find_orphan_render_processes(listing: str, *, checkouts: list[str]) -> list[tuple[int, str]]:
    """Astrid-started Chromium or bundle processes whose parent is pid 1, from ``ps`` text.

    ``checkouts`` are the serve and dev checkout roots. A command must start with
    ``<checkout>/remotion/`` (or contain it, for a node command) to count. Nothing
    else matches: other Chromium installs and user processes are never candidates.
    """
    roots = tuple(f"{str(root).rstrip('/')}/remotion/" for root in checkouts if root)
    found: list[tuple[int, str]] = []
    for pid, (ppid, command) in sorted(_parse_ps(listing).items()):
        if ppid != 1 or pid <= 1:
            continue
        if _CHROME_SHELL_MARK in command and command.startswith(roots):
            found.append((pid, command))
            continue
        out_dir = _BUNDLE_OUT_DIR.search(command)
        executable = command.split(" ", 1)[0]
        if (out_dir and os.path.basename(executable) == "node" and any(root in command for root in roots)
                and out_dir.group(1).startswith(_TEMP_ROOTS)
                and _BUNDLE_TEMP_NAME.match(os.path.basename(out_dir.group(1)))):
            found.append((pid, command))
    return found


def reap_orphan_render_processes(
    *,
    checkouts: list[str],
    list_fn: Callable[[], str] = _ps_listing,
    kill_fn: Callable[[int, int], None] = os.kill,
) -> list[dict[str, Any]]:
    """Kill the orphans ``find_orphan_render_processes`` reports, by exact pid.

    Each pid is checked again right before the signal: it must still be an orphan
    with the same command, so a reused pid is never signalled.
    """
    reaped: list[dict[str, Any]] = []
    for pid, command in find_orphan_render_processes(list_fn(), checkouts=checkouts):
        current = _parse_ps(list_fn()).get(pid)
        if current is None or current != (1, command):
            continue
        try:
            kill_fn(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        reaped.append({"pid": pid, "command": command})
    return reaped


def run_promote(*, data_root: Path, ref: str | None = None, force: bool = False,
                client: Path | None = None, open_client=None, terminate_host=None,
                digest_fn=None, closure_fn=None, ready_wait_seconds: float = READY_WAIT_SECONDS,
                sync_node_modules: Callable[[Path], dict[str, Any]] | None = None,
                reap_orphans: Callable[[list[str]], list[dict[str, Any]]] | None = None):
    """Promote only a clean linked source tree after an owned stop proves absent.

    A new CLI has no retained process graph: it refuses safely and asks the
    resident owner to perform the transition. Marker/PID possession is never
    cleanup authority, and force abandonment is unsupported.

    After the owned stop and the checkout, ``remotion/node_modules`` is brought in line
    with the lockfile (``npm ci`` under the pinned Node, keeping the Remotion Chrome), and
    ``reap_orphans`` (given the serve and dev checkout roots) sweeps orphaned render
    processes before the reopen. None skips the sweep.
    """
    if force:
        raise DevOperatorError("force abandonment is unsupported; wait for owned work to settle")
    from astrid.sdk.host_bootstrap import _terminate_old_host
    from astrid.sdk.client import AstridClient
    terminate_host = terminate_host or _terminate_old_host
    open_client = open_client or AstridClient.open_from_launcher
    default_digest, default_closure = _default_digests()
    digest_fn, closure_fn = digest_fn or default_digest, closure_fn or default_closure
    dev = Path(client).resolve() if client else dev_checkout()
    profile = served_profile(data_root)
    serve = Path(profile["source_checkout"])
    _require_linked_serve_worktree(dev, serve)
    if _git(dev, "status", "--porcelain", "--untracked-files=all") or _git(serve, "status", "--porcelain", "--untracked-files=all"):
        raise DevOperatorError("source or served tree has local changes; promotion preserves dirty work and refuses")
    target = _git(dev, "rev-parse", "--verify", f"{ref or 'HEAD'}^{{commit}}")
    old = _git(serve, "rev-parse", "HEAD")
    runtime_dir = Path(data_root) / "runtime"
    with open_client(start_pack_host=False) as connection:
        if in_flight_tasks(connection):
            raise DevOperatorError("Runtime reports active work; promotion refused", state="busy")
    state_path = runtime_dir / "generic-host.json"
    before = _read_json(state_path)
    if before is None:
        raise DevOperatorError("resident host custody is unavailable; use the supported owner stop/relaunch", state="unavailable")
    with (runtime_dir / "generic-host.lock").open("a+") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if _read_json(state_path) != before or served_profile(data_root) != profile:
            raise DevOperatorError("service/profile identity changed before promotion")
        if _git(serve, "rev-parse", "HEAD") != old or _git(serve, "status", "--porcelain", "--untracked-files=all"):
            raise DevOperatorError("served tree changed before the owned stop")
        # This helper retains the exact graph, stops it and proves it absent.
        # Any uncertainty exits before Git mutates the served source.
        try:
            terminate_host(before)
        except Exception as exc:
            raise DevOperatorError("resident owner could not prove host cleanup; served tree remains unchanged",
                                   next_action="use the supported owner stop/relaunch; never kill a numeric PID") from exc
        if _read_json(state_path) != before or served_profile(data_root) != profile:
            raise DevOperatorError("service/profile changed during owned cleanup; no checkout mutation")
        if _git(serve, "rev-parse", "HEAD") != old or _git(serve, "status", "--porcelain", "--untracked-files=all"):
            raise DevOperatorError("served tree changed during owned cleanup; no checkout mutation")
        _git(serve, "checkout", "--quiet", "--detach", target)
        orphans_reaped: list[dict[str, Any]] = []
        if reap_orphans is not None:
            orphans_reaped = reap_orphans([str(serve), str(dev)])
        try:
            node_modules = (sync_node_modules or sync_remotion_node_modules)(serve / "remotion")
        except DevOperatorError as exc:
            raise DevOperatorError(
                f"{exc}; the serve worktree is already at {target[:12]} and the pack host is stopped",
                next_action=f"{exc.next_action or 'fix the error above'}; then rerun dev promote --ref {target[:12]}",
                state=exc.state,
            ) from exc
    try:
        with open_client(start_pack_host=True):
            pass
    except Exception as exc:
        raise DevOperatorError("source promoted after owned stop, but activation failed; retain state and retry supported relaunch",
                               state="activation_failed") from exc
    after = host_state(runtime_dir)
    deadline = time.monotonic() + min(60.0, max(0.0, ready_wait_seconds))
    while not after["ready"] and time.monotonic() < deadline:
        time.sleep(READY_POLL_SECONDS)
        after = host_state(runtime_dir)
    observed = _read_json(state_path) or {}
    if (not after["ready"] or observed.get("process_birth_id") == before.get("process_birth_id")
            or observed.get("runtime_instance_id") != before.get("runtime_instance_id")
            or after.get("source_checkout_digest") != digest_fn(serve)
            or after.get("source_closure_digest") != closure_fn(serve)):
        raise DevOperatorError("promoted host readiness/source/service identity is unproved", state="activation_failed")
    return {"ok": True, "state": "promoted", "old_sha": old, "new_sha": target,
            "served_checkout": str(serve), "orphans_reaped": orphans_reaped, "node_modules": node_modules,
            "pair": pair_report(served=serve, client=dev, host=after,
                                                               digest_fn=digest_fn, closure_fn=closure_fn)}


def _reclaim_command(parsed: Any, resolve_runtime_data_root: Any) -> int:
    """`astrid dev reclaim`: report and expire registered disposable storage roots."""
    import sys
    from astrid.sdk.storage_ownership import reclaim

    try:
        report = reclaim(Path(resolve_runtime_data_root()), dry_run=parsed.dry_run)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2) if parsed.json else f"Astrid dev reclaim: {exc}", file=sys.stderr)
        return 1
    if parsed.json:
        print(json.dumps({"ok": True, **report}, indent=2, sort_keys=True))
        return 0
    print(f"Astrid dev reclaim ({'dry run' if parsed.dry_run else 'applied'})")
    for root in report["roots"]:
        print(f"{root['name']}: {root['bytes']} bytes, {root['count']} items, {root['expired_count']} expired")
    print(f"reclaimable: {report['reclaimable_bytes']} bytes; reclaimed: {report['reclaimed_bytes']} bytes")
    return 0


def render_human(payload: Mapping[str, Any]) -> list[str]:
    """Plain-text lines for a status or promote result."""
    if "dev" in payload and "pair" in payload and payload.get("state") == "reported":
        served, dev, host, pair = payload["served"] or {}, payload["dev"], payload["host"], payload["pair"]
        return [
            f"served checkout: {payload['profile']['source_checkout']} @ {(served.get('head') or '?')[:12]}",
            f"dev tree: {dev['checkout']} @ {dev['head'][:12]} branch={dev['branch']} dirty={dev['dirty']}",
            f"dev head matches served head: {payload['dev_matches_served_head']}",
            f"pack host: pid={host.get('pid')} alive={host.get('alive')} ready={host.get('ready')}",
            f"pair: {pair['status']} ({pair['reason']})",
        ]
    lines = [f"promoted {payload['old_sha'][:12]} -> {payload['new_sha'][:12]} in {payload['served_checkout']}",
             f"pair: {payload['pair']['status']} ({payload['pair']['reason']})"]
    if payload.get("node_modules") is not None:
        lines.append(_node_modules_line(payload["node_modules"]))
    if payload.get("orphans_reaped"):
        lines.append(f"reaped {len(payload['orphans_reaped'])} orphan render process(es)")
    return lines


def _node_modules_line(nm: Mapping[str, Any]) -> str:
    if nm.get("synced"):
        before = nm.get("lockfile_before") or "none"
        return (f"remotion/node_modules: synced ({nm.get('command', 'npm ci')}; lockfile {before} -> "
                f"{nm['lockfile']}, {round(float(nm.get('seconds', 0)))} s)")
    if nm.get("reason") == "in sync":
        return f"remotion/node_modules: in sync (lockfile {nm['lockfile']})"
    return f"remotion/node_modules: skipped ({nm.get('reason', 'no lockfile')})"


def _dev_cache_prune(parsed: Any) -> int:
    """`astrid dev cache prune --visualize`: reclaim the visualize evidence cache.

    Filesystem-only: it never connects to or starts the runtime.
    """
    import sys
    from astrid.sdk import visualize_cache
    from astrid.sdk.invocation import _visualize_cache_base

    try:
        limit = visualize_cache.max_cache_bytes(override_mb=parsed.keep_mb)
        report = visualize_cache.prune_visualize_cache(
            _visualize_cache_base(),
            max_bytes=limit,
            max_dirs=visualize_cache.DEFAULT_MAX_DIRS,
        )
    except (OSError, ValueError) as exc:
        payload = {"ok": False, "error": str(exc)}
        if parsed.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Astrid dev cache prune: {exc}", file=sys.stderr)
        return 1
    if parsed.json:
        print(json.dumps({"ok": True, **report.as_dict()}, indent=2, sort_keys=True))
    else:
        for line in visualize_cache.format_report(report):
            print(line)
    return 0


def main(args: list[str]) -> int:
    import argparse
    from astrid.sdk.storage_root import resolve_runtime_data_root
    parser = argparse.ArgumentParser(prog="astrid dev", description="Read source/served identity; promote only through retained owner custody.")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status"); status.add_argument("--json", action="store_true")
    promote = sub.add_parser("promote"); promote.add_argument("--ref"); promote.add_argument("--json", action="store_true")
    promote.add_argument("--force", action="store_true", help="rejected: abandonment is unsupported")
    cache_cmd = sub.add_parser("cache", help="reclaim durable evidence caches (timelines visualize)")
    cache_ops = cache_cmd.add_subparsers(dest="cache_operation", required=True)
    cache_prune = cache_ops.add_parser("prune", help="evict least-recently-used visualize captures until within the cache limit")
    cache_prune.add_argument("--visualize", action="store_true", required=True, help="prune the timelines visualize evidence cache")
    cache_prune.add_argument("--keep-mb", type=float, default=None, help="byte cap for this run in MB (default: ASTRID_VISUALIZE_CACHE_MB or 500)")
    cache_prune.add_argument("--json", action="store_true")
    reclaim_cmd = sub.add_parser("reclaim", help="report and expire registered disposable roots")
    reclaim_cmd.add_argument("--dry-run", action="store_true", help="report only; do not delete expired roots")
    reclaim_cmd.add_argument("--json", action="store_true")
    parsed = parser.parse_args(args)
    if parsed.command == "reclaim":
        return _reclaim_command(parsed, resolve_runtime_data_root)
    if parsed.command == "cache":
        return _dev_cache_prune(parsed)
    try:
        payload = run_status(data_root=resolve_runtime_data_root()) if parsed.command == "status" else run_promote(
            data_root=resolve_runtime_data_root(), ref=parsed.ref, force=parsed.force,
            reap_orphans=lambda checkouts: reap_orphan_render_processes(checkouts=checkouts))
    except (DevOperatorError, OSError, ValueError) as exc:
        payload = {"ok": False, "state": getattr(exc, "state", "error"), "error": str(exc),
                   "next_action": getattr(exc, "next_action", None)}
    if parsed.json:
        print(json.dumps(payload, sort_keys=True, indent=2, default=str))
    elif payload.get("ok"):
        for line in render_human(payload):
            print(line)
    else:
        import sys
        print(f"Astrid dev {parsed.command}: {payload['error']}", file=sys.stderr)
        if payload.get("next_action"):
            print(f"next action: {payload['next_action']}", file=sys.stderr)
    return 0 if payload["ok"] else 1
