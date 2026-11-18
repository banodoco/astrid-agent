"""VibeComfy state retained by the host's admitted resident incarnation.

This is a pack cache, not an admission or process owner. Captured pack bytes,
Runtime fences, installation custody and shutdown belong to the existing host.
Only closed declarative workflows enter this interpreter; jobs own all values,
attempt clients, cancellation, receipts and output paths.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

_LIMIT = 16
_SESSION = None
_SESSION_LOCK = threading.Lock()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _shape(values):
    from vibecomfy.prepared import _value_shape
    return _value_shape(values)


def _closed_python(path):
    # The pinned prepared-workflow authority checks the AST before load_bundle
    # can execute it. Reuse its exact closed-builder policy, not a second list.
    from vibecomfy.prepared import PreparedWorkflowUnavailable, _source_closure
    try:
        _source_closure(SimpleNamespace(python_path=Path(path)))
    except (PreparedWorkflowUnavailable, OSError, ValueError):
        return False
    return True


def _eligible(inputs):
    if (inputs.get("entry_inputs") or inputs.get("source_video")
            or inputs.get("readiness_profile_path") not in (None, "", "-")
            or inputs.get("after_outputs_job") not in (None, "", "{}")):
        return False
    if inputs.get("python"):
        return _closed_python(inputs["python"])
    source = Path(str(inputs.get("workflow") or ""))
    if source.suffix == ".py":
        return _closed_python(source)
    try:
        document = json.loads(source.read_bytes())
    except (OSError, ValueError):
        return False
    # Ready-template selection can execute a registry builder. The canonical
    # Python/companion path is eligible after its closed-source check instead.
    return isinstance(document, dict) and isinstance(document.get("nodes"), list) and "template_id" not in document


class CanonicalCatalogue:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        self.loaded = OrderedDict()

    def load(self, path, *, url, generation=""):
        from .production_engine import (
            _workflow_content_digest,
            load_workflow_path,
            workflow_members,
        )
        identity = _workflow_content_digest(path)
        key = _digest([identity, url, generation])
        if key in self.loaded:
            self.loaded.move_to_end(key)
            return self.loaded[key]
        if len(self.loaded) >= _LIMIT:
            # Bounded incarnation. Do not evict source paths while another job
            # may still be reading its canonical bundle.
            raise ValueError("resident workflow catalogue is full; start a fresh session")
        root = self.root / key
        root.mkdir()
        source = root / ("workflow.py" if Path(path).suffix == ".py" else "workflow.json")
        for name, (member, required) in workflow_members(Path(path)).items():
            if not required and not member.exists():
                continue
            target = {"python": source, "companion": source.with_suffix(".vibe.json"),
                      "source": source.with_name("source.json"), "workflow": source}[name]
            shutil.copyfile(member, target)
        if _workflow_content_digest(source) != identity:
            raise ValueError("workflow source changed during resident capture")
        loaded = load_workflow_path(source, root / "canonical", comfy_url=url)
        self.loaded[key] = loaded
        return loaded

    def close(self):
        self.loaded.clear()
        shutil.rmtree(self.root)


class HostPreparationCache(CanonicalCatalogue):
    """Parent-side canonical facts; linked to the same resident adapter owner."""
    def __init__(self, root, scope, compiler_generation):
        super().__init__(root)
        self.scope = scope
        self.prepared = {}
        self.last_loaded = None
        self.managed = None
        self.compiler_generation = compiler_generation
        self.invalidated = False
        self._preserve_catalogue = False

    def invalidate(self):
        # A completed failure does not revoke another live job. The next
        # admission must retire the quiescent incarnation and prepare afresh.
        self.invalidated = True

    def assert_current(self):
        from .resident_source import compiler_content_identity
        if compiler_content_identity() != self.compiler_generation:
            raise ValueError("resident compiler source generation changed")

    def load(self, path, *, url, generation=""):
        self.last_loaded = super().load(path, url=url, generation=generation)
        return self.last_loaded

    def can_retain(self, inputs, *, align):
        """Prove freezability before dispatch; no custom code joins on failure."""
        from vibecomfy.prepared import PreparedWorkflowContext
        from vibecomfy.schema.cache import object_info_payload_checksum

        from astrid.core.generation.target_inventory import declared_rows

        from .local_server_engine import (
            _live_preflight,
            local_server_bundle,
            prepare_local_workflow,
        )
        from .production_engine import _canonical_bundle_value, _validate_workflow_input_bindings

        loaded = self.last_loaded
        values = json.loads(inputs.get("workflow_inputs") or "{}")
        key = _digest([loaded.workflow_content_digest, inputs["comfy_url"], align, _shape(values)])
        if key in self.prepared:
            return self.prepared[key]
        bundle = local_server_bundle(loaded.resolved, self.root / ("admit-" + key),
                                     allow_empty_image="image" in values)
        bundle = _canonical_bundle_value(bundle)
        values = _validate_workflow_input_bindings(bundle, values)
        provider = _live_preflight(bundle, values, inputs["comfy_url"], self.root,
                                   target_custody=declared_rows(bundle.workflow) if align == "trust" else None,
                                   prepared_models=declared_rows(bundle.workflow) if self.managed is not None else None)
        context = PreparedWorkflowContext(loaded.workflow_content_digest, self.scope, self.scope, self.scope,
            self.scope, inputs["comfy_url"], "parent-admission", object_info_payload_checksum(provider.object_info()), "pinned")
        held = prepare_local_workflow(bundle, values, provider, context=context, executable_hooks=False)
        self.prepared[key] = held is not None
        if held is not None:
            held.session.invalidate()
        return self.prepared[key]

    def close(self):
        if self.managed is not None:
            self.managed.close()
            self.managed = None
        if not self._preserve_catalogue:
            super().close()

    def retire_managed(self, host, *, clear_catalogue):
        """Retire the existing owner before reacquiring its installation lock."""
        from astrid.core.execution.managed_tool_session import SessionCapacityError
        adapter = host.managed_tool_session.current_adapter
        if getattr(adapter, "has_jobs", False):
            raise SessionCapacityError("managed generation replacement is pending active admissions")
        # Keep this request's immutable workflow capture reachable across the
        # owner replacement. Its old preparation cannot survive the release.
        self._preserve_catalogue = True
        try:
            if host.managed_tool_session.occupied:
                host.managed_tool_session.release(reason="managed_readiness_changed")
            # The owning adapter normally closes this resource after native
            # drain. An unattached cache still closes only after that release.
            if self.managed is not None:
                self.managed.close()
                self.managed = None
        finally:
            self._preserve_catalogue = False
        getattr(adapter, "vibecomfy_preparations", {}).pop(self.scope, None)
        self.prepared.clear()
        if clear_catalogue:
            for child in self.root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
            self.loaded.clear()
            self.last_loaded = None
        self.invalidated = False

    def observe_managed(self, host, preparation):
        adapter = host.managed_tool_session.current_adapter
        managed = self.managed
        other_managed = any(getattr(cache, "managed", None) is not None and cache is not self
                            for cache in getattr(adapter, "vibecomfy_preparations", {}).values())
        if other_managed or managed is not None and not managed.observe(preparation):
            self.retire_managed(host, clear_catalogue=True)


def host_cache(host, record, task, inputs, *, align, entry):
    """Select cached facts only after the host captured and checked the source."""
    if not _eligible(inputs) or inputs.get("comfy_url") and align == "align":
        return None
    from .resident_source import compiler_content_identity
    compiler_generation = compiler_content_identity()
    from astrid.core.execution.generic_host import _admitted_task_spec
    scope = _digest([record.capability_digest, record.source_digest, record.dependency_digest,
                     task.get("project_id"), task.get("realm_id"), task.get("runtime_epoch"),
                     _admitted_task_spec(task).get("authority_context", task.get("spec", {}).get("authority_context")), align, entry,
                     inputs.get("comfy_url"), inputs.get("comfy_output_dir"), compiler_generation])
    adapter = host.managed_tool_session.current_adapter
    caches = getattr(adapter, "vibecomfy_preparations", {})
    existing = caches.get(scope)
    if existing is not None and existing.scope == scope and host.managed_tool_session.active:
        adapter.assert_current()
        if existing.invalidated:
            existing.retire_managed(host, clear_catalogue=True)
        return existing
    if len(caches) >= _LIMIT:
        raise ValueError("resident preparation capacity exceeded")
    if entry and entry != "workflow":
        from astrid.core.generation import entries
        described = entries.find_entry(entry)
        if described is None or described.get("hooks_declared"):
            return None
    from runtime_protocol import platform

    from astrid.sdk.storage_root import resolve_runtime_data_root
    area = resolve_runtime_data_root() / "runtime" / "pack-preparations"
    platform.private_directory(area)
    return HostPreparationCache(area / uuid.uuid4().hex, scope, compiler_generation)


def current_session():
    from astrid.core.execution.resident_sessions import _JOB
    job = _JOB.get()
    if job is None or not job.session_identity:
        return None
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            from astrid.core.execution.resident_worker_context import on_session_close
            _SESSION = VibeComfySession(Path.cwd() / "vibecomfy")
            on_session_close(_SESSION.close)
        return _SESSION


def run_cancellable(*args, **kwargs):
    """Cancellation belongs to this attempt's async task, never process signals."""
    import asyncio

    from vibecomfy.runtime.run import run

    from astrid.sdk import current_pack_job
    job = current_pack_job()

    async def execute():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        def cancel():
            loop.call_soon_threadsafe(task.cancel)
        unsubscribe = job.on_cancel(cancel)
        try:
            if job.cancelled():
                raise asyncio.CancelledError()
            return await run(*args, **kwargs)
        finally:
            unsubscribe()
    return asyncio.run(execute())


