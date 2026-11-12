"""ComfyUI contribution to generation.engine (Plan 32 T2a).

Requirements come from VibeComfy's per-workflow ReadyMetadata. Alignment and
execution use the same local run path on managed local engines and pods.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

MODES = ("align", "check", "trust")


class _ComfyUIValidationError(ValueError):
    missing: list[str]
    unpinned: bool


_WORKFLOWS: dict[Any, list[Any]] = {}


def _call_hook(hook: Any, *args: Any) -> Any:
    """Call a workflow hook; one that declares an ``engine`` parameter is given the engine's facts."""
    import inspect
    if "engine" in inspect.signature(hook).parameters:
        from astrid.core.generation.engine_facts import comfyui_facts
        return hook(*args, engine=comfyui_facts())
    return hook(*args)


def _workflows(entry: Any, inputs: Any = None) -> list[Any]:
    """The workflow(s) an entry runs: its built graph, its bundle, or an action entry's profile workflows."""
    if entry.get("workflow") is not None:
        return [entry["workflow"]]  # the already built, bound graph (no second build or mode choice)
    build = (entry.get("hooks") or {}).get("build")
    if build is not None:  # requirements() reads the built graph's rows; build is deterministic, so not cached
        return [_call_hook(build, inputs or {}, entry.get("files") or {}, _job_dir(entry))["workflow"]]
    key = (entry.get("action"), entry.get("bundle"), tuple(entry.get("requirements") or ()) if entry.get("action") else ())
    if key not in _WORKFLOWS:
        _WORKFLOWS[key] = _load_workflows(entry)
    return _WORKFLOWS[key]


def _load_workflows(entry: Any) -> list[Any]:
    from astrid.core.generation import entries
    if not entry.get("action"):
        return [entries._load_entry_workflow(entry)]
    import yaml

    from astrid.core.pack.loader import packs_root
    pack = packs_root() / entry["action"].split(".")[0]
    return [entries._load_entry_workflow({"bundle": str(pack / yaml.safe_load(
        (pack / ref).read_text(encoding="utf-8"))["workflow"])}) for ref in entry.get("requirements") or ()]


def _pinned_locks() -> dict[tuple[str, str], Any]:
    """Pinned model-lock rows by (destination, revision), across every RunPod engine."""
    from astrid.core.generation import entries
    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    rows: dict[tuple[str, str], Any] = {}
    for engine in dict.fromkeys(("comfyui", *entries.runpod_engines())):
        try:
            lock = load_recipe_model_lock(engine, entries.RUNPOD_PLATFORM)
        except Exception:  # noqa: BLE001 - reported through the raised error or fallback; diagnostic at debug, never a user-facing traceback
            logging.getLogger(__name__).debug('Could not load pinned model lock for engine %s', engine, exc_info=True)
            continue
        for model in lock.models:
            rows.setdefault((model.destination, model.revision), model)
    return rows


def _key(row: Any) -> str:
    return f"{row.get('subdir') or ''}/{row['name']}".lstrip("/")


def _present(models_root: Path, row: Any) -> bool:
    path = models_root / _key(row)
    if not path.is_file():
        return False
    return row.get("size") is None or path.stat().st_size == row["size"]


class VibeComfyCommands:
    """VibeComfy's own commands, the only place a model is fetched.

    The argv shapes are checked against vibecomfy.cli.build_parser() in test_run_local.py.
    Every command takes the workflow path; prepare runs with --no-models --no-packs so that
    `vibecomfy fetch` is the only command that downloads a model, and `nodes restore` the only one
    that installs node packs. fetch skips files already on disk, so it is not passed --force.
    """

    def __init__(self, workflow: str = "", lockfile: str | None = None, *, runner: Any = None) -> None:
        self.workflow, self.lockfile, self.runner = workflow, lockfile, runner

    def argv(self, step: str) -> list[str]:
        if step == "doctor":
            return ["doctor", self.workflow, "--non-interactive"]
        if step == "prepare":
            return ["prepare", self.workflow, "--deps", "sync", "--no-models", "--no-packs", "--non-interactive"]
        if step == "nodes_restore":
            return ["nodes", "--non-interactive", "restore", *(["--lockfile", self.lockfile] if self.lockfile else [])]
        if step == "fetch":
            return ["fetch", self.workflow, "--non-interactive"]
        raise ValueError(step)

    def _run(self, step: str) -> None:
        if self.runner is not None:
            self.runner(self.argv(step))
            return
        import subprocess
        import sys
        subprocess.run([sys.executable, "-c", "import sys;from vibecomfy.cli import main;sys.exit(main(sys.argv[1:]))",
                        *self.argv(step)], check=True)

    def doctor(self) -> None:
        self._run("doctor")

    def prepare_deps_sync(self) -> None:
        self._run("prepare")

    def nodes_restore(self, nodes: Any) -> None:
        self._run("nodes_restore")

    def fetch(self, rows: Any) -> None:
        self._run("fetch")  # per workflow; VibeComfy skips the rows already present


