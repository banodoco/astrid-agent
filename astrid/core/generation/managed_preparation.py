"""Trusted host preparation of an installation before a sandboxed HTTP worker."""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import time
from concurrent.futures import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .engine_contracts import (
    MANAGED_ENGINE_PREPARATION_ENV as PREPARATION_ENV,
)
from .engine_contracts import (
    EngineRecipe,
    IntegrityError,
    SourcePatch,
)
from .engine_local_endpoint import DEFAULT_ENGINE

SCHEMA = "astrid.managed-engine-preparation.v1"
_PREPARATION_CONTEXT = ContextVar("astrid_managed_engine_preparation", default=None)
_NATIVE_STEPS = frozenset({"runtime_inspect", "nodes_restore", "pip_bootstrap", "fetch", "prepare", "doctor"})
# A preparation can outlive its Python call when a POST may still be accepted
# and cleanup cannot prove that its command drained. Keep the actual lock owner
# alive for this process lifetime; dropping the wrapper must not close the fd.
_UNRESOLVED_CUSTODY: dict[int, "ManagedEnginePreparation"] = {}


def _preparation_engine(name: str | None) -> Any:
    from astrid.core.contracts.errors import AstridValueError

    from .entries import engine_table

    name = name or "comfyui"
    engine = engine_table().get(name)
    if engine is None:
        raise AstridValueError(f"no generation.engine contribution for {name!r}")
    return copy.copy(engine)


def _retain_unresolved(preparation: "ManagedEnginePreparation") -> None:
    """Keep the owner of an unresolved lock alive until process exit."""
    _UNRESOLVED_CUSTODY[id(preparation)] = preparation


def _recipe_node_patches(recipe: EngineRecipe) -> dict[str, list[SourcePatch]]:
    """Resolve recipe patch source IDs to custom-node directory names."""
    comfy = next((s for s in recipe.sources if s.name == "comfyui"), None)
    custom_nodes = PurePosixPath(comfy.relative_path) / "custom_nodes" if comfy else None
    patches_by_pack: dict[str, list[SourcePatch]] = {}
    sources = {source.name: source for source in recipe.sources}
    for patch in recipe.source_patches:
        source = sources.get(patch.source)
        if source is None or custom_nodes is None:
            continue
        try:
            relative = PurePosixPath(source.relative_path).relative_to(custom_nodes)
        except ValueError:
            continue
        if len(relative.parts) == 1:
            patches_by_pack.setdefault(relative.name, []).append(patch)
    return patches_by_pack


def _write_managed_nodes_lock(path: Path, nodes: list[dict[str, str]], recipe: EngineRecipe) -> None:
    """Write the selected node pins and their recipe-owned source patches."""
    patches_by_pack = _recipe_node_patches(recipe)
    sources = {source.name: source for source in recipe.sources}
    lines = []
    for node in nodes:
        name = Path(urlsplit(node["repo"]).path).name.removesuffix(".git")
        if not name or any(c.isspace() or c in "/\\" for c in name):
            raise IntegrityError("managed node name is invalid")
        lines += [f"[nodepacks.{json.dumps(name)}]", f"url = {json.dumps(node['repo'])}",
                  f"commit = {json.dumps(node['commit'])}"]
        patches = patches_by_pack.get(name, ())
        pins = {(sources[patch.source].pin.repository, sources[patch.source].pin.revision) for patch in patches}
        if patches and pins != {(node["repo"], node["commit"])}:
            # A recipe patch is bound to its source pin; never transfer it to another repository or commit.
            raise IntegrityError(f"recipe source patches for node pack {name} are pinned to {sorted(pins)}, "
                                 f"but the workflow locks {node['repo']} at {node['commit']}")
        if patches:
            values = ", ".join(f"{{ path = {json.dumps(patch.path)}, sha256 = {json.dumps(patch.sha256_after)} }}"
                               for patch in patches)
            lines.append(f"patches = [{values}]")
    path.write_text("\n".join(lines) + "\n")