class VibeComfySession(CanonicalCatalogue):
    """One captured catalogue, target pair and bounded prepared-template map."""
    def __init__(self, root):
        super().__init__(root)
        from vibecomfy.runtime import session as runtime_session

        from .resident_source import compiler_content_identity
        self.compiler_generation = compiler_content_identity()
        # The pinned producer's metadata helper probes the ambient cwd on
        # every receipt. This interpreter has one fixed private cwd for its
        # entire lifetime. Resolve that advisory value once; actual compiler
        # and source authority remains content-checked for every job.
        self._git_sha = runtime_session._git_sha
        revision = self._git_sha()
        self._revision = lambda: revision
        runtime_session._git_sha = self._revision
        self.lock = threading.RLock()
        self.targets = {}
        self.templates = {}
        self.requirements = {}

    def entry_manifest(self, entry, values, source):
        if not entry or entry == "workflow":
            return None
        from .actions.run.run import _entry_manifest
        from .production_engine import _workflow_content_digest
        key = _digest([entry, _workflow_content_digest(source), _shape(values)])
        with self.lock:
            if key not in self.requirements:
                self.requirements[key] = _entry_manifest(entry, values, workflow_path=source)
            return copy.deepcopy(self.requirements[key])

    def run(self, path, destination, *, comfy_url, comfy_output_dir="", task_identity,
            attempt_identity=None, expected_execution_identity=None, workflow_input_bindings=None):
        from vibecomfy.prepared import PreparedWorkflowContext
        from vibecomfy.schema.cache import object_info_payload_checksum

        from astrid.core.generation.backends.comfy_held import ComfyTargetSession
        from astrid.core.generation.backends.comfy_target import ComfyTarget
        from astrid.core.generation.entries import prepare_execution_bindings
        from astrid.core.generation.managed_preparation import read_preparation
        from astrid.core.generation.target_inventory import declared_rows
        from astrid.sdk import current_pack_job

        from .local_server_engine import (
            _live_preflight,
            local_server_bundle,
            prepare_local_workflow,
            run_local_server,
        )
        from .production_engine import (
            _canonical_bundle_value,
            _validate_workflow_input_bindings,
            loaded_workflow_execution_identity,
        )

        job = current_pack_job()
        from .resident_source import compiler_content_identity
        if compiler_content_identity() != self.compiler_generation:
            raise ValueError("resident compiler generation changed before dispatch")
        # Only preparation and lookup are serial. The GPU run, receipt and
        # delivery below never hold this lock.
        with self.lock:
            session = self.targets.get(comfy_url)
            if session is None:
                if len(self.targets) >= 2:
                    raise ValueError("resident target capacity exceeded")
                session = self.targets[comfy_url] = ComfyTargetSession(ComfyTarget(comfy_url, "attached"))
            client = session.client()
            generation = client.generation.identity if client.generation is not None else uuid.uuid4().hex
            loaded = self.load(path, url=comfy_url, generation=generation)
            if (expected_execution_identity is not None
                    and loaded_workflow_execution_identity(loaded, None) != expected_execution_identity):
                raise ValueError("workflow execution identity changed before resident dispatch")
            shape = _digest([loaded.workflow_content_digest, generation,
                             job.settings.get("align", "check"), job.settings.get("entry"),
                             _shape(dict(workflow_input_bindings or {}))])
            row = self.templates.get(shape)
            if row is None:
                bundle = local_server_bundle(loaded.resolved, self.root / ("template-" + shape),
                    allow_empty_image="image" in (workflow_input_bindings or {}))
            else:
                bundle = row[0]
            bundle = _canonical_bundle_value(bundle)
            bindings = _validate_workflow_input_bindings(bundle, workflow_input_bindings)
            bindings = prepare_execution_bindings(bundle.workflow, bindings, saved_seed_controls=loaded.saved_seed_controls)
            if row is None:
                prepared = read_preparation(task=task_identity, attempt=attempt_identity, endpoint=comfy_url)
                provider = _live_preflight(bundle, bindings, comfy_url, destination, client=client,
                    target_custody=declared_rows(bundle.workflow) if job.settings.get("align") == "trust" else None,
                    prepared_models=declared_rows(bundle.workflow) if prepared is not None else None)
                scope = _digest(job.session_identity)
                context = PreparedWorkflowContext(loaded.workflow_content_digest, job.capability_identity,
                    scope, _digest(dict(job.settings.get("managed_engine_preparation", {}).get("installation", {}))
                                   | {"align": job.settings.get("align", "check")}), scope,
                    comfy_url, generation, object_info_payload_checksum(provider.object_info()), "vibecomfy.prepared.v1")
                held = prepare_local_workflow(bundle, bindings, provider, context=context, executable_hooks=False)
                if held is None:
                    raise ValueError("workflow no longer satisfies its resident preparation admission")
                if len(self.templates) >= _LIMIT:
                    raise ValueError("resident template capacity exceeded")
                row = self.templates[shape] = (bundle, held, context)
            bundle, held, context = row
        return run_local_server(bundle, destination, comfy_url=comfy_url, comfy_output_dir=comfy_output_dir,
            task_identity=task_identity, attempt_identity=attempt_identity, run_inputs=bindings,
            workflow_conversion_object_info_sha256=loaded.workflow_conversion_object_info_sha256,
            prepared_workflow=held, prepared_context=context, retained_session=session)

    def close(self):
        from vibecomfy.runtime import session as runtime_session
        if runtime_session._git_sha is self._revision:
            runtime_session._git_sha = self._git_sha
        for _bundle, held, _context in self.templates.values():
            held.session.invalidate()
        self.templates.clear()
        for session in self.targets.values():
            session.close()
        self.targets.clear()
        super().close()