def _job_dir(job: Any) -> Any:
    """The one directory build and after_outputs share: the job's job_dir, else its out."""
    return job.get("job_dir") or job.get("out")


class ComfyUIEngine:
    name = "comfyui"

    def __init__(self, commands: Any = None, models_root: Any = None) -> None:
        self.commands = commands
        self.models_root = models_root

    def requirements(self, entry: Any, inputs: Any) -> dict[str, Any]:
        return self.workflow_requirements(_workflows(entry, inputs))

    def workflow_requirements(self, workflows: Any) -> dict[str, Any]:
        """Requirements of the already selected canonical graph(s), without rebuilding."""
        models: dict[tuple[str, str | None], dict[str, Any]] = {}
        nodes: dict[str, dict[str, Any]] = {}
        base = None
        for workflow in workflows:
            metadata = workflow.metadata
            for asset in metadata.get("model_assets", []):
                row = {"name": asset["name"], "subdir": asset.get("subdir") or "", "url": asset.get("url"),
                       "sha256": asset.get("sha256"), "size": asset.get("size_bytes")}
                models.setdefault((row["subdir"], row["name"]), row)
            requirements = metadata.get("requirements") or {}
            base = base or (requirements.get("runtime") or {}).get("comfy_commit")
            refs = [*(requirements.get("custom_node_refs") or ()), *(metadata.get("custom_node_packs") or {}).values()]
            for ref in refs:
                repo, commit = ref.get("url") or ref.get("repository"), ref.get("commit") or ref.get("revision")
                if repo:
                    nodes.setdefault(repo, {"repo": repo, "commit": commit})
        if any(row["sha256"] is None or row["size"] is None for row in models.values()):
            pinned = _pinned_locks()  # a workflow that pins only a url takes digest and size from the lock
            for row in models.values():
                revision = (row["url"] or "").split("/resolve/")[-1].split("/")[0]
                model = pinned.get((f"{row['subdir']}/{row['name']}", revision))
                if model is not None:
                    row["sha256"] = row["sha256"] or model.sha256.removeprefix("sha256:")
                    row["size"] = row["size"] if row["size"] is not None else model.bytes
        extra = {"nodes": list(nodes.values())}
        self.validate_extra(extra)
        return {"engine": self.name, "base": base, "models": list(models.values()), "extra": extra}

    def validate_extra(self, extra: Any) -> None:
        """ComfyUI's extra is exactly nodes[repo, commit]; anything else is refused."""
        nodes = extra.get("nodes") if isinstance(extra, dict) and set(extra) == {"nodes"} else None
        if not isinstance(nodes, list) or not all(
                isinstance(node, dict) and set(node) == {"repo", "commit"}
                and all(isinstance(v, str) and v for v in node.values()) for node in nodes):
            error = _ComfyUIValidationError("comfyui extra must be {'nodes': [{'repo': str, 'commit': str}, ...]}")
            error.unpinned = True  # a custom node ref without its repo or commit, as distinct from a loader failure
            raise error

    def align(self, manifest: Any, mode: str, *, workflow: Any = None) -> Any:
        """Align this disk to the manifest. Models arrive only through VibeComfy fetch.

        ``workflow`` is the executable job path, separate from the portable requirements manifest.
        align: restore declared nodes, fetch missing rows, then dependency preparation and diagnosis.
        check: report the missing rows; touch nothing.
        trust: accept the manifest as declared; the result is marked unverified.
        """
        if mode not in MODES:
            raise ValueError(f"align mode must be one of {MODES}, not {mode!r}")
        models_root = Path(manifest.get("models_root") or self.models_root or "")
        missing = [row for row in manifest.get("models", []) if not _present(models_root, row)]
        result = {"mode": mode, "missing": [_key(r) for r in missing], "fetched": [], "verified": mode != "trust"}
        if mode == "trust":
            result["status"] = "unverified"
            return result
        if mode == "check":
            result["status"] = "ready" if not missing else "missing"
            return result
        commands = self.commands or VibeComfyCommands(str(workflow or manifest.get("workflow", "")), manifest.get("lockfile"))
        commands.nodes_restore((manifest.get("extra") or {}).get("nodes", []))
        if missing:
            commands.fetch(missing)
        commands.prepare_deps_sync()
        commands.doctor()
        result.update(fetched=[_key(r) for r in missing], status="ready")
        return result

    def probe(self, endpoint: str, timeout: float = 5.0) -> dict[str, Any]:
        """Report whether a ComfyUI at endpoint is ready: GET /object_info must answer with a JSON object.

        Never raises: a closed port, a timeout, a non-200 or non-JSON answer is {"ready": False, "reason": ...}.
        """
        import json
        import urllib.request
        url = str(endpoint).rstrip("/") + "/object_info"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310  (caller-validated endpoint)
                info = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001  (every failure is a clean not-ready)
            return {"ready": False, "endpoint": endpoint, "reason": f"{type(exc).__name__}: {exc}", "exception": exc}
        if not isinstance(info, dict):
            return {"ready": False, "endpoint": endpoint, "reason": "object_info is not a JSON object"}
        return {"ready": True, "endpoint": endpoint, "nodes": len(info)}

    def prepare(self, job: Any) -> Any:
        """Run the entry's optional build once (CPU, deterministic): {workflow, bindings, plan}, or None."""
        build = (job.get("hooks") or {}).get("build")
        if build is None:
            return None
        built = _call_hook(build, job.get("inputs", {}), job.get("files", {}), _job_dir(job))
        if not isinstance(built, dict) or "workflow" not in built:
            raise ValueError("build must return {'workflow': ..., 'bindings': ..., 'plan': ...}")
        from astrid.core.generation.entries import prepare_execution_bindings
        bindings = dict(built.get("bindings") or {})
        prepared = prepare_execution_bindings(built["workflow"], bindings)
        for name in prepared.keys() - bindings.keys():
            built["workflow"].set_input(name, prepared[name])
        return {**built, "bindings": prepared}

    def finish(self, job: Any, outputs: Any) -> Any:
        """Run the entry's optional after_outputs on this machine, after execute and before copy-back.

        Not VibeComfy's template finalize. A raise fails the job; the caller never swallows it.
        """
        after_outputs = (job.get("hooks") or {}).get("after_outputs")
        if after_outputs is None:
            return outputs
        return _call_hook(after_outputs, _job_dir(job), job.get("inputs", {}), job.get("files", {}), outputs)

    def dry_run(self, job: Any) -> Any:
        """Build only; nothing is aligned, started or submitted. The plan is what the caller prints."""
        built = self.prepare(job)
        return {"plan": built.get("plan") if built else None,
                "bindings": built.get("bindings") if built else None}

    def run(self, job: Any, local: Any) -> Any:
        """Build, align, start or reuse ComfyUI on this machine, submit to localhost, then finalize."""
        built = self.prepare(job)
        endpoint = job.get("endpoint")
        if endpoint:  # attached: probe the caller's server, then trust it; align only when asked for
            probed = self.probe(endpoint)
            if not probed["ready"]:
                raise ValueError(f"ComfyUI at {endpoint} is not ready: {probed['reason']}") from probed.get("exception")
            if job.get("mode") == "align":
                self.align(job["manifest"], "align", workflow=job.get("workflow"))
            url = endpoint
        else:
            report = self.align(job["manifest"], job.get("mode", "align"), workflow=job.get("workflow"))
            if job.get("refuse_missing") and report.get("status") == "missing":
                error = _ComfyUIValidationError("local engine is missing declared models: " + ", ".join(report["missing"])
                                   + "; run: astrid generate align --apply --consent user_env --all")
                error.missing = report["missing"]
                raise error
            url = local.ensure_running()  # reuses a live server (attached) or starts one
        outputs = local.submit(url, built["workflow"] if built else job["workflow"])
        return self.finish(job, outputs)

    def execute(self, job: Any) -> Any:
        """Run job["workflow"] through the one local run path; files land in job["out"].

        A thin delegation to vibecomfy's run action (_run_and_settle), which is also what the
        comfyui-workflow backend reaches. Returns its manifest: {"outputs": [...], ...}.
        """
        from astrid.packs.vibecomfy.actions.run import run as run_action
        keys = ("execution_identity", "attempt_identity", "readiness_profile_path", "readiness_profile_hash",
                "comfy_url", "comfy_output_dir", "placement", "execution_request")
        return run_action._run_and_settle(
            Path(job["workflow"]), Path(job["out"]), task_identity=job.get("task_identity"),
            **{key: job[key] for key in keys if job.get(key) is not None})



ENGINE = ComfyUIEngine()
