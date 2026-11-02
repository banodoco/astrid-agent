"""Run-loop helpers, step execution, editor-review iteration, and audit registration for the hype orchestrator.

Extracted from ``run.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import pickle
import queue
import signal
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from astrid.core import timeline
from astrid.pack_api import PARENT_IDS_ENV, AuditContext
from astrid.pack_api import AstridError, render_astrid_error
from astrid.pack_api import sha256_file
from astrid.pack_api import group_exists as _owned_group_exists, popen_owned_group, popen_owned_process, observe_tree, release_group as _release_owned_group, signal_group as _signal_owned_group, terminate_tree as _terminate_owned_tree, terminate_group as _terminate_owned_group
from astrid.packs.video_editing.shared.result_receipts import write_action_receipt

from .config import STEP_ORDER
from .steps import PER_BRIEF_SENTINELS, Step


def _clear_per_brief_sentinels(brief_out: Path) -> None:
    for name in PER_BRIEF_SENTINELS:
        (brief_out / name).unlink(missing_ok=True)


# Brief frontmatter keys we recognize. Unknown keys are parsed
# (best-effort) but ignored, so future briefs can declare additional metadata
_BRIEF_FRONTMATTER_BOOL_KEYS = ("allow_generative_visuals",)


def _coerce_frontmatter_value(raw: str) -> object:
    """Coerce a YAML-like scalar string into a Python value.

    Recognizes: ``true``/``false`` (case-insensitive) -> bool; bare integers and
    floats -> numeric; strings wrapped in matching single or double quotes ->
    unquoted str; everything else -> raw str (whitespace-trimmed).
    """
    text = raw.strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if (text.startswith("\"") and text.endswith("\"") and len(text) >= 2) or (
        text.startswith("'") and text.endswith("'") and len(text) >= 2
    ):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text

def parse_brief_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split an optional YAML-like ``---``-fenced frontmatter block off a brief.

    The frontmatter must begin on line 1 with a line containing only ``---``,
    end with another line containing only ``---``, and contain
    ``key: value`` pairs (one per line). Blank lines and ``#`` comment lines
    inside the block are tolerated. When no frontmatter is present, returns
    ``({}, text)`` unchanged.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    metadata: dict[str, object] = {}
    closing_index: int | None = None
    for index in range(1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if stripped == "---":
            closing_index = index
            break
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            # Malformed line inside frontmatter; treat the file as having no
            # frontmatter to avoid silently corrupting a brief that happens to
            # start with three dashes (e.g. an em-dash separator).
            return {}, text
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key:
            return {}, text
        metadata[key] = _coerce_frontmatter_value(value)
    if closing_index is None:
        return {}, text
    body = "\n".join(lines[closing_index + 1 :])
    return metadata, body

def _brief_allow_generative_visuals(metadata: dict[str, object]) -> bool:
    """Return the truth value of the ``allow_generative_visuals`` frontmatter key.

    Treats missing keys, non-bool values, and the literal ``False`` as
    ``False`` so a malformed brief never silently enables generative effects.
    """
    return metadata.get("allow_generative_visuals") is True

def prepare_brief_artifacts(args: argparse.Namespace) -> None:
    args.brief_out.mkdir(parents=True, exist_ok=True)
    source_text = args.brief.read_text(encoding="utf-8")
    metadata, body = parse_brief_frontmatter(source_text)
    args.brief_frontmatter = metadata
    args.brief_allow_generative_visuals = _brief_allow_generative_visuals(metadata)
    body_bytes = body.encode("utf-8")
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    existing_hash = sha256_file(args.brief_copy) if args.brief_copy.is_file() else None
    if existing_hash == body_hash:
        return
    if existing_hash is not None:
        _clear_per_brief_sentinels(args.brief_out)
    args.brief_copy.write_bytes(body_bytes)

def step_output_root(step: Step, args: argparse.Namespace) -> Path:
    return args.brief_out if step.per_brief else args.out


def log_dir_for_step(step: Step, args: argparse.Namespace) -> Path:
    return step_output_root(step, args) / "logs"


def sentinel_paths(step: Step, args: argparse.Namespace) -> list[Path]:
    root = step_output_root(step, args)
    return [root / name for name in step.sentinels]

def should_rerun(step: Step, args: argparse.Namespace, forced: bool) -> bool:
    if forced:
        return True
    if step.always_run:
        return True
    paths = sentinel_paths(step, args)
    existing = [path.exists() for path in paths]
    if step.name == "refine" and all(existing):
        refine_path = paths[0]
        for name in ("hype.timeline.json", "hype.assets.json", "hype.metadata.json"):
            candidate = step_output_root(step, args) / name
            if candidate.exists() and candidate.stat().st_mtime > refine_path.stat().st_mtime:
                return True
    if step.name == "render" and all(existing):
        render_path = paths[0]
        for name in ("hype.timeline.json", "hype.assets.json", "hype.metadata.json", "refine.json"):
            candidate = step_output_root(step, args) / name
            if candidate.exists() and candidate.stat().st_mtime > render_path.stat().st_mtime:
                return True
    if step.name == "editor_review" and all(existing):
        review_path = paths[0]
        candidate = step_output_root(step, args) / "hype.mp4"
        if candidate.exists() and candidate.stat().st_mtime > review_path.stat().st_mtime:
            return True
    if all(existing):
        return False
    if step.name == "cut" and any(existing):
        print("cut: partial prior output detected, rerunning")
    return True

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
            package_parent = str(Path(__file__).resolve().parents[5])
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
                [sys.executable, "-X", "utf8", "-m", "astrid.packs.video_editing.actions.hype.step_worker", str(request_path)],
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

def write_skip_log(step: Step, args: argparse.Namespace, message: str) -> None:
    logs_dir = log_dir_for_step(step, args)
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / f"{step.name}.log").write_text(message + "\n", encoding="utf-8")
    print(message)

def _notes_overlap_ratio(prev: list[dict[str, Any]], curr: list[dict[str, Any]]) -> float:
    from astrid.packs.editorial.api import notes_overlap_ratio

    return notes_overlap_ratio(prev, curr)

def _plan_action(review: dict[str, Any]) -> str:
    from astrid.packs.editorial.api import plan_next_action

    return plan_next_action(review)

def _apply_trim_deltas_to_arrangement(
    path: Path,
    notes: list[dict[str, Any]],
) -> None:
    """Apply editor notes to an attempt-local arrangement artifact.

    This is a pure worker-side artifact transform. It never writes a runtime
    timeline; applying a resulting timeline remains an explicit client action.
    """
    arrangement = timeline.load_arrangement(path, assign_missing_uuids=True)
    clips_by_order = {int(clip["order"]): clip for clip in arrangement.get("clips", [])}
    clips_by_uuid = {str(clip["uuid"]): clip for clip in arrangement.get("clips", []) if isinstance(clip.get("uuid"), str)}
    for note in notes:
        if note.get("action") != "micro-fix":
            continue
        clip = None
        clip_uuid = note.get("clip_uuid")
        if isinstance(clip_uuid, str) and clip_uuid:
            clip = clips_by_uuid.get(clip_uuid)
            if clip is None:
                logging.warning(
                    "pipeline: editor note clip_uuid=%r not found; falling back to clip_order",
                    clip_uuid,
                )
        else:
            logging.warning("pipeline: editor note missing clip_uuid; falling back to clip_order")
        if clip is None:
            clip_order = note.get("clip_order")
            clip = clips_by_order.get(clip_order) if isinstance(clip_order, int) else None
        if not clip:
            continue
        audio_source = clip.get("audio_source")
        if not isinstance(audio_source, dict):
            continue
        trim_range = audio_source.get("trim_sub_range")
        if not isinstance(trim_range, list) or len(trim_range) != 2:
            continue
        detail = note.get("action_detail")
        if not isinstance(detail, dict):
            continue
        trim_range[0] = float(trim_range[0]) + float(detail.get("trim_delta_start_sec", 0.0))
        trim_range[1] = float(trim_range[1]) + float(detail.get("trim_delta_end_sec", 0.0))

    # Persist only the attempt-local arrangement artifact for downstream
    # worker steps.
    timeline.save_arrangement(arrangement, path)

def _rotate_editor_review(brief_out: Path, iteration: int) -> None:
    review_path = brief_out / "editor_review.json"
    if not review_path.exists():
        return
    review_path.replace(brief_out / f"editor_review.iter{iteration}.json")

def _invalidate_downstream_sentinels(brief_out: Path) -> None:
    for name in (
        "hype.timeline.json",
        "hype.assets.json",
        "hype.metadata.json",
        "refine.json",
        "hype.mp4",
        "hype.mp4.provenance.json",
        "editor_review.json",
    ):
        (brief_out / name).unlink(missing_ok=True)

def _run_revise(args: argparse.Namespace, prior_arrangement: Path, editor_notes: Path) -> int:
    from astrid.packs.video_editing.actions.hype import entrypoint as run_mod

    from .steps import (  # late import to avoid circular dependency
        Step,
        add_extra_args,
        step_argv,
    )

    step = Step(
        "arrange_revise",
        ("arrangement.json",),
        lambda step_args: add_extra_args(
            step_args,
            "arrange",
            [
                *step_argv("editorial.arrange", step_args.python_exec),
                "--pool",
                str(step_args.out / "pool.json"),
                "--brief",
                str(step_args.brief_copy),
                "--out",
                str(step_args.brief_out),
                "--source-slug",
                str(step_args.source_slug),
                "--brief-slug",
                str(step_args.brief_slug),
                "--revise",
                "--from-arrangement",
                str(prior_arrangement),
                "--editor-notes",
                str(editor_notes),
                *(["--env-file", str(step_args.env_file)] if step_args.env_file else []),
            ],
        ),
        per_brief=True,
    )
    return run_mod.run_step(step, step.build_cmd(args), args)


def _run_steps_once(steps: list[Step], args: argparse.Namespace) -> int:
    from astrid.packs.video_editing.actions.hype import entrypoint as run_mod
    from_index = STEP_ORDER.index(args.from_step) if getattr(args, "from_step", None) else None
    for step in steps:
        if step.name in {"refine", "render", "editor_review"} and not args.render:
            continue
        if step.name == "validate" and not args.render:
            write_skip_log(step, args, "validate: skipped because --render was not set")
            continue
        forced = from_index is not None and STEP_ORDER.index(step.name) >= from_index
        if not should_rerun(step, args, forced):
            continue
        returncode = run_mod.run_step(step, step.build_cmd(args), args)
        if returncode != 0:
            return returncode
    return 0


def _write_result_manifest(args: argparse.Namespace) -> None:
    """Publish the orchestrator's attempt-local result bundle.

    GenericPackHost discovers declared outputs from this manifest.  Keeping
    the bundle rooted under ``out/briefs`` makes the orchestrator's result
    explicit without creating a second project-level plan or ledger.
    """
    inputs = {
        name: (None if value is None else str(value))
        for name, value in (
            ("video", getattr(args, "video", None)),
            ("audio", getattr(args, "audio", None)),
            ("brief", getattr(args, "brief", None)),
            ("theme", getattr(args, "theme", None)),
        )
    }
    output_paths = {"briefs": "briefs"}
    if (args.out / "hype.plan.json").is_file():
        output_paths["plan"] = "hype.plan.json"
    write_action_receipt(
        args.out,
        kind="video_editing.hype",
        inputs=inputs,
        output_paths=output_paths,
        result_ports=("briefs",),
    )

def pool_main(args: argparse.Namespace) -> int:
    from .steps import _write_dry_run_plan, select_steps  # late import to avoid circular dependency

    args.out.mkdir(parents=True, exist_ok=True)
    args.audit = None if args.no_audit else AuditContext.for_run(args.out)
    if args.audit is not None:
        _register_run_inputs(args)
    prepare_brief_artifacts(args)
    # Phase 3 mixed-mode: --dry-run plans the run without invoking executors.
    # Computes step set, builds redacted commands, writes hype.plan.json, exits.
    if getattr(args, "dry_run", False):
        returncode = _write_dry_run_plan(args)
        if returncode == 0:
            _write_result_manifest(args)
        return returncode

    selected_steps = select_steps(args)

    # The runtime ledger owns orchestration state.  Hype emits only its
    # attempt-local result artifacts (including the optional dry-run plan);
    # it never reads or writes a project-root plan.json.
    steps = [step for step in selected_steps if step.name not in set(args.skip)]
    editor_steps = [step for step in steps if step.name != "validate"]
    validate_steps = [step for step in steps if step.name == "validate"]
    args.editor_iteration = 1
    prior_notes: list[dict[str, Any]] | None = None

    while True:
        returncode = _run_steps_once(editor_steps, args)
        if returncode != 0:
            return returncode
        if not args.render:
            _write_result_manifest(args)
            return 0

        review_path = args.brief_out / "editor_review.json"
        if not review_path.exists():
            return 0
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            break
        if not isinstance(review, dict):
            break
        notes = review.get("notes") if isinstance(review.get("notes"), list) else []
        if review.get("verdict") == "ship":
            break
        if int(args.editor_iteration) >= int(args.max_editor_passes):
            break
        if prior_notes is not None and _notes_overlap_ratio(prior_notes, notes) >= 0.8:
            break

        action = _plan_action(review)
        arrangement_path = args.brief_out / "arrangement.json"
        if action == "micro-fix":
            _apply_trim_deltas_to_arrangement(
                arrangement_path,
                notes,
            )
        elif action == "rework":
            returncode = _run_revise(args, arrangement_path, review_path)
            if returncode != 0:
                return returncode
        else:
            break

        prior_notes = notes
        _rotate_editor_review(args.brief_out, int(args.editor_iteration))
        _invalidate_downstream_sentinels(args.brief_out)
        args.editor_iteration = int(args.editor_iteration) + 1
        args.from_step = "cut"
    if args.render and validate_steps:
        returncode = _run_steps_once(validate_steps, args)
        if returncode == 0:
            _write_result_manifest(args)
        return returncode
    _write_result_manifest(args)
    return 0

def _register_run_inputs(args: argparse.Namespace) -> None:
    audit: AuditContext = args.audit
    parents: list[str] = []
    if args.video is not None:
        parents.append(
            audit.register_asset(
                kind="source_video",
                path=str(args.video),
                label="Source video",
                stage="pipeline.input",
            )
        )
    if args.audio is not None:
        parents.append(
            audit.register_asset(
                kind="source_audio",
                path=str(args.audio),
                label="Source audio",
                stage="pipeline.input",
            )
        )
    if args.brief is not None:
        parents.append(
            audit.register_asset(
                kind="brief",
                path=args.brief,
                label="Brief",
                stage="pipeline.input",
            )
        )
    if args.theme is not None:
        parents.append(
            audit.register_asset(
                kind="theme",
                path=args.theme,
                label="Theme",
                stage="pipeline.input",
            )
        )
    for key, path in args.asset_pairs:
        parents.append(
            audit.register_asset(
                kind="source_asset",
                path=str(path),
                label=f"Source asset: {key}",
                stage="pipeline.input",
                metadata={"asset_key": key},
            )
        )
    audit.register_node(
        stage="pipeline.run",
        label="Pipeline run",
        parents=parents,
        metadata={
            "source_slug": args.source_slug,
            "brief_slug": args.brief_slug,
            "render": args.render,
            "skips": args.skip,
        },
    )
    args.audit_parent_ids = parents
