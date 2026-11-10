"""Trusted host preparation of an installation before a sandboxed HTTP worker."""
from __future__ import annotations

from concurrent.futures import CancelledError
import copy
import hashlib
import json
import re
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit

PREPARATION_ENV = "ASTRID_MANAGED_ENGINE_PREPARATION_JSON"
SCHEMA = "astrid.managed-engine-preparation.v1"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def configured_models_root(engine: str = "comfyui", *, root: str | None = None) -> str:
    """Read the installed cache binding without starting or contacting its owner.

    No receipt means the proposed default root, not a readiness assertion.
    Existing malformed/unsafe receipts fail closed, including on a cold install.
    """
    from ._engine_target_files import _TargetFiles
    from .engine_contracts import effective_models_root, IntegrityError
    from .engine_local_endpoint import DEFAULT_PLATFORM, default_engine_root
    from .engine_recipe import load_recipe
    from .engine_runner import _parse_receipt, plan_install
    root = root or default_engine_root(engine)
    files = _TargetFiles(root)
    path = str(Path(root) / ".astrid-engine/install.json")
    deadline = time.monotonic() + 10
    fact = files.file_fact(path, hash_bytes=False, deadline=deadline)
    if not fact.exists:
        return effective_models_root(root, None)
    if fact.symlink or not fact.regular or fact.bytes is None or fact.bytes > 1024 * 1024:
        raise IntegrityError("managed install receipt is not a safe regular file")
    raw = files.read_json(path, deadline=deadline)
    recipe = load_recipe(engine, DEFAULT_PLATFORM)
    plan = plan_install(recipe, root=root, placement="local", models_root=raw.get("models_root"))
    receipt = _parse_receipt(raw, plan)
    return effective_models_root(root, receipt.models_root)


