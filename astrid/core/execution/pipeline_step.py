"""Shared, custody-preserving execution of pipeline steps."""
from __future__ import annotations

import argparse
import os
import pickle
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from astrid.core.foundation.pack_resources import checkout_root
from astrid.core.audit import PARENT_IDS_ENV, AuditContext
from astrid.core.contracts.errors import AstridError, render_astrid_error
from astrid.core.execution.process_group import group_exists as _owned_group_exists, popen_owned_group, popen_owned_process, observe_tree, release_group as _release_owned_group, signal_group as _signal_owned_group, terminate_tree as _terminate_owned_tree, terminate_group as _terminate_owned_group

Step = Any

def step_output_root(step: Step, args: argparse.Namespace) -> Path:
    return args.brief_out if step.per_brief else args.out


def log_dir_for_step(step: Step, args: argparse.Namespace) -> Path:
    return step_output_root(step, args) / "logs"


def sentinel_paths(step: Step, args: argparse.Namespace) -> list[Path]:
    root = step_output_root(step, args)
    return [root / name for name in step.sentinels]

def print_log_tail(step_name: str, log_path: Path) -> None:
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-40:]
    render_astrid_error(
        AstridError(
            f"{step_name}: failed; last {len(tail)} log lines from {log_path}",
            recovery_command=f"Check the log at {log_path} for details and retry the {step_name} step",
            state_snapshot={"step": step_name, "log_path": str(log_path), "log_tail": tail},
        )
    )


def _signal_process_group(process: subprocess.Popen, sig: int) -> None:
    """Signal the owned child session, using its stable keeper when set."""
    _signal_owned_group(process, sig)


def _process_group_exists(process: subprocess.Popen) -> bool:
    """Return whether any member of the owned session is still alive."""
    return _owned_group_exists(process)


def _terminate_process_group(process: subprocess.Popen, *, grace_seconds: float = 1.0) -> None:
    """Terminate the complete child session and reap its direct child.

    A callback can handle SIGTERM by exiting immediately while one of its
    descendants ignores SIGTERM.  Waiting only for the callback leader would
    then leak that descendant.  Group liveness is checked independently and
    the remaining group is escalated to SIGKILL before the leader is reaped.
    """
    _terminate_owned_group(process, grace_seconds=grace_seconds)


def _callback_cancelled(args: argparse.Namespace) -> bool:
    callback = getattr(args, "cancelled", None)
    if callable(callback) and callback():
        return True
    event = getattr(args, "cancel_event", None)
    return bool(event is not None and event.is_set())

