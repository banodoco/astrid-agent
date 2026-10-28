"""Canonical full-install planner and context-only executor (CONTRACTS v1).

No provider, transport, subprocess or target filesystem implementation lives here.
``engine_runner_target`` supplies finite file/environment operations; A2 supplies
``engine_execution --engine-service`` with custody-backed service operations.
The latter must never acknowledge a detached/unowned process as an owned server.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import ntpath
import posixpath
import re
import shlex
import time
import uuid
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from typing import Mapping

from . import engine_recipe as recipes
from .engine_contracts import (
    CommandFailed,
    CommandRequest,
    CustodyUnresolved,
    Deadline,
    DeadlineExceeded,
    DownloadResult,
    EndpointHealthError,
    EndpointOwnershipError,
    EngineError,
    EngineId,
    EngineRecipe,
    ExecutionContext,
    HealthEvidence,
    InstallPlan,
    InstallReceipt,
    InstallStep,
    IntegrityError,
    Placement,
    PlatformAdapter,
    PlatformKey,
    RecipeValidationError,
    StepReceipt,
    TargetPath,
    TransportError,
    UnsafePathError,
    UnsupportedPlacement,
    UnsupportedPlatform,
)

_HELPER_SCHEMA = "astrid.engine-runner-helper.v1"
_SERVICE_SCHEMA = "astrid.engine-service.v1"
_RECEIPT = ".astrid-engine/install.json"
__all__ = ["plan_install", "install_engine", "engine_status"]
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# Explicit package closure, never a directory walk over ambient modules.
_CODE_PATHS = (
    "__init__.py", "version.py", "core/__init__.py", "core/generation/__init__.py",
    "core/generation/engine_contracts.py", "core/generation/engine_recipe.py",
    "core/generation/engine_runner.py", "core/generation/engine_runner_target.py",
    "core/generation/model_downloader.py",
)
# A2 must extend this list with its transitive custody/helper imports when landing.
_A2_PATHS = ("core/generation/engine_execution.py", "core/generation/engine_platforms.py",
             "core/generation/_engine_target_files.py", "core/generation/engine_custody.py",
             "core/execution/__init__.py", "core/execution/process_group.py",
             "core/execution/custody_broker.py", "core/execution/platform_helpers.py")


def _plain(value):
    if is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def _json(value):
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _deadline(deadline, step_id=None):
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)):
        raise RecipeValidationError("deadline must be a finite monotonic time", step_id=step_id)
    if time.monotonic() >= deadline:
        raise DeadlineExceeded("engine deadline exceeded", step_id=step_id, retryable=True)


def _root(root, platform):
    """Lexical target rules only; never resolve/stat a controller target path."""
    if not isinstance(root, str) or not root or any(ord(c) < 32 for c in root):
        raise UnsafePathError("install root must be a target-native absolute path")
    if platform.startswith("windows-"):
        drive, tail = ntpath.splitdrive(root)
        if not drive or not tail.startswith(("\\", "/")) or root.startswith(("\\\\", "//")):
            raise UnsafePathError("Windows root must be an absolute drive path")
        parts = re.split(r"[\\/]", tail)
        normalized = ntpath.normpath(root)
        if any(p in {".", ".."} for p in parts) or normalized == drive + "\\":
            raise UnsafePathError("unsafe Windows install root")
        return normalized
    if (not root.startswith("/") or root.startswith("//") or "\\" in root
            or ":" in root or any(p in {".", ".."} for p in root.split("/"))):
        raise UnsafePathError("Linux root must be an absolute normalized path")
    normalized = posixpath.normpath(root)
    if normalized == "/" or normalized != root:
        raise UnsafePathError("install root must be normalized and below filesystem root")
    return normalized


def plan_install(recipe: EngineRecipe, *, root: TargetPath, placement: Placement) -> InstallPlan:
    if not isinstance(recipe, EngineRecipe):
        raise RecipeValidationError("plan_install requires an EngineRecipe")
    if placement not in {"local", "runpod"}:
        raise UnsupportedPlacement("unknown engine placement")
    if placement == "runpod" and not recipe.platform.startswith("linux-"):
        raise UnsupportedPlatform("RunPod requires a Linux target")
    verified = recipes.load_recipe(recipe.engine, recipe.platform)
    if recipes.recipe_digest(recipe) != recipes.recipe_digest(verified):
        raise IntegrityError("recipe differs from the verified packaged recipe")
    root = _root(root, verified.platform)
    models = recipes.load_model_lock(verified.models_lock)
    steps = []
    for source in verified.sources:
        parents = tuple("source:" + parent.name for parent in verified.sources
                        if source.relative_path.startswith(parent.relative_path + "/"))
        # The loader preserves source order; reject a forward parent dependency.
        if not set(parents).issubset({s.id for s in steps}):
            raise RecipeValidationError("source parents must precede nested checkouts")
        steps.append(InstallStep("source:" + source.name, "source", parents,
                                 "Verify pinned source " + source.name))
    source_ids = tuple(s.id for s in steps)
    for env in verified.environments:
        steps.append(InstallStep("environment:" + env.name, "environment", source_ids,
                                 "Verify hash-locked environment " + env.name))
    env_ids = tuple(s.id for s in steps if s.kind == "environment")
    for model in models.models:
        steps.append(InstallStep("model:" + model.name, "model", env_ids,
                                 "Verify locked model " + model.name))
    prerequisites = tuple(s.id for s in steps if s.kind in {"environment", "model"})
    steps.extend((InstallStep("start", "start", prerequisites, "Start or reuse the owned loopback engine"),
                  InstallStep("health", "health", ("start",), "Validate owned health and required nodes")))
    return InstallPlan("astrid.engine-plan.v1", verified.engine, verified.platform, placement,
                       root, recipes.recipe_digest(verified), verified.models_lock.sha256, tuple(steps))


def helper_manifest(recipe: EngineRecipe) -> Mapping[str, str]:
    """Read-only local code/data identity, also recomputed by the target helper.

    Keys are package-relative paths; values hash exact bytes. A3's downloader must
    be installed before execution. Planning deliberately does not need it.
    """
    root = recipes._ROOT
    result = {}
    for relative in _CODE_PATHS:
        try:
            result[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        except OSError as exc:
            raise IntegrityError("reviewed engine helper closure is incomplete") from exc
    for relative in _A2_PATHS:
        path = root / relative
        if path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    result["packs/engine-index.json"] = hashlib.sha256(recipes._INDEX_PATH.read_bytes()).hexdigest()
    entry = next(e for e in recipes.load_recipe_index().entries
                 if (e.engine, e.platform) == (recipe.engine, recipe.platform))
    refs = [entry.recipe, recipe.pins, recipe.models_lock]
    refs += [e.requirements.packaged_lock for e in recipe.environments
             if e.requirements.packaged_lock is not None]
    for ref in refs:
        _, payload = recipes._read_ref(ref)
        result[ref.path] = hashlib.sha256(payload).hexdigest()
    return dict(sorted(result.items()))



class _Runner:
    def __init__(self, plan, context, adapter, deadline):
        _deadline(deadline)
        if not isinstance(plan, InstallPlan):
            raise RecipeValidationError("install_engine requires an InstallPlan")
        self.recipe = recipes.load_recipe(plan.engine, plan.platform)
        expected = plan_install(self.recipe, root=plan.root, placement=plan.placement)
        if plan != expected:
            raise IntegrityError("install plan differs from the recomputed verified plan")
        if (context.platform != plan.platform or adapter.key != plan.platform
                or context.placement != plan.placement):
            raise UnsupportedPlatform("context, adapter and plan target must match")
        if plan.platform.startswith("windows-"):
            raise UnsupportedPlatform("Windows runner execution requires the A7 adapter")
        if adapter.validate_root(plan.root) != plan.root:
            raise UnsafePathError("adapter changed the bound install root")
        self.plan, self.context, self.adapter, self.deadline = plan, context, adapter, deadline
        self.models_lock = recipes.load_model_lock(self.recipe.models_lock)
        self.helper_sha256 = _sha(helper_manifest(self.recipe))
        self.env = dict(adapter.environment(self.recipe, plan.root))
        self.step_id = None
        self.locks, self.commits, self.models, self.steps = {}, {}, [], []
        self.health = None
        self.receipt_id = uuid.uuid4().hex
        self.started_at = _now()
        self.service_owner = None

    def path(self, *relative):
        return self.adapter.join(self.plan.root, *relative)

    def fact(self, path, *, hashed=False):
        _deadline(self.deadline, self.step_id)
        return self.context.file_fact(path, hash_bytes=hashed, deadline=self.deadline)

    def optional_json(self, path):
        fact = self.fact(path)
        if fact.symlink:
            raise UnsafePathError("engine state file is a symlink", step_id=self.step_id)
        if not fact.exists:
            return None
        if fact.symlink or not fact.regular or fact.bytes is None or fact.bytes > 1024 * 1024:
            raise IntegrityError("invalid engine state file", step_id=self.step_id)
        _deadline(self.deadline, self.step_id)
        return self.context.read_json(path, deadline=self.deadline)

    def write(self, path, value):
        _deadline(self.deadline, self.step_id)
        self.context.write_json_atomic(path, _plain(value), deadline=self.deadline)

    def run(self, argv, *, cwd=None, env=None, check=True, deadline=None):
        limit = self.deadline if deadline is None else min(deadline, self.deadline)
        _deadline(limit, self.step_id)
        if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
            raise RecipeValidationError("engine command requires a nonempty public argv", step_id=self.step_id)
        result = self.context.run(CommandRequest(tuple(argv), cwd or self.plan.root,
                                                 self.env if env is None else env, limit))
        if result.timed_out:
            # The context may return this only after verifying exact owned stop.
            raise DeadlineExceeded("owned engine command exceeded deadline", step_id=self.step_id,
                                   retryable=True)
        if result.exit_code is None:
            raise CustodyUnresolved("command termination is unproven", step_id=self.step_id,
                                    recovery_handle=result.job_id)
        if type(result.exit_code) is not int:
            raise TransportError("invalid command exit status", step_id=self.step_id)
        if check and result.exit_code != 0:
            # Raw output, commands and arbitrary transport bodies are never public diagnostics.
            raise CommandFailed(f"engine command failed (exit {result.exit_code}); inspect target log",
                                step_id=self.step_id)
        return result

    def sanity(self, command):
        """Run the operator's sanity command under the engine child env, between environments and models.

        ``command`` is a public argv string; ``--venv <primary venv>`` is appended. The child env is
        ``engine_child_env`` (the one ComfyUI gets), never the caller's. A nonzero exit aborts the
        install before any model download; the message carries the library paths, not raw output.
        """
        from ._engine_python_bootstrap import engine_child_env
        self.step_id = "sanity"
        argv = tuple(shlex.split(command))
        primary = next(e for e in self.recipe.environments if e.name == self.recipe.primary_environment)
        venv = self.path(primary.relative_path)
        env = engine_child_env(venv, self.env) if self.plan.platform.startswith("linux-") else dict(self.env)
        result = self.run(argv + ("--venv", venv), env=env, check=False)
        if result.exit_code != 0:
            keep = [ln.strip()[:300] for ln in (result.stdout + "\n" + result.stderr).splitlines()
                    if ln.startswith(("LIB ", "FAIL ", "libs:", "gpu_sanity:"))][:20]
            raise CommandFailed("engine sanity command failed (exit %s) before any model download; "
                                "LD_LIBRARY_PATH=%s; %s" % (result.exit_code, env.get("LD_LIBRARY_PATH", ""),
                                                            " | ".join(keep) or "no library paths reported"),
                                step_id="sanity")

    def payload(self, **extra):
        return {"schema": _HELPER_SCHEMA, "engine": self.plan.engine, "platform": self.plan.platform,
                "root": self.plan.root, "recipe_sha256": self.plan.recipe_sha256,
                "helper_sha256": self.helper_sha256, **extra}

    def helper(self, operation, **extra):
        result = self.run(("python3", "-I", "-m", "astrid.core.generation.engine_runner_target",
                           operation, _json(self.payload(**extra))))
        value = self.response(result, _HELPER_SCHEMA, operation)
        if set(value) == {"error_code"}:
            from . import engine_contracts
            errors = {cls.code: cls for cls in (
                engine_contracts.IntegrityError, engine_contracts.UnsafePathError,
                engine_contracts.RecipeValidationError)}
            error = errors.get(value["error_code"], IntegrityError)
            raise error("target helper validation failed", step_id=self.step_id)
        return value

    def response(self, result, schema, operation):
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise IntegrityError("invalid target helper response", step_id=self.step_id) from exc
        if (len(result.stdout.encode("utf-8")) > 262144
                or result.stdout_truncated or result.stderr_truncated or not isinstance(value, dict)
                or set(value) != {"schema", "operation", "facts"} or value["schema"] != schema
                or value["operation"] != operation or not isinstance(value["facts"], dict)):
            raise IntegrityError("unbound target helper response", step_id=self.step_id)
        return value["facts"]

    def service(self, operation, *, launch=None, deadline=None):
        request = {**self.payload(), "schema": _SERVICE_SCHEMA,
                   "launch": _plain(launch) if launch else None,
                   "owner_id": self.service_owner,
                   "timeout_seconds": self.recipe.health.timeout_seconds,
                   "poll_interval_seconds": self.recipe.health.poll_interval_seconds}
        result = self.run(("python3", "-I", "-m", "astrid.core.generation.engine_execution",
                           "--engine-service", operation, _json(request)), deadline=deadline)
        value = self.response(result, _SERVICE_SCHEMA, operation)
        keys = {"state", "engine", "platform", "root", "recipe_sha256", "helper_sha256",
                "launch_sha256", "owner_id", "healthy", "object_info_sha256", "required_nodes_present"}
        if set(value) != keys or any(value[k] != request[k] for k in (
                "engine", "platform", "root", "recipe_sha256", "helper_sha256")):
            raise EndpointOwnershipError("service evidence does not match installation", step_id=self.step_id)
        if value["state"] not in {"absent", "owned", "unowned"}:
            raise EndpointOwnershipError("invalid service ownership state", step_id=self.step_id)
        if value["state"] == "unowned":
            raise EndpointOwnershipError("engine port is occupied by an unowned service", step_id=self.step_id)
        if value["state"] == "owned":
            if (not isinstance(value["owner_id"], str) or not value["owner_id"]
                    or len(value["owner_id"]) > 256
                    or value["launch_sha256"] != _sha(self.launch_spec())
                    or self.service_owner is not None and value["owner_id"] != self.service_owner):
                raise EndpointOwnershipError("service incarnation or launch binding differs", step_id=self.step_id)
            self.service_owner = value["owner_id"]
        return value

    def launch_spec(self):
        request = self.adapter.start_request(self.recipe, self.plan.root, deadline=self.deadline)
        if request.deadline != self.deadline or not request.argv:
            raise RecipeValidationError("adapter start request violates recipe binding", step_id=self.step_id)
        # Check all launch fields against the verified recipe's closed placeholders.
        values = self.placeholder_values()
        expected = {"argv": [s.format_map(values) for s in self.recipe.start.argv],
                    "cwd": self.recipe.start.cwd.format_map(values),
                    "env": {k: v.format_map(values) for k, v in self.recipe.start.env.items()}}
        if list(request.argv) != expected["argv"] or request.cwd != expected["cwd"]:
            raise RecipeValidationError("adapter launch differs from verified recipe", step_id=self.step_id)
        if any(request.env.get(k) != v for k, v in expected["env"].items()):
            raise RecipeValidationError("adapter launch environment differs from recipe", step_id=self.step_id)
        return {"argv": list(request.argv), "cwd": request.cwd, "env": dict(request.env)}

    def placeholder_values(self, environment=None):
        if environment is None:
            environment = next(e for e in self.recipe.environments if e.name == self.recipe.primary_environment)
        return {"install_root": self.plan.root,
                "python": self.adapter.venv_python(self.path(environment.relative_path)),
                "comfy_root": self.path(next(s.relative_path for s in self.recipe.sources if s.name == "comfyui")),
                "models_root": self.path("models"), "models_config": self.path("runtime/extra_model_paths.yaml"),
                "host": self.recipe.start.bind_host, "port": str(self.recipe.start.port)}

    def source(self, source, *, readonly=False):
        path = self.path(source.relative_path)
        fact, git = self.fact(path), self.fact(self.adapter.join(path, ".git"))
        if fact.symlink or git.symlink or git.exists and git.regular:
            raise UnsafePathError("source or Git directory is unsafe", step_id=self.step_id)
        changed = False
        if not git.exists:
            if readonly:
                raise IntegrityError("pinned source is absent", step_id=self.step_id)
            if fact.exists:
                if self.helper("empty-source", source=source.name) != {"empty": True}:
                    raise IntegrityError("source directory is not empty", step_id=self.step_id)
            self.context.ensure_dir(path, deadline=self.deadline)
            self.run(("git", "init", path))
            changed = True
        origin = self.run(("git", "-C", path, "remote", "get-url", "origin"), check=False)
        if origin.exit_code != 0:
            if readonly or origin.exit_code != 2:
                raise IntegrityError("pinned source origin is unavailable", step_id=self.step_id)
            self.run(("git", "-C", path, "remote", "add", "origin", source.pin.repository))
            changed = True
        elif origin.stdout.strip() != source.pin.repository:
            raise IntegrityError("source origin differs from canonical pin", step_id=self.step_id)
        dirty = self.run(("git", "-C", path, "status", "--porcelain", "--untracked-files=all"))
        if dirty.stdout.strip() or dirty.stdout_truncated:
            raise IntegrityError("source contains uncommitted content", step_id=self.step_id)
        head = self.run(("git", "-C", path, "rev-parse", "HEAD"), check=False)
        if origin.stdout_truncated or head.stdout_truncated:
            raise IntegrityError("source validation output was truncated", step_id=self.step_id)
        if head.exit_code != 0 or head.stdout.strip() != source.pin.revision:
            if readonly:
                raise IntegrityError("source revision differs from canonical pin", step_id=self.step_id)
            if head.exit_code not in {0, 128}:
                raise CommandFailed("could not inspect source revision", step_id=self.step_id)
            self.run(("git", "-C", path, "fetch", "--depth=1", "origin", source.pin.revision))
            self.run(("git", "-C", path, "checkout", "--detach", source.pin.revision))
            changed = True
        if changed:
            self.source(source, readonly=True)
        self.commits[source.name] = source.pin.revision
        return "executed" if changed else "skipped", {"commit": source.pin.revision}

    def environment(self, environment, *, readonly=False):
        name = environment.name
        state_path = self.path(".astrid-engine/environments", name, "receipt.json")
        lock_path = self.path(".astrid-engine/environments", name, "requirements.lock")
        lock = self.fact(lock_path, hashed=True)
        state = self.optional_json(state_path)
        if lock.exists and (lock.symlink or not lock.regular or not lock.sha256):
            raise IntegrityError("unsafe requirements lock", step_id=self.step_id)
        identity = {"recipe_sha256": self.plan.recipe_sha256, "helper_sha256": self.helper_sha256,
                    "environment": name}
        if state is not None and (not isinstance(state, Mapping)
                or any(state.get(k) != v for k, v in identity.items())
                or lock.sha256 != state.get("requirements_lock_sha256")):
            raise IntegrityError("environment receipt or persisted lock identity differs", step_id=self.step_id)
        if environment.node_lock:
            ref = environment.node_lock
            source = next(s for s in self.recipe.sources if s.name == ref.source)
            node = self.fact(self.path(source.relative_path, ref.path), hashed=True)
            if not node.regular or node.symlink or node.sha256 != ref.sha256:
                raise IntegrityError("node lock differs from pinned source file", step_id=self.step_id)
            if self.helper("node-lock", environment=name) != {"valid": True}:
                raise IntegrityError("node lock verification failed", step_id=self.step_id)
        if state is not None:
            if self.environment_probe(environment, lock.sha256):
                self.locks[name] = lock.sha256
                return "skipped", {"requirements_lock_sha256": lock.sha256}
            if readonly:
                raise IntegrityError("installed environment no longer matches its lock", step_id=self.step_id)
        elif readonly:
            raise IntegrityError("environment has no verified receipt", step_id=self.step_id)
        uv = self.adapter.uv_executable(self.path("runtime"))
        python = self.adapter.venv_python(self.path(environment.relative_path))
        preparation = self.helper("lock-inputs", environment=name)
        lock = self.fact(lock_path, hashed=True)
        if preparation != {"lock_exists": lock.exists}:
            raise IntegrityError("requirements lock state changed", step_id=self.step_id)
        uv_verified = False
        extra_env = getattr(self.adapter, "environment_command_env", None)
        for index, argv in enumerate(self.adapter.environment_commands(self.recipe, environment, self.plan.root)):
            if argv and argv[0] == uv and not uv_verified:
                self.verify_uv(uv, environment.uv_version)
                uv_verified = True
            extra = {} if extra_env is None else extra_env(self.recipe, environment, self.plan.root, index)
            self.run(argv, env={**self.env, **extra} if extra else None)
        if not uv_verified:
            self.verify_uv(uv, environment.uv_version)
        if not lock.exists:
            if environment.requirements.mode == "resolve-once":
                inputs = [self.path(next(s.relative_path for s in self.recipe.sources if s.name == p.source), p.path)
                          for p in environment.requirements.input_paths]
                inputs.append(self.path(".astrid-engine/environments", name, "requirements.in"))
                self.run((uv, "--no-config", "pip", "compile", "--python", python,
                          "--no-sources", "--generate-hashes", "--only-binary", ":all:",
                          "--exclude-newer", environment.requirements.exclude_newer,
                          "--index-url", "https://pypi.org/simple", "-o", lock_path + ".tmp", *inputs))
            self.helper("commit-lock", environment=name)
        lock = self.fact(lock_path, hashed=True)
        if not lock.regular or lock.symlink or not lock.sha256:
            raise IntegrityError("hash-bearing requirements lock was not persisted", step_id=self.step_id)
        # Persist the resolved lock identity before installing anything; retries
        # may repair the environment but can never silently re-resolve this lock.
        self.locks[name] = lock.sha256
        self.write(state_path, {**identity, "requirements_lock_sha256": lock.sha256, "validated": False})
        self.helper("install-locks", environment=name)
        support = self.path(".astrid-engine/environments", name, "support.lock")
        wheels = self.path(".astrid-engine/environments", name, "wheels.lock")
        self.run((uv, "--no-config", "pip", "sync", "--python", python,
                  "--index-url", "https://pypi.org/simple", "--require-hashes", support))
        for install in environment.source_installs:
            cwd = self.path(next(s.relative_path for s in self.recipe.sources if s.name == install.source))
            argv = tuple(a.format_map(self.placeholder_values(environment)) for a in install.argv)
            if "--no-deps" not in argv or "--no-build-isolation" not in argv:
                raise RecipeValidationError("source install must disable dependency resolution and build isolation",
                                            step_id=self.step_id)
            self.run(argv, cwd=cwd)
        if environment.final_wheels:
            self.run((uv, "--no-config", "pip", "install", "--python", python,
                      "--force-reinstall", "--no-deps", "--require-hashes", "-r", wheels))
        self.run((uv, "--no-config", "pip", "check", "--python", python))
        self.write_install_receipts(environment)
        if not self.environment_probe(environment, lock.sha256):
            raise IntegrityError("environment final verification failed", step_id=self.step_id)
        self.write(state_path, {**identity, "requirements_lock_sha256": lock.sha256, "validated": True})
        return "executed", {"requirements_lock_sha256": lock.sha256}

    def write_install_receipts(self, environment):
        """Persist per-wheel receipts after the hash-pinned (--require-hashes) install.

        uv leaves archive_info empty, so the later probe relies on these receipts
        plus the installed RECORD digest captured here.
        """
        if not environment.final_wheels:
            return
        from .engine_runner_target import _RECEIPT_SCRIPT, receipt_name
        python = self.adapter.venv_python(self.path(environment.relative_path))
        wheels = {w.name: {"version": w.version} for w in environment.final_wheels}
        result = self.run((python, "-I", "-c", _RECEIPT_SCRIPT, _json({"wheels": wheels})),
                          env={**self.env, "CUDA_VISIBLE_DEVICES": ""})
        try:
            digests = json.loads(result.stdout)["record_sha256"]
        except (ValueError, KeyError, TypeError):
            raise IntegrityError("installed wheel RECORD digests are unreadable", step_id=self.step_id)
        for wheel in environment.final_wheels:
            digest = digests.get(wheel.name) if isinstance(digests, dict) else None
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise IntegrityError("installed wheel RECORD digest is missing", step_id=self.step_id)
            self.write(self.path(".astrid-engine/environments", environment.name, receipt_name(wheel.name)),
                       {"name": wheel.name, "version": wheel.version, "url": wheel.url,
                        "sha256": wheel.sha256, "record_sha256": digest})

    def verify_uv(self, executable, version):
        result = self.run((executable, "--version"))
        if result.stdout_truncated or result.stdout.split()[:2] != ["uv", version]:
            raise IntegrityError("uv version differs from recipe", step_id=self.step_id)

    def environment_probe(self, environment, lock_digest):
        # This is static product verification code, never recipe-supplied code
        # or an installer blob. It runs under the context's process custody.
        from .engine_runner_target import _ENV_PROBE
        value = self.helper("environment-spec", environment=environment.name)
        if (set(value) != {"expected", "requirements_lock_sha256"}
                or not isinstance(value["expected"], dict)
                or value["requirements_lock_sha256"] != lock_digest):
            raise IntegrityError("environment probe inputs differ from persisted lock", step_id=self.step_id)
        python = self.adapter.venv_python(self.path(environment.relative_path))
        env = {**self.env, "CUDA_VISIBLE_DEVICES": ""}
        probe = self.run((python, "-I", "-c", _ENV_PROBE, _json(value["expected"])), env=env, check=False)
        if probe.exit_code:
            return False
        uv = self.adapter.uv_executable(self.path("runtime"))
        return self.run((uv, "--no-config", "pip", "check", "--python", python),
                        env=env, check=False).exit_code == 0

    def model(self, model, *, readonly=False):
        path = self.path("models", model.destination)
        if readonly:
            fact = self.fact(path, hashed=True)
            if (not fact.regular or fact.symlink or fact.bytes != model.bytes or fact.sha256 != model.sha256):
                raise IntegrityError("installed model failed full integrity verification", step_id=self.step_id)
            result = DownloadResult(model.name, path, "skipped", model.bytes, model.sha256, True, 0)
        else:
            _deadline(self.deadline, self.step_id)
            result = self.context.download_model(model, models_root=self.path("models"), deadline=self.deadline)
            if (not isinstance(result, DownloadResult) or result.model != model.name or result.path != path
                    or result.bytes != model.bytes or result.sha256 != model.sha256 or result.verified is not True
                    or result.action not in {"downloaded", "resumed", "skipped"}
                    or type(result.resumed_from_bytes) is not int
                    or not 0 <= result.resumed_from_bytes <= model.bytes):
                raise IntegrityError("model result differs from canonical lock", step_id=self.step_id)
        self.models.append(result)
        return ("skipped" if result.action == "skipped" else "executed"), {"bytes": result.bytes, "sha256": result.sha256}

    def start(self, *, readonly=False):
        launch = self.launch_spec()
        probe = self.service("probe", launch=launch)
        if probe["state"] == "owned":
            if self.helper("models-config-check") != {"valid": True}:
                raise IntegrityError("owned engine models configuration differs", step_id=self.step_id)
            return "skipped", {"owner_id": self.service_owner, "launch_sha256": _sha(launch)}
        if readonly:
            raise EndpointHealthError("owned engine server is absent", step_id=self.step_id)
        if self.helper("models-config") != {"written": True}:
            raise IntegrityError("engine models configuration was not written", step_id=self.step_id)
        result = self.service("start", launch=launch)
        if result["state"] != "owned":
            raise EndpointOwnershipError("server start did not establish ownership", step_id=self.step_id)
        return "executed", {"owner_id": self.service_owner, "launch_sha256": _sha(launch)}

    def check_health(self):
        if self.helper("models-config-check") != {"valid": True}:
            raise IntegrityError("engine models configuration differs", step_id=self.step_id)
        limit = min(self.deadline, time.monotonic() + self.recipe.health.timeout_seconds)
        value = self.service("health", launch=self.launch_spec(), deadline=limit)
        if value["state"] != "owned":
            raise EndpointHealthError("owned engine is absent", step_id=self.step_id)
        digest = value["object_info_sha256"]
        if (value["healthy"] is not True or value["required_nodes_present"] is not True
                or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise EndpointHealthError("engine health or required nodes failed", step_id=self.step_id)
        self.health = HealthEvidence("healthy", _now(), digest, True)
        return "skipped", {"object_info_sha256": digest, "required_nodes_present": True}

    def execute(self, step, *, readonly=False):
        self.step_id = step.id
        _deadline(self.deadline, step.id)
        if not set(step.depends_on).issubset({s.id for s in self.steps if s.validation == "passed"}):
            raise IntegrityError("step dependencies have not passed", step_id=step.id)
        if step.kind == "source":
            action, facts = self.source(next(s for s in self.recipe.sources if "source:" + s.name == step.id), readonly=readonly)
        elif step.kind == "environment":
            action, facts = self.environment(next(e for e in self.recipe.environments if "environment:" + e.name == step.id), readonly=readonly)
        elif step.kind == "model":
            action, facts = self.model(next(m for m in self.models_lock.models if "model:" + m.name == step.id), readonly=readonly)
        elif step.kind == "start":
            action, facts = self.start(readonly=readonly)
        else:
            action, facts = self.check_health()
            if not readonly and any(s.action == "executed" for s in self.steps):
                action = "executed"
        self.steps.append(StepReceipt(step.id, step.kind, action, "passed", facts))

    def receipt(self, status, error_code=None):
        return InstallReceipt("astrid.engine-install.v1", self.receipt_id, self.plan.engine,
                              self.plan.platform, self.plan.placement, self.plan.root, status, False,
                              self.plan.recipe_sha256, self.plan.models_lock_sha256, dict(self.locks),
                              dict(self.commits), tuple(self.models), tuple(self.steps), self.health,
                              self.started_at, None if status == "installing" else _now(), error_code)


SANITY_CMD_ENV = "ASTRID_ENGINE_SANITY_CMD"


def install_engine(plan: InstallPlan, *, context: ExecutionContext,
                   adapter: PlatformAdapter, deadline: Deadline, sanity_cmd: str | None = None) -> InstallReceipt:
    """Install the engine. ``sanity_cmd`` (default: ``ASTRID_ENGINE_SANITY_CMD``) runs once after the
    last environment step and before the first model step; a failure aborts the install there."""
    sanity_cmd = sanity_cmd if sanity_cmd is not None else os.environ.get(SANITY_CMD_ENV, "")
    runner = _Runner(plan, context, adapter, deadline)
    with context.acquire_install_lock(plan.root, deadline=deadline):
        # Also bind installed target code/data before any source/environment action.
        if runner.helper("identity") != {"helper_sha256": runner.helper_sha256}:
            raise IntegrityError("target engine helper identity differs")
        previous = runner.optional_json(runner.path(_RECEIPT))
        if previous is not None:
            parsed = _parse_receipt(previous, plan)
            if parsed.error_code == "custody_unresolved":
                handle = next((s.facts.get("recovery_handle") for s in reversed(parsed.steps)
                               if s.facts.get("recovery_handle")), None)
                raise CustodyUnresolved("recover the exact prior command before retrying installation",
                                        step_id=next((s.id for s in reversed(parsed.steps)
                                                      if s.validation == "failed"), None),
                                        recovery_handle=handle)
        context.ensure_dir(runner.path(".astrid-engine"), deadline=deadline)
        runner.write(runner.path(_RECEIPT), runner.receipt("installing"))
        try:
            sanity_pending = bool(sanity_cmd.strip())
            for step in plan.steps:
                if sanity_pending and step.kind not in {"source", "environment"}:
                    sanity_pending = False
                    runner.sanity(sanity_cmd)
                runner.execute(step)
                runner.write(runner.path(_RECEIPT), runner.receipt("installing"))
            # Revalidate pinned trees after package builds, and lock/model facts
            # immediately before committing admission. No installer runs here.
            for source in runner.recipe.sources:
                runner.step_id = "source:" + source.name
                runner.source(source, readonly=True)
            for environment in runner.recipe.environments:
                runner.step_id = "environment:" + environment.name
                runner.environment(environment, readonly=True)
            runner.step_id = "health"
            _, facts = runner.check_health()
            runner.steps[-1] = replace(runner.steps[-1], facts=facts)
            result = runner.receipt("installed")
            runner.write(runner.path(_RECEIPT), result)
            return result
        except EngineError as exc:
            if exc.step_id is None:
                exc.step_id = runner.step_id
            step = next((s for s in plan.steps if s.id == runner.step_id), None)  # None: the sanity hook
            if step is not None:
                facts = {"error_code": exc.code}
                if exc.recovery_handle is not None:
                    facts["recovery_handle"] = exc.recovery_handle
                failed = StepReceipt(step.id, step.kind, "executed", "failed", facts)
                indexes = [i for i, s in enumerate(runner.steps) if s.id == step.id]
                if indexes:
                    runner.steps[indexes[0]] = failed
                else:
                    runner.steps.append(failed)
            # Never mask timeout/cancellation/custody with a failed journal write.
            # No new cleanup deadline is invented; context owns recovery custody.
            try:
                runner.write(runner.path(_RECEIPT), runner.receipt("failed", exc.code))
            except (EngineError, OSError):
                pass
            raise


def _parse_receipt(raw, plan):
    """Strict JSON receipt reconstruction; a receipt is evidence, never instructions."""
    def obj(value, cls):
        if not isinstance(value, Mapping) or set(value) != {f.name for f in fields(cls)}:
            raise IntegrityError("invalid install receipt fields")
        return dict(value)
    value = obj(raw, InstallReceipt)
    recipe = recipes.load_recipe(plan.engine, plan.platform)
    models_lock = recipes.load_model_lock(recipe.models_lock)
    for key in ("engine", "platform", "placement", "root", "recipe_sha256", "models_lock_sha256"):
        if value[key] != getattr(plan, key):
            raise IntegrityError("install receipt identity differs")
    if (value["schema"] != "astrid.engine-install.v1" or value["qualified"] is not False
            or value["status"] not in ("installing", "installed", "failed")
            or not isinstance(value["receipt_id"], str) or not 1 <= len(value["receipt_id"]) <= 256):
        raise IntegrityError("invalid install receipt state")
    try:
        for key in ("started_at", "completed_at"):
            stamp = value[key]
            if stamp is not None and datetime.fromisoformat(stamp).utcoffset() is None:
                raise ValueError
        if value["started_at"] is None:
            raise ValueError
        if value["error_code"] is not None and (not isinstance(value["error_code"], str)
                or not re.fullmatch("[a-z_]+", value["error_code"])):
            raise ValueError
        for key in ("requirements_locks", "source_commits"):
            if not isinstance(value[key], Mapping):
                raise ValueError
        if not all(isinstance(v, str) and _DIGEST.fullmatch(v) for v in value["requirements_locks"].values()):
            raise ValueError
        if not all(isinstance(v, str) and re.fullmatch("[0-9a-f]{40}", v) for v in value["source_commits"].values()):
            raise ValueError
        if (not set(value["requirements_locks"]).issubset({e.name for e in recipe.environments})
                or not set(value["source_commits"]).issubset({s.name for s in recipe.sources})):
            raise ValueError
        if not isinstance(value["models"], (list, tuple)) or not isinstance(value["steps"], (list, tuple)):
            raise ValueError
        models = tuple(DownloadResult(**obj(v, DownloadResult)) for v in value["models"])
        steps = tuple(StepReceipt(**obj(v, StepReceipt)) for v in value["steps"])
        health = None if value["health"] is None else HealthEvidence(**obj(value["health"], HealthEvidence))
        if len({s.id for s in steps}) != len(steps):
            raise ValueError
        locked = {m.name: m for m in models_lock.models}
        if len({m.model for m in models}) != len(models):
            raise ValueError
        for model in models:
            expected_model = locked.get(model.model)
            if (expected_model is None or model.verified is not True
                    or model.action not in {"downloaded", "resumed", "skipped"}
                    or type(model.bytes) is not int or model.bytes != expected_model.bytes
                    or model.sha256 != expected_model.sha256
                    or model.path != posixpath.join(plan.root, "models", expected_model.destination)
                    or type(model.resumed_from_bytes) is not int
                    or not 0 <= model.resumed_from_bytes <= model.bytes):
                raise ValueError
        expected = {s.id: s.kind for s in plan.steps}
        for step in steps:
            if (step.id not in expected or step.kind != expected[step.id]
                    or step.action not in {"executed", "skipped"} or step.validation not in {"passed", "failed"}
                    or not isinstance(step.facts, Mapping)
                    or not all(isinstance(k, str) and (v is None or type(v) in {str, int, bool})
                               for k, v in step.facts.items())):
                raise ValueError
        if health is not None and (
                health.status not in {"healthy", "unhealthy", "unknown"}
                or type(health.required_nodes_present) is not bool
                or datetime.fromisoformat(health.checked_at).utcoffset() is None
                or health.object_info_sha256 is not None and (
                    not isinstance(health.object_info_sha256, str)
                    or not _DIGEST.fullmatch(health.object_info_sha256))):
            raise ValueError
        if value["status"] == "installed" and (
                tuple(s.id for s in steps) != tuple(s.id for s in plan.steps)
                or any(s.validation != "passed" for s in steps) or health is None
                or health.status != "healthy" or health.required_nodes_present is not True
                or not isinstance(health.object_info_sha256, str) or not _DIGEST.fullmatch(health.object_info_sha256)
                or value["completed_at"] is None or value["error_code"] is not None
                or set(value["requirements_locks"]) != {e.name for e in recipe.environments}
                or value["source_commits"] != {s.name: s.pin.revision for s in recipe.sources}
                or tuple(m.model for m in models) != tuple(m.name for m in models_lock.models)):
            raise ValueError
    except (TypeError, ValueError, KeyError) as exc:
        raise IntegrityError("invalid install receipt evidence") from exc
    return InstallReceipt(**{**value, "models": models, "steps": steps, "health": health})


def engine_status(engine: EngineId, *, platform: PlatformKey, placement: Placement, root: TargetPath,
                  context: ExecutionContext, deadline: Deadline) -> InstallReceipt | None:
    recipe = recipes.load_recipe(engine, platform)
    plan = plan_install(recipe, root=root, placement=placement)
    # No platform implementation import is needed for a read-only Linux status.
    # The same target path/launch spelling is supplied by the context's adapter
    # only through A2's platform factory when a receipt actually exists.
    _deadline(deadline)
    if context.platform != platform or context.placement != placement:
        raise UnsupportedPlatform("status context does not match requested target")
    if platform.startswith("windows-"):
        raise UnsupportedPlatform("Windows status requires the A7 adapter")
    path = posixpath.join(plan.root, _RECEIPT)
    fact = context.file_fact(path, hash_bytes=False, deadline=deadline)
    if fact.symlink:
        raise UnsafePathError("install receipt is a symlink")
    if not fact.exists:
        return None
    if fact.symlink or not fact.regular or fact.bytes is None or fact.bytes > 1024 * 1024:
        raise IntegrityError("invalid install receipt file")
    receipt = _parse_receipt(context.read_json(path, deadline=deadline), plan)
    if receipt.status != "installed":
        return receipt
    # Lazy optional production adapter dependency, absent from lookup/planning.
    try:
        from .engine_platforms import platform_adapter
    except ImportError as exc:
        raise UnsupportedPlatform("installed status requires the A2 production Linux adapter") from exc
    runner = _Runner(plan, context, platform_adapter(platform), deadline)
    runner.receipt_id, runner.started_at = receipt.receipt_id, receipt.started_at
    try:
        if runner.helper("identity") != {"helper_sha256": runner.helper_sha256}:
            raise IntegrityError("target engine helper identity differs")
        for step in plan.steps:
            runner.execute(step, readonly=True)
        if (runner.locks != receipt.requirements_locks or runner.commits != receipt.source_commits
                or runner.health.object_info_sha256 != receipt.health.object_info_sha256):
            raise IntegrityError("current evidence differs from installed receipt")
    except (CustodyUnresolved, DeadlineExceeded):
        raise
    except EngineError as exc:
        return replace(receipt, status="failed", health=HealthEvidence("unhealthy", _now(), None, False),
                       error_code=exc.code)
    return replace(receipt, steps=tuple(runner.steps), models=tuple(runner.models), health=runner.health)