def read_preparation(*, task: str, attempt: str | None, endpoint: str,
                     execution_identity: str | None = None, inputs: Any = None) -> dict[str, Any] | None:
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
    from .engine_local_endpoint import _valid_root
    expected_mode = os.environ.get("ASTRID_ALIGN", "check")
    installation = body["installation"]
    health = body["health"]
    if (not isinstance(installation, dict)
            or set(installation) != {"root", "receipt_sha256", "recipe_sha256", "helper_sha256"}
            or not _valid_root(installation["root"])
            or any(not isinstance(installation[key], str) or re.fullmatch(r"[0-9a-f]{64}", installation[key]) is None
                   for key in ("receipt_sha256", "recipe_sha256", "helper_sha256"))
            or set(health) != {"healthy", "object_info_sha256", "required_nodes_present"}
            or health["required_nodes_present"] is not True
            or not isinstance(health["object_info_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", health["object_info_sha256"]) is None
            or not isinstance(body["inputs"], dict)
            or set(body["inputs"]) not in ({"workflow"}, {"python", "companion", "source"})
            or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                   for value in body["inputs"].values())
            or body["manifest"].get("engine") != "comfyui"
            or set(body["manifest"]) - {"engine", "base", "models", "extra", "models_root"}
            or not _valid_root(body["manifest"].get("models_root"))
            or body["mode"] != expected_mode
            or body["alignment"].get("mode") != body["mode"]
            or body["alignment"].get("status") not in {"ready", "unverified"}
            or not _valid_root(body["output_dir"])):
        raise ValueError("managed engine preparation has invalid installation facts")
    manifest = body["manifest"]
    alignment = body["alignment"]
    if (set(manifest) != {"engine", "base", "models", "extra", "models_root"}
            or manifest["base"] is not None and not isinstance(manifest["base"], str)
            or not isinstance(manifest["models"], list)
            or not isinstance(manifest["extra"], dict) or set(manifest["extra"]) != {"nodes"}
            or not isinstance(manifest["extra"]["nodes"], list)
            or set(alignment) != {"mode", "missing", "fetched", "verified", "status"}
            or alignment["verified"] is not (body["mode"] != "trust")
            or alignment["status"] != ("unverified" if body["mode"] == "trust" else "ready")
            or any(not isinstance(alignment[key], list) or not all(isinstance(item, str) for item in alignment[key])
                   for key in ("missing", "fetched"))
            or body["mode"] in {"check", "trust"} and alignment["fetched"]
            or body["mode"] == "check" and alignment["missing"]):
        raise ValueError("managed engine preparation has invalid requirements or alignment")
    for row in manifest["models"]:
        if (not isinstance(row, dict) or set(row) != {"name", "subdir", "url", "sha256", "size"}
                or not isinstance(row["name"], str) or not row["name"] or Path(row["name"]).name != row["name"]
                or not isinstance(row["subdir"], str) or Path(row["subdir"]).is_absolute()
                or any(part in {".", ".."} for part in row["subdir"].split("/"))
                or row["url"] is not None and not isinstance(row["url"], str)
                or row["sha256"] is not None and (not isinstance(row["sha256"], str)
                    or re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", row["sha256"]) is None)
                or row["size"] is not None and (type(row["size"]) is not int or row["size"] < 0)):
            raise ValueError("managed engine preparation has an invalid model row")
    for node in manifest["extra"]["nodes"]:
        if (not isinstance(node, dict) or set(node) != {"repo", "commit"}
                or any(not isinstance(value, str) or not value for value in node.values())):
            raise ValueError("managed engine preparation has an invalid node row")
    if inputs is not None and any(name not in inputs or not Path(inputs[name]).is_file()
            or hashlib.sha256(Path(inputs[name]).read_bytes()).hexdigest() != expected
            for name, expected in body["inputs"].items()):
        raise ValueError("managed engine preparation input bytes differ from this attempt")
    return body


class ManagedEnginePreparation:
    """One lock and one supervisor client, retained until the attempt has drained.

    The install lock excludes another host's preparation and installation while
    the HTTP worker runs. No worker receives this object or its control socket.
    """
    def __init__(self, *, task: str, attempt: str, mode: str, deadline: float, cancelled: Any, scratch: Path):
        from .engine_execution import create_local_backend
        from .engine_local_endpoint import DEFAULT_PLATFORM, default_engine_root
        from .engine_recipe import load_recipe

        self.task, self.attempt, self.mode = task, attempt, mode
        self.scratch = scratch
        self.deadline, self.cancelled = deadline, cancelled
        self.root = default_engine_root("comfyui")
        self.recipe = load_recipe("comfyui", DEFAULT_PLATFORM)
        self.context, self.adapter = create_local_backend(engine="comfyui", platform=DEFAULT_PLATFORM,
            placement="local", root=self.root, deadline=deadline)
        self.context.service_client.cancelled = cancelled
        self.lock = None
        self.report = None
        self.uncertain = False

    def check(self):
        if self.cancelled():
            raise CancelledError("managed preparation cancelled")
        if time.monotonic() >= self.deadline:
            from .engine_contracts import DeadlineExceeded
            raise DeadlineExceeded("managed preparation deadline exceeded")

    def close(self):
        if self.uncertain:
            from .engine_contracts import CustodyUnresolved
            raise CustodyUnresolved("managed preparation retains an unconfirmed command; keep installation fenced")
        if self.lock is not None:
            lock, self.lock = self.lock, None
            lock.__exit__(None, None, None)

    def prepare(self, workflow: Path, *, execution_identity: str, inputs: Any, canonical_workflow: Any, input_digests: Any) -> dict[str, Any]:
        from .engine_contracts import CustodyUnresolved
        try:
            return self._prepare(workflow, execution_identity=execution_identity, inputs=inputs, canonical_workflow=canonical_workflow, input_digests=input_digests)
        except CustodyUnresolved:
            self.uncertain = True
            raise

    def _prepare(self, workflow: Path, *, execution_identity: str, inputs: Any, canonical_workflow: Any, input_digests: Any) -> dict[str, Any]:
        from .engine_contracts import IntegrityError, effective_models_root
        from .engine_recipe import recipe_digest
        from .engine_runner import _Runner, _parse_receipt, plan_install
        from astrid.packs.vibecomfy.actions.run import run as run_action

        self.check()
        staged_members = [workflow]
        if workflow.suffix == ".py":
            staged_members += [workflow.with_suffix(".vibe.json"), workflow.with_name("source.json")]
        staged_digests = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in staged_members}
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
        runner.step_id = "helper-identity"
        if runner.helper("identity") != {"helper_sha256": runner.helper_sha256}:
            raise IntegrityError("managed engine helper identity differs")
        for step in plan.steps:
            if step.kind in {"source", "environment"}:
                runner.execute(step, readonly=True)
        if runner.locks != receipt.requirements_locks or runner.commits != receipt.source_commits:
            raise IntegrityError("managed installation evidence differs from its receipt")
        engine = copy.copy(run_action._engine_for({"engine": "comfyui"}))
        manifest = engine.workflow_requirements([canonical_workflow])
        manifest = {**manifest, "engine": "comfyui", "models_root": configured_models_root(root=self.root)}
        commands_type = type(engine).requirements.__globals__["VibeComfyCommands"]
        self.scratch.mkdir(parents=True, exist_ok=True)
        lockfile = self.scratch / "managed-custom-nodes.lock"
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
        if any(not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected
               for path, expected in staged_digests.items()):
            raise IntegrityError("managed alignment changed the canonical workflow bytes")
        runner.step_id = "start"
        runner.start(readonly=False)
        self.check()
        runner.step_id = "health"
        runner.check_health()
        self.check()
        health = {"healthy": True, "object_info_sha256": runner.health.object_info_sha256,
                  "required_nodes_present": runner.health.required_nodes_present}
        self.report = {"schema": SCHEMA, "task_id": self.task, "attempt_id": self.attempt,
            "execution_identity": execution_identity, "inputs": dict(input_digests),
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
            import logging
            logging.getLogger(__name__).error(
                "managed alignment failed task=%s attempt=%s step=%s exit=%s stderr_sha256=%s",
                self.task, self.attempt, argv[0], result.exit_code,
                hashlib.sha256(result.stderr.encode()).hexdigest())
            raise IntegrityError("managed alignment command failed; inspect the host log for this attempt")
        self.check()
