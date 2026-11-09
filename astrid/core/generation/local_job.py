"""``astrid generate <entry> --placement local --job FILE --out DIR --json`` (Plan 32 T4g).

The pod-side command of a rented machine: read the ``astrid.machine-job.v1``
file the controller wrote, resolve the entry's requirements manifest, and run
the one local run path with the job's ``align``. Progress goes to
``ASTRID_PROGRESS_PATH``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = "astrid.machine-job.v1"
ALIGN_MODES = ("align", "check", "trust")
#: Job inputs that name the shipped workflow of an entry-less (workflow-file) job.
_WORKFLOW_INPUTS = ("source", "python")


class JobFileError(ValueError):
    """The job file is unreadable or not an astrid.machine-job.v1 document."""


def read_job(path: str | Path) -> dict[str, Any]:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JobFileError(f"job file could not be read: {path}: {exc}") from exc
    if not isinstance(body, dict) or body.get("schema") != SCHEMA:
        raise JobFileError(f"job file is not {SCHEMA}")
    if body.get("align") not in ALIGN_MODES:
        raise JobFileError(f"job align must be one of {', '.join(ALIGN_MODES)}")
    if not isinstance(body.get("inputs"), dict):
        raise JobFileError("job inputs must be an object")
    return body


def run_job(entry: str, job_path: str | Path, out: str | Path, *, mode: str | None = None,
            align: str | None = None) -> dict[str, Any]:
    """Run one job file locally; returns the run's manifest. ``align`` overrides the job's."""
    from astrid.core.generation import entries
    from astrid.core.generation.progress import write_progress
    from astrid.packs.vibecomfy.actions.run import run as run_action

    write_progress("prepare")
    job = read_job(job_path)
    if align is not None and align not in ALIGN_MODES:
        raise JobFileError(f"align must be one of {', '.join(ALIGN_MODES)}")
    inputs = dict(job["inputs"])
    execution = job.get("execution")
    if execution is not None:
        if not isinstance(execution, dict):
            raise JobFileError("job execution must be an object")
        execution = dict(execution)
    task = job.get("task_identity") or "pod-job"
    attempt = job.get("attempt_identity") or "-"
    if job.get("entry") not in (None, entry) and entry != "workflow":
        raise JobFileError(f"job entry {job.get('entry')!r} differs from the requested {entry!r}")
    if execution is not None:
        bindings = job.get("bindings", json.loads(execution.get("workflow_inputs") or "{}"))
        if not isinstance(bindings, dict):
            raise JobFileError("job entry bindings must be an object")
        return _run_prepared_job(entry, inputs, execution, bindings, Path(out), task=task, attempt=attempt,
                                 mode=mode or job.get("mode"), align=align or job["align"])
    if entry == "workflow":
        shipped = next((inputs.pop(name) for name in _WORKFLOW_INPUTS if name in inputs), None)
        if shipped is None:
            raise JobFileError("a workflow-file job must ship its workflow")
        workflow, named = Path(str(shipped)), None
    else:
        described = entries.find_entry(entry)
        if described is None:
            raise JobFileError(f"entry {entry!r} is not available on this machine")
        workflow, named = entries.bundle_path(described), entry
    # File inputs ride the job directory; they reach the run as its own file arguments.
    files = {name: str(inputs.pop(name)) for name in ("managed_assets", "source_video") if name in inputs}
    if named is None:
        return run_action._run_and_settle(
            workflow, Path(out), task_identity=task, attempt_identity=attempt,
            workflow_inputs=json.dumps(inputs, sort_keys=True), entry=None,
            align=align or job["align"], **files)
    return _run_named_entry(described, named, inputs, files, Path(out), task=task, attempt=attempt,
                            mode=mode or job.get("mode"), align=align or job["align"])


def _run_prepared_job(entry: str, inputs: dict[str, Any], execution: dict[str, Any], bindings: dict[str, Any], out: Path,
                      *, task: str, attempt: str, mode: str | None, align: str) -> dict[str, Any]:
    """Execute a controller-prepared bundle; public inputs never include transport controls."""
    from astrid.core.generation import entries
    from astrid.packs.vibecomfy.actions.run import run as run_action

    workflow = execution.get("python") or execution.get("workflow") or execution.get("source")
    if not isinstance(workflow, str):
        raise JobFileError("a prepared job must ship its workflow")
    files = {name: str(execution[name]) for name in ("managed_assets", "source_video") if execution.get(name)}
    if entry == "workflow":
        named = None
    else:
        try:
            description, inputs = entries.resolve_entry_inputs(entry, inputs, mode=mode)
        except (ValueError, KeyError, TypeError) as exc:
            raise JobFileError(f"job inputs are not valid for entry {entry!r}: {exc}") from exc
        mode = description.get("mode") or mode
        described = entries.find_entry(entry)
        named = entry
        if described is None:
            from astrid.core.model_catalog.registry import ModelRegistry
            catalog = ModelRegistry.load_default().get(entry)
            selected = catalog.modes.get(mode)
            backend = selected.backends.get("local") if selected is not None else None
            if backend is None or not backend.template:
                raise JobFileError(f"entry {entry!r} has no local workflow for mode {mode!r}")
            named = str(backend.template)
            described = entries.find_entry(named)
        if described is None:
            raise JobFileError(f"entry {entry!r} is not available on this machine")
        if entries.entry_hooks(described).get("build"):
            return _run_named_entry(described, entry, inputs, {}, out, task=task, attempt=attempt,
                                    mode=mode, align=align, bindings=bindings)
        # The facade has already bound this exact graph. Rebinding or rebuilding it
        # would discard its chosen seed, native controls and uploaded media bindings.
    from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path
    with staged_workflow_path(workflow=execution.get("workflow"), python=execution.get("python"),
                              companion=execution.get("companion"), source=execution.get("source")) as (workflow, _):
        return run_action._run_and_settle(
            workflow, out, task_identity=task, attempt_identity=attempt, entry=named,
            workflow_inputs=json.dumps(bindings, sort_keys=True), align=align, **files)


def _run_named_entry(described: dict[str, Any], named: str, inputs: dict[str, Any], files: dict[str, str],
                     out: Path, *, task: str, attempt: str, mode: str | None, align: str,
                     bindings: dict[str, Any] | None = None) -> dict[str, Any]:
    """A named entry through the contract the controller's facade applies (sdk/generation.py).

    * the CLI's own validation of the entry's public inputs; the resolved values (aliases to public
      names) are what every later step reads;
    * readiness and ``target_schema`` of the entry, as the facade's local path checks them;
    * a contribution's ``build`` once, before the run, and its ``after_outputs`` once, after it
      (``entries.build_entry``; ``after_outputs`` runs inside ``_run_and_settle`` through its
      ``after_outputs_job`` seam, before the outputs are sealed);
    * otherwise the entry's ``binds`` and ``defaults`` applied to the workflow
      (``entries.apply_workflow_inputs``, the same function ``entries.prepare`` calls).
    """
    import shutil
    import tempfile
    from types import SimpleNamespace

    from astrid.core.generation import entries
    from astrid.core.generation.preflight import require_generation_target_readiness
    from astrid.packs.vibecomfy.actions.run import run as run_action

    try:
        description, inputs = entries.resolve_entry_inputs(named, inputs, mode=mode)
    except (ValueError, KeyError, TypeError) as exc:
        raise JobFileError(f"job inputs are not valid for entry {named!r}: {exc}") from exc
    mode = description.get("mode") or mode
    # (c) readiness and target schema, on the machine that runs the job. The pod has no attached
    # target client, which is the controller's managed-placement case (client None).
    prerequisite = SimpleNamespace(id=named, modes={mode: SimpleNamespace(
        backends={"local": SimpleNamespace(template=str(described["bundle"]))})})
    require_generation_target_readiness(prerequisite, mode, None)
    # A contribution's build and after_outputs come from its vibecomfy.workflows entry.
    hooks = entries.entry_hooks(described)
    run_files = dict(files)
    job_dir = None
    manifest = None
    try:
        if "build" in hooks:
            job_dir = Path(tempfile.mkdtemp(prefix="astrid-job-"))
            built = entries.build_entry(described, inputs, job_dir)  # (b) build, once
            workflow, workflow_inputs = Path(built["bundle"]) / "workflow.py", dict(built["bindings"])
            if bindings:
                # Native bindings belong to the chosen graph, never to the public
                # entry schema. Replay the controller's resolved seed and controls.
                for name, value in bindings.items():
                    built["workflow"].set_input(name, value)
                workflow_inputs.update(bindings)
                from vibecomfy.workflow_bundle import emit_bundle
                emit_bundle(built["workflow"], workflow, {"operation": "authored"})
            assets = dict(built.get("assets") or {})
            # The requirements of the graph build chose, not of the static bundle. Read from the built
            # folder, so build is not called a second time.
            chosen = {k: v for k, v in described.items() if k not in ("hooks", "hooks_declared", "requirements")}
            manifest = entries.requirements({**chosen, "bundle": built["bundle"]}, inputs)
            entries.target_schema(described, built["workflow"], None, align=align)
            if assets:
                import zipfile
                from astrid.packs.vibecomfy.asset_manifest import build_asset_manifest
                asset_manifest = build_asset_manifest(assets)
                archive = job_dir / "assets.zip"
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
                    zipped.writestr("manifest.json", json.dumps(asset_manifest))
                    for record in asset_manifest["assets"]:
                        zipped.write(assets[record["binding"]], record["member"])
                run_files["managed_assets"] = str(archive)
                workflow_inputs = {k: v for k, v in workflow_inputs.items() if k not in assets}
        else:
            # (a) the facade's entries.prepare, on the controller's own arguments: values are the
            # caller's inputs, binds and defaults the entry's.
            loaded = entries._load_entry_workflow(described)
            try:
                _, plan, assets = entries.apply_workflow_inputs(
                    loaded, mode=mode, values=inputs, binds=described.get("binds"),
                    defaults=described.get("defaults"))
            except (ValueError, KeyError) as exc:
                raise JobFileError(f"job inputs are not valid for entry {named!r}: {exc}") from exc
            if plan["unbound"]:
                raise JobFileError("workflow has no binding for requested controls: " + ", ".join(plan["unbound"]))
            entries.target_schema(described, loaded, None, align=align)
            workflow, workflow_inputs = entries.bundle_path(described), dict(plan["bindings"])
            if assets:
                # Media bindings are file paths on this machine; they ride as plain bindings.
                workflow_inputs.update({name: str(path) for name, path in assets.items()})
        hook_job = {}
        if "after_outputs" in hooks:
            # (b) after_outputs once, inside the run, on the managed outputs before they are sealed;
            # a raise fails the job. It reads the state build left in this job directory.
            job = entries.entry_job(described, inputs, job_dir, hooks={})
            hook_job = {"after_outputs_job": json.dumps(
                {"entry": described["id"], "job_dir": str(job_dir), "inputs": job["inputs"], "files": job["files"]},
                sort_keys=True, default=str)}
        result = run_action._run_and_settle(
            workflow, out, task_identity=task, attempt_identity=attempt,
            workflow_inputs=json.dumps(workflow_inputs, sort_keys=True, default=str), entry=named,
            align=align, manifest=manifest, **hook_job, **run_files)
        return result
    finally:
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