def native_failure_diagnostic(step: str, stdout: str, stderr: str) -> tuple[str, str]:
    """Reduce untrusted output to fixed labels; never forward a remote text tail."""
    step = step if step in _NATIVE_STEPS else "unknown"
    reason = {"runtime_inspect": "runtime_inspection_failed", "nodes_restore": "node_restore_failed",
              "pip_bootstrap": "pip_bootstrap_failed", "fetch": "model_fetch_failed",
              "prepare": "native_preparation_failed", "doctor": "workflow_diagnosis_failed"}.get(step, "command_failed")
    # VibeComfy doctor emits its diagnostic on stdout and returns 1. Only
    # recognize complete fixed headings, never its paths, errors or suggestions.
    if step == "doctor":
        headings = {"Layer: Python scratchpad import/build": "workflow_load_failed",
                    "Layer: Schema reconciliation": "schema_reconciliation_failed",
                    "Layer: VibeWorkflow validation": "workflow_validation_failed",
                    "Layer: Porting helper diagnostics": "workflow_helper_failed",
                    "Layer: nodepack lockfile drift": "nodepack_drift",
                    "Missing models:": "models_missing"}
        for line in stdout[-4096:].splitlines()[-32:]:
            if line in headings:
                reason = headings[line]
                break
    if any(line.startswith("ModuleNotFoundError: ") for line in stderr[-4096:].splitlines()[-32:]):
        reason = "dependency_missing"
    if step == "doctor":
        fixed = {"ValueError: managed doctor target schema differs from owned engine health": "owned_schema_changed",
                 "ValueError: managed doctor target schema is unavailable": "owned_schema_unavailable",
                 "ValueError: managed doctor target schema exceeds its bound": "owned_schema_too_large"}
        for line in stderr[-4096:].splitlines()[-32:]:
            if line in fixed:
                reason = fixed[line]
                break
    return step, reason


def native_failure_classes(stdout: str, reason: str) -> tuple[str, ...]:
    """Expose only public class IDs in the pinned dependency's shipped index."""
    if reason != "schema_reconciliation_failed":
        return ()
    try:
        # VibeComfy owns its object_info cache layout; read class ids through its API only.
        from vibecomfy.porting.object_info.consume import list_classes
        public = set(list_classes())
    except (OSError, ValueError, TypeError, ImportError):
        return ()
    prefix = "- workflow contains unresolved class types: "
    for line in stdout[-4096:].splitlines()[-32:]:
        if line.startswith(prefix):
            return tuple(sorted({name for name in line[len(prefix):].split(", ")
                                 if name in public and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name)})[:16])
    return ()


@contextmanager
def preparation_scope(report):
    """Project one trusted in-process machine attempt without global environment changes."""
    token = _PREPARATION_CONTEXT.set(json.dumps(report, sort_keys=True))
    try:
        yield
    finally:
        _PREPARATION_CONTEXT.reset(token)



def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def configured_models_root(engine: str = DEFAULT_ENGINE, *, root: str | None = None) -> str:
    """Read the installed cache binding without starting or contacting its owner.

    No receipt means the proposed default root, not a readiness assertion.
    Existing malformed/unsafe receipts fail closed, including on a cold install.
    """
    from ._engine_target_files import _TargetFiles
    from .engine_contracts import IntegrityError, effective_models_root
    from .engine_local_endpoint import DEFAULT_PLATFORM, default_engine_root
    from .engine_recipe import load_recipe
    from .engine_runner import _parse_receipt, plan_from_receipt, plan_install
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
    locator = plan_install(recipe, root=root, placement="local")
    receipt = _parse_receipt(raw, locator)
    plan_from_receipt(recipe, receipt, root=root, placement="local")
    return effective_models_root(root, receipt.models_root)


def read_preparation(*, task: str, attempt: str | None, endpoint: str,
                     execution_identity: str | None = None, inputs: Any = None) -> dict[str, Any] | None:
    """Read only the host-projected, attempt-bound preparation, never ambient policy."""
    import os
    raw = _PREPARATION_CONTEXT.get()
    if raw is None:
        raw = os.environ.get(PREPARATION_ENV)
    if raw is None:
        return None
    body = json.loads(raw)
    keys = {"schema", "task_id", "attempt_id", "execution_identity", "inputs", "manifest", "manifest_sha256",
            "installation", "endpoint", "output_dir", "placement", "mode", "alignment", "health"}
    if (not isinstance(body, dict) or set(body) not in (keys, keys | {"native_preparation"}) or body["schema"] != SCHEMA
            or body["task_id"] != task or body["attempt_id"] != attempt or body["endpoint"] != endpoint
            or body["placement"] != "managed" or body["mode"] not in {"align", "check", "trust"}
            or not isinstance(body["manifest"], dict) or digest(body["manifest"]) != body["manifest_sha256"]
            or not isinstance(body["alignment"], dict) or not isinstance(body["health"], dict)
            or body["health"].get("healthy") is not True
            or execution_identity is not None and body["execution_identity"] != execution_identity):
        raise ValueError("managed engine preparation differs from this attempt")
    from . import target_inventory
    from .engine_local_endpoint import _valid_root
    pod_alignment = getattr(target_inventory, "POD_ALIGN", None)
    expected_mode = (pod_alignment.get() if pod_alignment is not None else None) or os.environ.get("ASTRID_ALIGN", "check")
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
            or body["manifest"].get("engine") != DEFAULT_ENGINE
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


