"""Trusted host preparation of an installation before a sandboxed HTTP worker."""
from __future__ import annotations

from concurrent.futures import CancelledError
import copy
import hashlib
import json
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit

PREPARATION_ENV = "ASTRID_MANAGED_ENGINE_PREPARATION_JSON"
SCHEMA = "astrid.managed-engine-preparation.v1"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_preparation(*, task: str, attempt: str | None, endpoint: str,
                     execution_identity: str | None = None) -> dict[str, Any] | None:
    """Read only the host-projected, attempt-bound preparation, never ambient policy."""
    import os
    raw = os.environ.get(PREPARATION_ENV)
    if raw is None:
        return None
    body = json.loads(raw)
    keys = {"schema", "task_id", "attempt_id", "execution_identity", "inputs", "manifest", "manifest_sha256",
            "installation", "endpoint", "output_dir", "placement", "mode", "alignment", "health"}
    if (not isinstance(body, dict) or set(body) != keys or body["schema"] != SCHEMA
            or body["task_id"] != task or body["attempt_id"] != attempt or body["endpoint"] != endpoint
            or body["placement"] != "managed" or body["mode"] not in {"align", "check", "trust"}
            or not isinstance(body["manifest"], dict) or digest(body["manifest"]) != body["manifest_sha256"]
            or not isinstance(body["alignment"], dict) or not isinstance(body["health"], dict)
            or body["health"].get("healthy") is not True
            or execution_identity is not None and body["execution_identity"] != execution_identity):
        raise ValueError("managed engine preparation differs from this attempt")
    return body