def run_step(step: Step, cmd: list[str], args: argparse.Namespace) -> int:
    logs_dir = log_dir_for_step(step, args)
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{step.name}.log"
    with log_path.open("w", encoding="utf-8") as log_handle:
        if step.invoke is not None:
            # Callback-style steps are still pack runtime code.  Keep them
            # behind the same process boundary as command steps; otherwise a
            # built-in executor can mutate the parent runner's Python state
            # despite its subprocess execution mode.
            request_path = log_path.with_suffix(log_path.suffix + ".request")
            child_args = argparse.Namespace(**vars(args))
            child_args.__dict__.pop("cancelled", None)
            child_args.__dict__.pop("cancel_event", None)
            request_path.write_bytes(
                pickle.dumps(
                    {
                        "module": step.invoke.__module__,
                        "function": step.invoke.__name__,
                        "args": child_args,
                    },
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            )
            # Preserve whether this runner itself belongs to an admitted
            # worker before adding the marker to the child environment.
            # Tests and direct callers may need the isolated-session path;
            # the marker we set below is for the callback worker's import
            # guard and is not, by itself, evidence of an inherited session.
            internal_worker = os.environ.get("ASTRID_INTERNAL_INVOCATION") == "1"
            env = os.environ.copy()
            env["ASTRID_INTERNAL_INVOCATION"] = "1"
            package_parent = str(checkout_root())
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                package_parent
                if not existing_pythonpath
                else os.pathsep.join((package_parent, existing_pythonpath))
            )
            # Direct callbacks get their own session so a timeout can clean
            # up descendants even when the callback leader exits first.  A
            # callback running inside an admitted GenericPackHost worker must
            # inherit that worker's session: otherwise outer host
            # cancellation cannot reach this detached callback process.
            launcher = popen_owned_process if internal_worker else popen_owned_group
            process = launcher(
                [sys.executable, "-X", "utf8", "-m", "astrid.core.execution.pipeline_step_worker", str(request_path)],
                cwd=str(args.out),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                **({"start_new_session": False} if internal_worker else {}),
            )
            assert process.stdout is not None
            lines: queue.Queue[str | None] = queue.Queue()

            def read_output() -> None:
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            deadline = time.monotonic() + max(
                0.0, float(getattr(args, "callback_timeout", 300.0))
            )
            timed_out = False
            cancelled = False
            stream_closed = False
            while process.poll() is None:
                observe_tree(process)
                try:
                    while True:
                        line = lines.get_nowait()
                        if line is None:
                            stream_closed = True
                            break
                        log_handle.write(line)
                        if args.verbose:
                            sys.stdout.write(line)
                            sys.stdout.flush()
                except queue.Empty:
                    pass
                if _callback_cancelled(args):
                    cancelled = True
                    if internal_worker:
                        # The callback shares the admitted worker's session;
                        # terminate only this process and descendants so the
                        # worker remains able to report cancellation upstream.
                        _terminate_owned_tree(process)
                    else:
                        _terminate_process_group(process)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    if internal_worker:
                        _terminate_owned_tree(process)
                    else:
                        _terminate_process_group(process)
                    break
                time.sleep(0.02)
            returncode = process.wait()
            _terminate_owned_tree(process)
            reader.join(timeout=1)
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    break
                if line is not None:
                    log_handle.write(line)
                    if args.verbose:
                        sys.stdout.write(line)
                        sys.stdout.flush()
            if timed_out:
                log_handle.write("callback timed out\n")
                returncode = 124
            elif cancelled:
                log_handle.write("callback cancelled\n")
                returncode = 143
            if not internal_worker:
                _release_owned_group(process)
            request_path.unlink(missing_ok=True)
        else:
            env = os.environ.copy()
            if getattr(args, "audit", None) is not None:
                env["ASTRID_AUDIT_RUN_DIR"] = str(args.out)
                parent_ids = getattr(args, "audit_parent_ids", [])
                if parent_ids:
                    env[PARENT_IDS_ENV] = ",".join(parent_ids)
            if getattr(args, "no_audit", False):
                env["ASTRID_AUDIT_DISABLED"] = "1"
            # Command steps deliberately inherit the worker's session.  A
            # command-step-created session would be invisible to the outer
            # GenericPackHost cancellation group and could leave a render
            # running after the task had been cancelled.
            internal_worker = env.get("ASTRID_INTERNAL_INVOCATION") == "1"
            launcher = popen_owned_process if internal_worker else popen_owned_group
            process = launcher(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                env=env,
                **({"start_new_session": False} if internal_worker else {}),
            )
            assert process.stdout is not None
            lines: queue.Queue[str | None] = queue.Queue()

            def read_output() -> None:
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            deadline = None
            command_timeout = getattr(args, "command_timeout", None)
            if command_timeout is not None:
                deadline = time.monotonic() + max(0.0, float(command_timeout))
            cancelled = False
            timed_out = False
            while process.poll() is None:
                observe_tree(process)
                try:
                    while True:
                        line = lines.get_nowait()
                        if line is None:
                            break
                        log_handle.write(line)
                        if args.verbose:
                            sys.stdout.write(line)
                            sys.stdout.flush()
                except queue.Empty:
                    pass
                if _callback_cancelled(args):
                    cancelled = True
                    if internal_worker:
                        # Nested command steps inherit the outer worker
                        # session.  Kill only this command and its descendants;
                        # killing the process group would also kill the Hype
                        # worker that must report cancellation upstream.
                        _terminate_owned_tree(process)
                    else:
                        _terminate_process_group(process)
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    if internal_worker:
                        _terminate_owned_tree(process)
                    else:
                        _terminate_process_group(process)
                    break
                time.sleep(0.02)
            returncode = process.wait()
            _terminate_owned_tree(process)
            reader.join(timeout=1)
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    break
                if line is not None:
                    log_handle.write(line)
                    if args.verbose:
                        sys.stdout.write(line)
                        sys.stdout.flush()
            if timed_out:
                log_handle.write("command timed out\n")
                returncode = 124
            elif cancelled:
                log_handle.write("command cancelled\n")
                returncode = 143
            if not internal_worker:
                _release_owned_group(process)
    if returncode != 0:
        print_log_tail(step.name, log_path)
    elif getattr(args, "audit", None) is not None:
        output_ids = _register_step_outputs(step, cmd, args, log_path)
        if output_ids:
            args.audit_parent_ids = output_ids
    return returncode

def _asset_kind_for_sentinel(name: str) -> str:
    return {
        "transcript.json": "transcript",
        "scenes.json": "scenes",
        "quality_zones.json": "quality_zones",
        "shots.json": "shots",
        "scene_triage.json": "scene_triage",
        "scene_descriptions.json": "scene_descriptions",
        "quote_candidates.json": "quote_candidates",
        "pool.json": "pool",
        "arrangement.json": "arrangement",
        "hype.timeline.json": "timeline",
        "hype.assets.json": "assets_registry",
        "hype.metadata.json": "metadata",
        "refine.json": "refinement",
        "hype.mp4": "render",
        "editor_review.json": "editor_review",
        "validation.json": "validation",
    }.get(name, Path(name).suffix.lstrip(".") or "artifact")

def _register_step_outputs(step: Step, cmd: list[str], args: argparse.Namespace, log_path: Path) -> list[str]:
    audit: AuditContext = args.audit
    parent_ids = list(getattr(args, "audit_parent_ids", []))
    output_ids: list[str] = []
    for path in sentinel_paths(step, args):
        if not path.exists():
            continue
        output_ids.append(
            audit.register_asset(
                kind=_asset_kind_for_sentinel(path.name),
                path=path,
                label=f"{step.name}: {path.name}",
                parents=parent_ids,
                stage=step.name,
                registration_source="pipeline_fallback",
            )
        )
    log_id = audit.register_asset(
        kind="log",
        path=log_path,
        label=f"{step.name} log",
        parents=parent_ids,
        stage=step.name,
        registration_source="pipeline_fallback",
    )
    audit.register_node(
        stage=step.name,
        label=f"Pipeline step: {step.name}",
        parents=parent_ids,
        metadata={"command": _redact_command(cmd)},
        outputs=[*output_ids, log_id],
        registration_source="pipeline_fallback",
    )
    return output_ids or [log_id]

def _redact_command(cmd: list[str]) -> list[str]:
    safe: list[str] = []
    skip_next = False
    for token in cmd:
        if skip_next:
            safe.append("<redacted>")
            skip_next = False
            continue
        safe.append(token)
        if token in {"--env-file", "--api-key", "--token", "--password"}:
            skip_next = True
    return safe