def judge_declarations(workflow, scratch, engine):
    """Render and judge the declarations without consulting installed state."""
    from vibecomfy.runtime.dependencies import runtime_requirements_from_workflow
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import (
        WorkflowBundleError,
        emit_bundle_with_candidate,
        load_bundle,
    )

    canonical_path = Path(scratch) / "canonical" / "workflow.py"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    for source_format in ("ready_template", "scratchpad"):
        try:
            emitted = emit_bundle_with_candidate(workflow.clone(), canonical_path,
                Provenance.USER_CONFIRMED, None, source_format=source_format)
            break
        except WorkflowBundleError:
            if source_format == "scratchpad":
                raise
    selected = load_bundle(emitted.python_path, trust=Provenance.USER_CONFIRMED)
    selected.require_canonical_authority("managed engine alignment")
    if runtime_requirements_from_workflow(selected.workflow) != runtime_requirements_from_workflow(workflow):
        raise IntegrityError("managed CLI runtime requirements differ from the admitted workflow")
    manifest = engine.workflow_requirements([workflow])
    if engine.workflow_requirements([selected.workflow]) != manifest:
        raise IntegrityError("managed CLI workflow requirements differ from the admitted workflow")
    missing = [model["name"] for model in manifest["models"]
               if not model.get("sha256") or model.get("size") is None]
    if missing:
        raise IntegrityError("managed model has no declared digest: " + ", ".join(missing))
    return manifest, source_format