class ManagedEnginePreparation:
    """One lock and one supervisor client, retained until the attempt has drained.

    The install lock excludes another host's preparation and installation while
    the HTTP worker runs. No worker receives this object or its control socket.
    """
    def __init__(self, *, task: str, attempt: str, mode: str, deadline: float, cancelled: Any):
        from .engine_execution import create_local_backend
        from .engine_local_endpoint import DEFAULT_PLATFORM, default_engine_root
        from .engine_recipe import load_recipe

        self.task, self.attempt, self.mode = task, attempt, mode
        self.deadline, self.cancelled = deadline, cancelled
        self.root = default_engine_root("comfyui")
        self.recipe = load_recipe("comfyui", DEFAULT_PLATFORM)
        self.context, self.adapter = create_local_backend(engine="comfyui", platform=DEFAULT_PLATFORM,
            placement="local", root=self.root, deadline=deadline)
        self.context.service_client.cancelled = cancelled
        self.lock = None
        self.report = None

    def check(self):
        if self.cancelled():
            raise CancelledError("managed preparation cancelled")
        if time.monotonic() >= self.deadline:
            from .engine_contracts import DeadlineExceeded
            raise DeadlineExceeded("managed preparation deadline exceeded")

    def close(self):
        if self.lock is not None:
            lock, self.lock = self.lock, None
            lock.__exit__(None, None, None)

    def prepare(self, workflow: Path, *, execution_identity: str, inputs: Any) -> dict[str, Any]:
        from .engine_contracts import IntegrityError, effective_models_root
        from .engine_recipe import recipe_digest
        from .engine_runner import _Runner, _parse_receipt, plan_install
        from astrid.packs.vibecomfy.actions.run import run as run_action

        self.check()
        lock = self.adapter.local_install_lock(self.root, deadline=self.deadline, cancelled=self.cancelled)
        lock.__enter__()
        self.lock = lock
        self.check()
        receipt_path = str(Path(self.root) / ".astrid-engine/install.json")
        raw = self.context.read_json(receipt_path, deadline=self.deadline)
        plan = plan_install(self.recipe, root=self.root, placement="local", models_root=raw.get("models_root"))
        receipt = _parse_receipt(raw, plan)
        if receipt.status != "installed":
            raise IntegrityError("managed preparation requires a completed installation")
        runner = _Runner(plan, self.context, self.adapter, self.deadline)
        # These checks read the saved environment identities; they do not
        # re-enter the install lock or require the HTTP service to be running.
        runner.check_environment_identities()
        engine = copy.copy(run_action._engine_for({"engine": "comfyui"}))
        manifest = run_action._engine_manifest(engine, workflow)
        manifest = {**manifest, "engine": "comfyui", "models_root": effective_models_root(self.root, receipt.models_root)}
        commands_type = type(engine).requirements.__globals__["VibeComfyCommands"]
        lockfile = workflow.parent / "managed-custom-nodes.lock"
        lines = []
        for node in (manifest.get("extra") or {}).get("nodes", []):
            name = Path(urlsplit(node["repo"]).path).name.removesuffix(".git")
            if not name or any(c.isspace() or c in "/\\" for c in name):
                raise IntegrityError("managed node name is invalid")
            lines += [f"[nodepacks.{json.dumps(name)}]", f"url = {json.dumps(node['repo'])}",
                      f"commit = {json.dumps(node['commit'])}"]
        lockfile.write_text("\n".join(lines) + "\n")
        if engine.commands is None:
            engine.commands = commands_type(str(workflow), str(lockfile), runner=self._command)
        self.check()
        alignment = engine.align(manifest, self.mode, workflow=workflow)
        if alignment.get("status") == "missing":
            raise IntegrityError("managed engine is missing declared models: " + ", ".join(alignment["missing"]))
        self.check()
        runner.step_id = "start"
        runner.start(readonly=False)
        self.check()
        runner.step_id = "health"
        runner.check_health()
        self.check()
        health = {"healthy": True, "object_info_sha256": runner.health.object_info_sha256,
                  "required_nodes_present": runner.health.required_nodes_present}
        self.report = {"schema": SCHEMA, "task_id": self.task, "attempt_id": self.attempt,
            "execution_identity": execution_identity, "inputs": {
                name: hashlib.sha256(Path(value).read_bytes()).hexdigest()
                for name, value in inputs.items() if name in {"workflow", "python", "companion", "source"} and value},
            "manifest": manifest, "manifest_sha256": digest(manifest),
            "installation": {"root": self.root, "receipt_sha256": hashlib.sha256(Path(receipt_path).read_bytes()).hexdigest(),
                "recipe_sha256": recipe_digest(self.recipe), "helper_sha256": runner.helper_sha256},
            "endpoint": f"http://127.0.0.1:{self.recipe.start.port}",
            "output_dir": str(Path(self.root) / next(s.relative_path for s in self.recipe.sources if s.name == "comfyui") / "output"),
            "placement": "managed", "mode": self.mode, "alignment": alignment, "health": health}
        return self.report

    def _command(self, argv):
        """VibeComfy's native commands use the installed Python and fixed target paths."""
        from .engine_contracts import CommandRequest, DeadlineExceeded, IntegrityError
        self.check()
        primary = next(e for e in self.recipe.environments if e.name == self.recipe.primary_environment)
        python = self.adapter.venv_python(self.adapter.join(self.root, primary.relative_path))
        comfy = self.adapter.join(self.root, next(s.relative_path for s in self.recipe.sources if s.name == "comfyui"))
        from .engine_custody import _installed_models_root
        models = _installed_models_root(self.root, self.recipe) or self.adapter.join(self.root, "models")
        env = {"COMFYUI_PATH": comfy, "VIBECOMFY_MODELS_ROOT": models,
               "VIBECOMFY_CUSTOM_NODES_DIR": comfy + "/custom_nodes", "VIBECOMFY_HEADLESS": "1",
               "VIBECOMFY_COMFY_CONFIGURATION": json.dumps({"runtime_root": self.root + "/runtime",
                   "comfy_root": comfy, "models_root": models, "python": python}),
               "PATH": str(Path(python).parent) + ":/usr/local/bin:/usr/bin:/bin"}
        if argv[0] == "prepare":
            argv = [*argv, "--runtime-root", self.root + "/runtime"]
        result = self.context.run(CommandRequest((python, "-I", "-c",
            "import sys;from vibecomfy.cli import main;sys.exit(main(sys.argv[1:]))", *argv), comfy, env, self.deadline))
        if result.timed_out:
            raise DeadlineExceeded("managed alignment command timed out")
        if result.exit_code != 0:
            raise IntegrityError("managed alignment command failed: " + str(result.exit_code) + ": " + result.stderr[-1500:])
        self.check()