class ManagedEnginePreparation:
    """One lock and one supervisor client, retained until the attempt has drained.

    The install lock excludes another host's preparation and installation while
    the HTTP worker runs. No worker receives this object or its control socket.
    """
    def __init__(self, *, task: str, attempt: str, mode: str, deadline: float, cancelled: Any, scratch: Path,
                 device: int | None = None, logs_root: Path | None = None, on_phase: Any = None):
        from .engine_execution import create_local_backend
        from .engine_local_endpoint import DEFAULT_PLATFORM, default_engine_root
        from .engine_recipe import load_recipe

        self.task, self.attempt, self.mode = task, attempt, mode
        self.device = device
        self.visibility = str(device) if device is not None else os.environ.get("CUDA_VISIBLE_DEVICES")
        self.scratch = scratch
        # The run root whose logs.json a failed step's whole output is declared in.
        self.logs_root = logs_root
        self.on_phase = on_phase
        self.deadline, self.cancelled = deadline, cancelled
        self.root = default_engine_root(DEFAULT_ENGINE)
        self.recipe = load_recipe(DEFAULT_ENGINE, DEFAULT_PLATFORM)
        self.context, self.adapter = create_local_backend(engine=DEFAULT_ENGINE, platform=DEFAULT_PLATFORM,
            placement="local", root=self.root, deadline=deadline)
        self.context.service_client.cancelled = cancelled
        self.lock = None
        self.report: dict[str, Any] | None = None
        self.uncertain = False
        self._preparing_engine = False

    def check(self):
        if self.cancelled():
            raise CancelledError("managed preparation cancelled")
        if time.monotonic() >= self.deadline:
            from .engine_contracts import DeadlineExceeded
            raise DeadlineExceeded("managed preparation deadline exceeded")

    def close(self, *, execution_root: Path | None = None, worker_drained: bool = True):
        if not worker_drained:
            self.uncertain = True
        try:
            if not self.uncertain:
                self._stop_preparing_engine()
            if not self.uncertain and execution_root is not None and self.report is not None:
                from .backends.vibecomfy_local_server import cleanup_prompts
                cleanup_prompts(self.report["endpoint"], execution_root, confirm=True)
        except BaseException:
            # A failed stop or failed prompt cleanup cannot establish that a
            # request has drained, regardless of which boundary raised it.
            self.uncertain = True
            _retain_unresolved(self)
            raise
        if self.uncertain:
            _retain_unresolved(self)
            from .engine_contracts import CustodyUnresolved
            raise CustodyUnresolved("managed preparation retains an unconfirmed command; keep installation fenced")
        if self.lock is not None:
            lock, self.lock = self.lock, None
            lock.__exit__(None, None, None)
        _UNRESOLVED_CUSTODY.pop(id(self), None)

    def prepare(self, workflow: Path, *, execution_identity: str, inputs: Any, canonical_workflow: Any, input_digests: Any) -> dict[str, Any]:
        from .engine_contracts import CustodyUnresolved
        try:
            report = self._prepare(workflow, execution_identity=execution_identity, inputs=inputs, canonical_workflow=canonical_workflow, input_digests=input_digests)
            self.check()
        except BaseException as error:
            if isinstance(error, CustodyUnresolved):
                self.uncertain = True
            if not self.uncertain:
                try:
                    self._stop_preparing_engine()
                except CustodyUnresolved as stop_error:
                    error.add_note(f"owned engine cleanup remains unconfirmed: {stop_error}")
            if self.uncertain:
                _retain_unresolved(self)
            raise
        # Only the completed report hands the live engine to the attempt. A
        # successful doctor still leaves all remaining preparation under custody.
        self._preparing_engine = False
        return report

    def _stop_preparing_engine(self):
        if not getattr(self, "_preparing_engine", False):
            return
        from .engine_contracts import CustodyUnresolved
        try:
            # This client stops only its authenticated supervisor's retained
            # handles and confirms all command/process/output drain before return.
            self.context.service_client.stop_owned(deadline=time.monotonic() + 5)
        except BaseException as stop_error:  # noqa: BLE001 - reported through the raised error or fallback; diagnostic at debug, never a user-facing traceback
            # The fail-safe comes first and never depends on the diagnostic.
            self.uncertain = True
            import logging
            logging.getLogger(__name__).debug("managed preparation owned-engine stop failed", exc_info=True)
            error = CustodyUnresolved("managed preparation failed; exact owned engine stop remains unconfirmed")
            error.add_note(f"owned engine stop failed: {type(stop_error).__name__}")
            raise error from None
        self._preparing_engine = False

    def _prepare(self, workflow: Path, *, execution_identity: str, inputs: Any, canonical_workflow: Any, input_digests: Any) -> dict[str, Any]:
        from .engine_contracts import IntegrityError, RecipeValidationError
        from .engine_local_endpoint import EngineNotInstalled
        from .engine_recipe import recipe_digest
        from .engine_runner import (
            _parse_receipt,
            _Runner,
            plan_from_receipt,
            plan_install,
            refresh_installed_launch,
            require_current_receipt,
        )

        self.check()
        staged_members = [workflow]
        if workflow.suffix == ".py":
            staged_members += [workflow.with_suffix(".vibe.json"), workflow.with_name("source.json")]
        staged_digests = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in staged_members}
        receipt_path = str(Path(self.root) / ".astrid-engine/install.json")

        def refuse_while_installing(holder_kind=None):
            # Never queue behind an installer; ordinary job/preparation
            # contention can wait for the current owner to finish.
            if holder_kind == "install":
                from .engine_contracts import EngineBusy
                raise EngineBusy("the local engine is being updated; try again when astrid engines install finishes")

        lock = self.adapter.local_install_lock(self.root, deadline=self.deadline, cancelled=self.cancelled,
                                               on_contention=refuse_while_installing)
        lock.__enter__()
        self.lock = lock
        self.check()
        try:
            raw = self.context.read_json(receipt_path, deadline=self.deadline)
        except RecipeValidationError as error:
            raise EngineNotInstalled(self.recipe.engine, "install receipt unreadable") from error
        locator = plan_install(self.recipe, root=self.root, placement="local")
        receipt = _parse_receipt(raw, locator)
        plan = plan_from_receipt(self.recipe, receipt, root=self.root, placement="local")
        if receipt.status != "installed":
            raise EngineNotInstalled(self.recipe.engine, f"install status {receipt.status}, engine not healthy")
        require_current_receipt(receipt, plan, self.recipe)
        runner = _Runner(plan, self.context, self.adapter, self.deadline, device=self.device, visibility=self.visibility)
        runner.step_id = "helper-identity"
        if runner.helper("identity") != {"helper_sha256": runner.helper_sha256}:
            raise IntegrityError("managed engine helper identity differs")
        # These checks read the saved environment identities; they do not
        # re-enter the install lock or require the HTTP service to be running.
        for step in plan.steps:
            if step.kind in {"source", "environment"}:
                runner.execute(step, readonly=True)
        if runner.locks != receipt.requirements_locks or runner.commits != receipt.source_commits:
            raise IntegrityError("managed installation evidence differs from its receipt")
        self._runtime_requirements = None
        self._guard_runtime(canonical_workflow, runner, post=False)
        engine = _preparation_engine(DEFAULT_ENGINE)
        manifest, source_format = judge_declarations(canonical_workflow, self.scratch, engine)
        self._native_generated_scratchpad = source_format == "scratchpad"
        canonical_path = self.scratch / "canonical" / "workflow.py"
        # CLI preparation owns only this emitted copy.
        native_members = list(canonical_path.parent.iterdir())
        self._canonical_members = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in native_members if path.is_file()}
        staged_digests.update(self._canonical_members)
        manifest = {**manifest, "engine": DEFAULT_ENGINE, "models_root": configured_models_root(root=self.root)}
        self._has_declared_nodes = bool((manifest.get("extra") or {}).get("nodes"))
        commands_type = type(engine).requirements.__globals__["VibeComfyCommands"]
        self.scratch.mkdir(parents=True, exist_ok=True)
        lockfile = self.scratch / "managed-custom-nodes.lock"
        _write_managed_nodes_lock(lockfile, (manifest.get("extra") or {}).get("nodes", []), self.recipe)
        if engine.commands is None:
            engine.commands = commands_type(str(canonical_path), str(lockfile), runner=self._command)
        self.check()
        self._alignment_runner = runner
        self._alignment_verification = (canonical_workflow, runner, staged_digests, manifest, receipt)
        if self.mode == "align":
            self._preflight_model_storage(manifest)
        callback = getattr(self, "on_phase", None)
        if callback is not None:
            callback("align")
        alignment = engine.align(manifest, self.mode, workflow=canonical_path)
        if alignment.get("status") == "missing":
            raise IntegrityError("managed engine is missing declared models: " + ", ".join(alignment["missing"]))
        self.check()
        self._verify_alignment(*self._alignment_verification)
        runner.step_id = "start"
        if callback is not None:
            callback("start_engine")
        start_action, start_facts = runner.start(readonly=False)
        self._preparing_engine = True
        self.check()
        runner.step_id = "health"
        health_action, health_facts = runner.check_health()
        self.check()
        receipt_sha256 = refresh_installed_launch(runner, raw, start_action=start_action, start_facts=start_facts,
                                                  health_action=health_action, health_facts=health_facts, check=self.check)
        self.check()
        health = {"healthy": True, "object_info_sha256": runner.health.object_info_sha256,
                  "required_nodes_present": runner.health.required_nodes_present}
        report: dict[str, Any] = {"schema": SCHEMA, "task_id": self.task, "attempt_id": self.attempt,
            "execution_identity": execution_identity, "inputs": dict(input_digests),
            "manifest": manifest, "manifest_sha256": digest(manifest),
            "installation": {"root": self.root, "receipt_sha256": receipt_sha256,
                "recipe_sha256": recipe_digest(self.recipe), "helper_sha256": runner.helper_sha256},
            "endpoint": f"http://127.0.0.1:{self.recipe.start.port}",
            "output_dir": str(Path(self.root) / next(s.relative_path for s in self.recipe.sources if s.name == "comfyui") / "output"),
            "placement": "managed", "mode": self.mode, "alignment": alignment, "health": health}
        self.report = report
        native_preparation: dict[str, Any] | None = getattr(self, "native_preparation", None)
        if native_preparation is not None:
            report["native_preparation"] = native_preparation
            if getattr(self, "pip_bootstrap", None) is not None:
                native_preparation["pip_bootstrap"] = self.pip_bootstrap
        return report

    def _verify_alignment(self, workflow, runner, staged_digests, manifest, receipt):
        """Attest mutation results before executing the engine, and again after doctor."""
        from .engine_contracts import IntegrityError
        self.check()
        if any(path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected
               for path, expected in staged_digests.items()):
            raise IntegrityError("managed alignment changed the canonical workflow bytes")
        if self.mode != "trust":
            self._verify_models(manifest)
        self._guard_runtime(workflow, runner, post=True)
        for step in runner.plan.steps:
            if step.kind in {"source", "environment"}:
                runner.execute(step, readonly=True)
        if runner.locks != receipt.requirements_locks or runner.commits != receipt.source_commits:
            raise IntegrityError("managed alignment changed the installed base")
        self.check()

    def _verify_models(self, manifest):
        from ._engine_target_files import _TargetFiles
        from .engine_contracts import IntegrityError
        files = _TargetFiles(manifest["models_root"])
        for model in manifest["models"]:
            self.check()
            path = str(Path(manifest["models_root"]) / model["subdir"] / model["name"])
            fact = files.file_fact(path, hash_bytes=True, deadline=self.deadline, check=self.check)
            expected = str(model.get("sha256") or "").removeprefix("sha256:")
            if (not fact.exists or not fact.regular or fact.symlink
                    or fact.bytes != model["size"] or not re.fullmatch(r"[0-9a-f]{64}", expected)
                    or fact.sha256 != expected):
                raise IntegrityError("managed model bytes differ from the declared digest: " + model["name"])

    def _preflight_model_storage(self, manifest):
        """Budget native VibeComfy fetch's full writes; that path ignores `.part`."""
        from ._engine_target_files import _TargetFiles
        from .model_storage import (
            DECLARED_VOLUME,
            STORAGE_WARNING,
            ModelStorageInsufficient,
            available_model_bytes,
            measure_declared_volume,
        )

        root = Path(manifest["models_root"])
        self._storage_models_root = root
        files = _TargetFiles(str(root))
        demand, candidates = {}, []
        for model in manifest["models"]:
            self.check()
            path = root / model["subdir"] / model["name"]
            fact = files.file_fact(str(path), hash_bytes=False, deadline=self.deadline)
            present = fact.exists and fact.regular and not fact.symlink and fact.bytes == model["size"]
            candidates.append((model, path, present))
        if all(present for _, _, present in candidates):
            # align does not fetch when every file has the expected size. Its
            # existing post-alignment digest verification still runs; avoid an
            # extra full-volume hash pass when no download is planned.
            return
        for model, path, present in candidates:
            self.check()
            if present:
                fact = files.file_fact(str(path), hash_bytes=True, deadline=self.deadline, check=self.check)
                if fact.sha256 == str(model["sha256"]).removeprefix("sha256:"):
                    continue
            directory = path.parent.resolve(strict=False)
            demand[directory] = demand.get(directory, 0) + model["size"]
        if demand:
            declared = DECLARED_VOLUME.get()
            volume = (measure_declared_volume(declared, root, deadline=self.deadline, check=self.check)
                      if declared is not None else None)
            warn = declared is not None and volume is None
            for directory, needed in demand.items():
                free, unknown = available_model_bytes(directory, deadline=self.deadline, check=self.check)
                if volume is not None and volume.covers(directory):
                    free = min(free, volume.free_bytes) if free is not None else volume.free_bytes
                warn |= unknown
                if free is not None and needed > free:
                    self._declare_storage_inventory()
                    raise ModelStorageInsufficient(needed, free, getattr(self, "task", "<task>"))
            if warn:
                from types import SimpleNamespace
                self._keep_step_output("storage", SimpleNamespace(stdout="", stderr=STORAGE_WARNING + "\n"))

    def _declare_storage_inventory(self) -> bool:
        """Best-effort diagnostics must preserve the failure, including its text."""
        from .model_storage import declare_storage_inventory

        logs_root = getattr(self, "logs_root", None)
        if logs_root is None:
            return False
        try:
            root = getattr(self, "_storage_models_root", None)
            if root is None:
                root = Path(configured_models_root(root=self.root))
            declare_storage_inventory(logs_root, root, deadline=self.deadline, cancelled=self.cancelled)
            return True
        except Exception:  # noqa: BLE001 - inventory must never mask the original storage failure
            return False

    def _guard_runtime(self, workflow, runner, *, post):
        """Refuse changes to the receipt-owned base before native synchronization."""
        from packaging.requirements import Requirement
        from vibecomfy.runtime.dependencies import runtime_requirements_from_workflow

        from .engine_contracts import IntegrityError
        requirements = runtime_requirements_from_workflow(workflow)
        self._runtime_requirements = requirements
        self._runtime_launch_flags = list(runner.launch_spec()["argv"])
        self._sync_packages = []
        primary = next(environment for environment in self.recipe.environments if environment.name == self.recipe.primary_environment)
        specification = runner.helper("environment-spec", environment=primary.name)["expected"]
        base_packages = {Requirement(text).name.lower().replace("_", "-") for text in specification["requirements"]}
        base_packages.update(specification["wheels"])
        base_packages.update(Requirement(text).name.lower().replace("_", "-") for text in specification["constraints"])
        self._protected_packages = sorted(base_packages)
        if requirements is None:
            return
        source = next(source for source in self.recipe.sources if source.name == "comfyui")
        if (requirements.comfy_commit and requirements.comfy_commit != source.pin.revision
                or requirements.comfy_source and requirements.comfy_source != source.pin.repository
                or requirements.comfy_ref and requirements.comfy_ref != source.pin.revision
                or requirements.extras):
            raise IntegrityError("managed workflow runtime conflicts with the installed recipe")
        self._sync_packages = [name for name, _ in requirements.packages if name.lower().replace("_", "-") not in base_packages]
        program = """import ast,json,sys
from vibecomfy.runtime.dependencies import inspect_runtime_target,compare_runtime
requirements=json.loads(sys.argv[1])
actual=inspect_runtime_target(runtime_root=sys.argv[2],python_executable=sys.executable,package_names=list(requirements.get('packages',{})))
from pathlib import Path
version_file=Path(sys.argv[2])/'ComfyUI'/'comfyui_version.py'
if actual.get('comfy_version') is None and version_file.is_file() and not version_file.is_symlink():
 for statement in ast.parse(version_file.read_text()).body:
  if isinstance(statement,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='__version__' for target in statement.targets):
   value=ast.literal_eval(statement.value)
   if isinstance(value,str):actual['comfy_version']=value
actual['launch_flags']=json.loads(sys.argv[3]);actual['managed']=True
print(json.dumps(compare_runtime(requirements,target=actual)))
"""
        report = json.loads(self._native(program, json.dumps(requirements.to_dict()), self.root + "/runtime",
                                         json.dumps(self._runtime_launch_flags)).stdout)
        checks = report.get("checks")
        if not isinstance(checks, list):
            raise IntegrityError("managed runtime inspection returned no dependency predicates")
        for check in checks:
            path = check["path"]
            if path in {"models", "custom_nodes"} or path.startswith(("models.", "custom_nodes.")):
                continue  # The manifest alignment and target schema own these rows.
            package = path.removeprefix("packages.").lower().replace("_", "-") if path.startswith("packages.") else None
            if check["status"] != "matching" and (post or package is None or package in base_packages):
                raise IntegrityError("managed runtime requirement is not satisfied by the installed base: " + path)

    def _native(self, program, *args, step="runtime_inspect"):
        from .engine_contracts import (
            CommandRequest,
            DeadlineExceeded,
            IntegrityError,
            effective_models_root,
        )
        self.check()
        primary = next(e for e in self.recipe.environments if e.name == self.recipe.primary_environment)
        python = self.adapter.venv_python(self.adapter.join(self.root, primary.relative_path))
        comfy = self.adapter.join(self.root, next(s.relative_path for s in self.recipe.sources if s.name == "comfyui"))
        from .engine_custody import _installed_models_root
        models = effective_models_root(self.root, _installed_models_root(self.root, self.recipe))
        env = {"COMFYUI_PATH": comfy, "VIBECOMFY_MODELS_ROOT": models,
               "VIBECOMFY_CUSTOM_NODES_DIR": comfy + "/custom_nodes", "VIBECOMFY_HEADLESS": "1",
               "VIBECOMFY_COMFY_CONFIGURATION": json.dumps({"runtime_root": self.root + "/runtime",
                   "comfy_root": comfy, "models_root": models, "python": python}),
               "PATH": str(Path(python).parent) + ":/usr/local/bin:/usr/bin:/bin",
               "VIBECOMFY_PROTECTED_RUNTIME_PACKAGES": ",".join(getattr(self, "_protected_packages", ()))}
        result = self.context.run(CommandRequest((python, "-I", "-c", program, *args), comfy, env, self.deadline))
        if result.timed_out:
            raise DeadlineExceeded("managed alignment command timed out")
        if result.exit_code != 0:
            import logging
            step, reason = native_failure_diagnostic(step, result.stdout, result.stderr)
            classes = native_failure_classes(result.stdout, reason)
            logging.getLogger(__name__).error(
                "managed alignment failed task=%s attempt=%s step=%s reason=%s missing_classes=%s exit=%s stdout_sha256=%s stderr_sha256=%s",
                self.task, self.attempt, step, reason, json.dumps(classes), result.exit_code,
                hashlib.sha256(result.stdout.encode()).hexdigest(),
                hashlib.sha256(result.stderr.encode()).hexdigest())
            detail = f", missing_classes={json.dumps(classes)}" if classes else ""
            where = ("its whole output is the run logs preparation.{0}.stdout and preparation.{0}.stderr"
                     .format(step) if self._keep_step_output(step, result) else "inspect the host log for this attempt")
            error = IntegrityError(f"managed alignment command failed (step={step}, reason={reason}{detail}); {where}")
            if step == "fetch" and any(re.search(
                    r"\[Errno (?:28|122)\]|\b(?:EDQUOT|ENOSPC)\b|Disk quota exceeded|No space left on device",
                    text, re.IGNORECASE) for text in (result.stdout, result.stderr)):
                setattr(error, "storage_exhausted", True)
            raise error
        # A successful step's output is evidence too (for example restore's "declared patches verified").
        self._keep_step_output(step, result)
        self.check()
        return result

    def _keep_step_output(self, step, result) -> bool:
        """Keep a step's whole stdout and stderr as declared run logs, on success and failure; never raises."""
        logs_root = getattr(self, "logs_root", None)
        if logs_root is None:
            return False
        try:
            from astrid.core._shared import run_logs

            folder = Path(logs_root) / ".preparation-logs"
            folder.mkdir(parents=True, exist_ok=True)
            for stream in ("stdout", "stderr"):
                path = folder / f"{step}.{stream}.log"
                path.write_text(getattr(result, stream) or "", encoding="utf-8")
                run_logs.declare(logs_root, run_logs.LogDecl(f"preparation.{step}.{stream}", stream, str(path)))
            return True
        except Exception:  # noqa: BLE001 - keeping logs must never change the step's failure
            return False

    def _command(self, argv):
        """Run the pinned CLI in its verified installation, through host custody."""
        from .engine_contracts import IntegrityError
        if any(path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected
               for path, expected in getattr(self, "_canonical_members", {}).items()):
            raise IntegrityError("managed CLI canonical capsule changed before command")
        if argv[0] == "nodes" and self.mode == "align" and getattr(self, "_has_declared_nodes", False):
            from astrid.packs.vibecomfy.native_preparation import native_pip_program
            primary = next(e for e in self.recipe.environments if e.name == self.recipe.primary_environment)
            python = self.adapter.venv_python(self.adapter.join(self.root, primary.relative_path))
            self.pip_bootstrap = json.loads(self._native(native_pip_program(), json.dumps({"python_executable": python}),
                                                        step="pip_bootstrap").stdout)
        program = "import sys;from vibecomfy.cli import main;sys.exit(main(sys.argv[1:]))"
        if argv[0] in {"prepare", "doctor", "fetch"}:
            # This is the host's own canonical renderer output, never admitted
            # Python. Its exact bytes are checked before and after the command.
            argv = [*argv, "--yes"]
        if argv[0] == "doctor":
            return self._doctor(argv)
        if argv[0] == "prepare":
            from astrid.packs.vibecomfy.native_preparation import native_preparation_program
            primary = next(e for e in self.recipe.environments if e.name == self.recipe.primary_environment)
            request = {"workflow_path": argv[1], "runtime_root": self.root + "/runtime",
                "python_executable": self.adapter.venv_python(self.adapter.join(self.root, primary.relative_path)),
                "uv_executable": self.adapter.uv_executable(self.adapter.join(self.root, "runtime")),
                "launch_flags": self._runtime_launch_flags,
                "capsule_sha256": {str(path): expected for path, expected in self._canonical_members.items()},
                "expected_requirements": self._runtime_requirements.to_dict() if self._runtime_requirements else None,
                "sync_packages": self._sync_packages}
            self.native_preparation = json.loads(self._native(native_preparation_program(), json.dumps(request),
                                                              step="prepare").stdout)
        else:
            step = "nodes_restore" if argv[0] == "nodes" else argv[0]
            try:
                self._native(program, *argv, step=step)
            except (OSError, IntegrityError) as error:
                if step == "fetch" and (getattr(error, "errno", None) in {errno.EDQUOT, errno.ENOSPC}
                                        or getattr(error, "storage_exhausted", False)):
                    if self._declare_storage_inventory():
                        error.add_note(f"see `astrid tasks logs {self.task}` (storage.inventory).")
                raise

    def _doctor(self, argv):
        from astrid.packs.vibecomfy.native_preparation import native_doctor_program

        from .engine_contracts import IntegrityError
        runner = self._alignment_runner
        # align has already run nodes/fetch/prepare. Re-attest their results
        # before the first launch can execute the installed ComfyUI or venv.
        self._verify_alignment(*self._alignment_verification)
        try:
            self.check()
            runner.step_id = "doctor-start"
            runner.start(readonly=False)
            # A refused foreign owner or mismatched launch never authorizes
            # stop. Uncertain start responses retain the fence in prepare().
            self._preparing_engine = True
            runner.step_id = "doctor-health"
            runner.check_health()
            expected = runner.health.object_info_sha256
            request = {"argv": argv, "endpoint": f"http://127.0.0.1:{self.recipe.start.port}",
                       "expected_schema_sha256": expected,
                       "request_timeout": min(20.0, max(0.001, self.deadline - time.monotonic()))}
            result = self._native(native_doctor_program(), json.dumps(request), step="doctor")
            self.check()
            runner.step_id = "doctor-post-health"
            runner.check_health()
            if runner.health.object_info_sha256 != expected:
                raise IntegrityError("managed doctor target schema changed during diagnosis")
            return result
        except BaseException:
            self._stop_preparing_engine()
            raise
