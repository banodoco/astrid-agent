"""`local_server` profile: run a workflow on the user's already-running ComfyUI.

Split from production_engine so the attach-only path (live preflight, private
save prefixes, image upload, owned-prompt cleanup) reads as one unit.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runtime_protocol import platform

from astrid.packs.vibecomfy.production_engine import (
    ProductionEngineError,
    ProductionRunResult,
    _canonical_bundle_value,
    _result_from_runtime_result,
    _workflow_comfy_configuration,
)

_LOCAL_PROMPT_TIMEOUT_SEC = 3300  # leave containment time below the host's 3600s deadline


class _UploadedMediaSchema:
    """Live schema view whose managed media file pickers accept uploaded names.

    A managed image, mask, video or audio value is a staged basename, then an uploaded
    ``astrid/<task>/<name>`` reference; neither can appear in the picker's
    choices read before the upload. Everything else is delegated unchanged, and only
    the classes and fields that managed media public inputs name are exempt.
    """

    def __init__(self, provider: Any, targets: set[tuple[str, str]]) -> None:
        self._provider = provider
        self._targets = targets

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def get_schema(self, class_type: str) -> Any:
        schema = self._provider.get_schema(class_type)
        fields = {field for kind, field in self._targets if kind == class_type}
        if schema is None or not fields.intersection(schema.inputs):
            return schema
        inputs = dict(schema.inputs)
        for field in fields.intersection(inputs):
            inputs[field] = dataclasses.replace(inputs[field], choices=None, unresolved_choices=True)
        return dataclasses.replace(schema, inputs=inputs)


def _uploaded_media_targets(bundle: Any, run_inputs: Mapping[str, Any] | None) -> set[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    for name in run_inputs or {}:
        spec = bundle.workflow.inputs.get(name)
        media = str(getattr(spec, "media_semantics", "") or "").lower()
        node = bundle.workflow.nodes.get(str(getattr(spec, "node_id", ""))) if spec is not None else None
        if media in {"image", "mask", "video", "audio"} and node is not None:
            targets.add((node.class_type, str(spec.field)))
    return targets


def _live_preflight(bundle: Any, run_inputs: Mapping[str, Any] | None, url: str, destination: Path, *,
                    client: Any = None, target_custody: Any = None, prepared_models: Any = None) -> Any:
    """Read the live schema, without preparing or installing dependencies."""
    from vibecomfy.node_packs import missing_packs_for_workflow, resolve_node_packs
    from astrid.core.generation.backends.comfy_schema import TargetSchemaProvider
    from vibecomfy.schema.validate import (
        _choice_scope,
        validate_api_against_schema,
        validate_api_link_shapes,
    )

    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient

    client = client if client is not None else ComfyTargetClient(ComfyTarget(url, "attached"))
    targets = _uploaded_media_targets(bundle, run_inputs)

    class UploadedTargetSchema(TargetSchemaProvider):
        def get_schema(self, class_type: str) -> Any:
            schema = super().get_schema(class_type)
            fields = {field for kind, field in targets if kind == class_type}
            if schema is None or not fields.intersection(schema.inputs):
                return schema
            inputs = dict(schema.inputs)
            for field in fields.intersection(inputs):
                inputs[field] = dataclasses.replace(inputs[field], choices=None, unresolved_choices=True)
            return dataclasses.replace(schema, inputs=inputs)

    def read_object_info():
        observed = client.object_info()
        if not isinstance(observed, Mapping):
            raise ProductionEngineError('ComfyUI object_info is not an object')
        for node in bundle.workflow.runtime_nodes().values():
            if node.class_type not in observed:
                continue  # The missing-node report below owns this case.
            row = observed[node.class_type]
            inputs = row.get('input', row.get('inputs')) if isinstance(row, Mapping) else None
            outputs = row.get('output', row.get('outputs')) if isinstance(row, Mapping) else None
            if (not isinstance(inputs, Mapping) or not isinstance(outputs, list)
                    or any(group in inputs and not isinstance(inputs[group], Mapping)
                           for group in ('required', 'optional'))):
                raise ProductionEngineError(f'invalid live schema for {node.class_type}')
        if target_custody is not None:
            from astrid.core.generation.target_inventory import trust_declared_loader_choices
            return trust_declared_loader_choices(observed, bundle.workflow)
        return observed

    custody = {}
    if prepared_models is not None:
        custody = {"auxiliary_model_custody": "target", "target_model_assets": list(prepared_models)}
    if target_custody is not None:
        # Explicitly trusted declarations; approval skips local byte checks, without asserting verification.
        custody = {"auxiliary_model_custody": "target", "target_model_assets": list(target_custody)}
    provider = UploadedTargetSchema.from_object_info(
        read_object_info(), server_url=url, model_availability="target",
        refresh_object_info=read_object_info, **custody,
    )
    if target_custody is not None:
        provider.trust_model_assets(target_custody)
    # Force a fresh read, including an empty registry (which VibeComfy's
    # general validator treats as an unavailable, skippable schema).
    provider.object_info()
    missing_nodes = sorted({
        node.class_type for node in bundle.workflow.runtime_nodes().values()
        if provider.get_schema(node.class_type) is None
    })
    # The pinned projection compiler consults per-node socket schemas even
    # when bundle approval receives an explicit provider. Capture the selected
    # target's schemas as transient metadata; graph values/revision stay sealed.
    # Missing classes must fail here, before ambient/on-demand authoring lookup.
    _capture_live_schemas(bundle, provider)
    api = {} if missing_nodes else bundle.workflow.compile("api", run_inputs=dict(run_inputs or {}))
    # A blank LoadImage may be an unused workflow placeholder. Refuse it when
    # it actually feeds an output, before sending the prompt to ComfyUI.
    observed = provider.object_info()
    pending = {str(output.node_id) for output in bundle.workflow.outputs}
    pending.update(node_id for node_id, node in api.items()
                   if observed.get(node.get("class_type"), {}).get("output_node"))
    if api and (not pending or any(node_id not in api for node_id in pending)):
        raise ProductionEngineError("ComfyUI workflow output roots could not be established")
    reached = set()
    while pending:
        node_id = pending.pop()
        if node_id in reached or node_id not in api:
            continue
        reached.add(node_id)
        node = api[node_id]
        if node.get("class_type") == "LoadImage" and not str(node.get("inputs", {}).get("image") or "").strip():
            raise ProductionEngineError("ComfyUI workflow has a connected LoadImage without an image binding")
        for value in node.get("inputs", {}).values():
            if (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
                    and isinstance(value[1], int)):
                pending.add(value[0])
    issues = [*validate_api_against_schema(api, provider), *validate_api_link_shapes(api, provider)] if api else []
    missing_models: list[str] = []
    for node_id, node in api.items():
        schema = provider.get_schema(node["class_type"])
        if schema is None:
            continue
        for name, value in node.get("inputs", {}).items():
            spec = schema.inputs.get(name)
            if (spec is not None and spec.choices is not None
                    and isinstance(value, str) and value not in spec.choices
                    and _choice_scope(node["class_type"], name, value) == "environment_asset"):
                missing_models.append(f"{value} ({node['class_type']}.{name}, node {node_id})")
    if missing_nodes or missing_models:
        packs, _unresolved = missing_packs_for_workflow(
            bundle.workflow, lockfile_path=destination / "custom_nodes.lock"
        )
        # The helper excludes classes known to the agent's offline schemas;
        # they may still be absent from this user's live server.
        packs = list({pack.name: pack for pack in [
            *packs, *resolve_node_packs(set(missing_nodes), lockfile_path=destination / "custom_nodes.lock"),
        ]}.values())
        nodes = [
            name + "".join(f" (pack {pack.name})" for pack in packs if name in pack.classes)
            for name in missing_nodes
        ]
        gaps = []
        if nodes:
            gaps.append("missing nodes: " + ", ".join(nodes))
        if missing_models:
            gaps.append("missing models: " + ", ".join(missing_models))
        from astrid.core.generation.asset_gaps import asset_readiness
        readiness = asset_readiness(
            getattr(bundle.workflow, 'metadata', {}).get('registry_id'),
            nodes=missing_nodes, models=missing_models,
        )
        import shlex
        error = ProductionEngineError(
            f"ComfyUI at {url}: {'; '.join(gaps)}. "
            'Run: ' + shlex.join(readiness['fixes'][0]['argv']) +
            ". See 'Missing nodes and models' in the VibeComfy skill (packs/vibecomfy/docs/SKILL.md)."
        )
        error.readiness = readiness
        error.code = readiness['issues'][0]['code']
        raise error
    errors = [issue.message for issue in issues if issue.severity == "error"]
    if errors:
        raise ProductionEngineError("ComfyUI workflow preflight failed: " + "; ".join(errors))
    return provider


def _approve_live(bundle: Any, bindings: dict[str, Any], provider: Any) -> Any:
    """Approve canonically against the selected target's exact model choices."""
    bundle.require_canonical_authority("attached target execution")
    _capture_live_schemas(bundle, provider)
    try:
        return bundle.compile(run_inputs=bindings, schema_provider=provider)
    except Exception as exc:
        if (getattr(provider, "auxiliary_model_custody", "local") == "target"
                and "target" in str(exc) and "auxiliary model" in str(exc)):
            raise ProductionEngineError(f"ComfyUI at {provider.server_url}: {exc}") from exc
        raise


def _capture_live_schemas(bundle: Any, provider: Any) -> None:
    for node in bundle.workflow.nodes.values():
        schema = provider.get_schema(node.class_type)
        if schema is not None:
            node.metadata['schema'] = schema


def local_server_bundle(resolved: Any, destination: Path, *, output_identity: str | None = None,
                        allow_empty_image: bool = False) -> Any:
    """Derive attempt-owned image bindings and fresh output filenames.

    A seedless graph can otherwise reuse SaveImage's cached result from an
    earlier task. Keep the user's prefix, adding an attempt namespace in a
    private canonical revision; the saved workflow remains unchanged.
    Without an output identity, prepare only image bindings before preflight.
    """
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate, load_bundle

    from astrid.core.generation.backends.vibecomfy_local_server import validate_identity

    if output_identity is not None:
        validate_identity(output_identity)
    bundle = _canonical_bundle_value(resolved)
    workflow = bundle.workflow.clone()
    changed = False
    if "image" not in workflow.inputs:
        nodes = [node for node in workflow.nodes.values() if node.class_type == "LoadImage"]
        if nodes:
            node = nodes[0]
            value = node.inputs.get("image", node.widgets.get("image"))
            if (isinstance(value, str) and (value.strip() or allow_empty_image) and not any(
                    spec.node_id == node.id and spec.field == "image"
                    for spec in workflow.inputs.values())):
                input_types = dict(zip(node.native_input_names or (), node.native_input_types or ()))
                workflow.register_input(
                    "image", node.id, "image", value,
                    type=input_types.get("image", "STRING"), media_semantics="image",
                )
                changed = True
    for output in workflow.outputs:
        node = workflow.nodes[output.node_id]
        prefix = node.inputs.get("filename_prefix", node.widgets.get("filename_prefix"))
        if output_identity is not None and isinstance(prefix, str) and prefix:
            fields = node.inputs if "filename_prefix" in node.inputs else node.widgets
            fields["filename_prefix"] = f"{prefix}_astrid_{output_identity}"
            changed = True
    if not changed:
        return bundle
    root = destination / "local-server-workflow"
    root.mkdir(parents=True, exist_ok=True)
    source = bundle.python_path.with_name("source.json") if bundle.python_path else None
    from vibecomfy.workflow_bundle import WorkflowBundleError

    # A full path: a bare name is published relative to the process cwd.
    extra_members = {root / "source.json": source.read_bytes()} if source and source.is_file() else None
    derived = None
    # The ready-template renderer rebuilds ``requirements`` from the workflow and drops the pinned
    # ``runtime`` block of a materialized template (the H3 graph carries one), so the publish check
    # refuses the pair; the scratchpad renderer keeps it.  Publication verifies the semantic digest
    # either way, so whichever renderer round-trips the graph exactly is the one that is used.
    for source_format in ("ready_template", "scratchpad"):
        try:
            derived = emit_bundle_with_candidate(
                workflow, root / "workflow.py", Provenance.USER_CONFIRMED, bundle.ui_sidecar,
                parent_revision=bundle.revision_id, operation="authored",
                parent_evidence={"revision_id": bundle.revision_id, "workflow_identity": bundle.workflow_identity},
                source_format=source_format, extra_members=extra_members,
            )
            break
        except WorkflowBundleError:
            if source_format == "scratchpad":
                raise
    return load_bundle(derived.python_path, trust=Provenance.USER_CONFIRMED)


def _staged_digest(destination: Path, name: str) -> str | None:
    """Digest recorded by managed-asset staging for one engine-input file."""
    path = destination / "engine-input" / ".managed-asset-digests.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        digests = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = digests.get(name) if isinstance(digests, dict) else None
    return value if isinstance(value, str) else None


def _trust_from_environment(bundle: Any) -> tuple[dict[str, Any], ...] | None:
    """``--align trust`` on an attached ComfyUI: the workflow's declared rows; otherwise none."""
    from astrid.core.generation import target_inventory

    if os.environ.pop(target_inventory.TRUST_ENV, None) != "trust":
        return None
    return target_inventory.declared_rows(bundle.workflow)


def _custody_file(path: Path, destination: Path, label: str) -> Path:
    """Check the native file and all ancestors before reading or replacing it."""
    if (path.is_symlink() or not path.is_file()
            or not path.resolve(strict=True).is_relative_to(destination.resolve(strict=True))
            or any(part.is_symlink() for part in path.parents)
            or not stat.S_ISREG(path.lstat().st_mode)):
        raise ProductionEngineError(f"local_server {label} escaped attempt custody")
    return path


def _project_native_attempt(path: Path, destination: Path, *, alignment: Any, prepared: Any = None) -> None:
    from astrid.core.foundation.atomic_io import write_json_atomic

    _custody_file(path, destination, "native attempt")
    attempt = json.loads(path.read_text(encoding="utf-8"))
    attempt["alignment"] = alignment
    if prepared is not None:
        # Placement and preparation belong to Astrid. Native target/adapter
        # remain the external HTTP transport that actually executed this run.
        attempt["placement"] = "managed"
        attempt["managed_engine_preparation"] = prepared
        embedded = attempt.get("managed_generation_result", {}).get("evidence", {}).get("producer", {}).get("attempt", {}).get("record")
        if isinstance(embedded, dict):
            embedded.update(placement="managed", alignment=alignment, managed_engine_preparation=prepared)
    _custody_file(path, destination, "native attempt")
    write_json_atomic(path, attempt)


def run_local_server(
    resolved: Any, destination: Path, *, comfy_url: str, comfy_output_dir: str,
    task_identity: str, attempt_identity: str | None, run_inputs: Mapping[str, Any] | None,
    workflow_conversion_object_info_sha256: str | None = None,
) -> ProductionRunResult:
    from vibecomfy.runtime.run import run_sync
    from vibecomfy.runtime.session import SessionConfig

    from astrid.core.generation.backends.vibecomfy_local_server import (
        AttachedArtifacts,
        TargetRuntimeClient,
        cleanup_prompts,
        upload_image,
        upload_media,
        validate_comfy_url,
        validate_identity,
        validate_output_directory,
    )

    url = validate_comfy_url(comfy_url)
    output_directory = validate_output_directory(comfy_output_dir, destination.parent)
    validate_identity(task_identity)
    if attempt_identity is not None:
        validate_identity(attempt_identity)
    bundle = _canonical_bundle_value(resolved)
    values = _workflow_comfy_configuration(bundle)
    values.update({
        "runtime_root": str(destination / ".vibecomfy-runtime"),
        "cwd": str(destination), "warm_policy": "never",
        "disable_known_models": True, "quiet_schema_degradation": False,
        "schema_warn_only": False, "prompt_timeout_sec": _LOCAL_PROMPT_TIMEOUT_SEC,
        "task_id": task_identity,
    })
    # An attached server owns its filesystem. Even the legacy optional output
    # directory cannot grant local-folder access; artifacts arrive over /view.
    values.pop("output_directory", None)
    if attempt_identity is not None:
        values["attempt_id"] = attempt_identity
    config = SessionConfig.from_dict(values)
    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient

    from astrid.core.generation.managed_preparation import read_preparation
    prepared = read_preparation(task=task_identity, attempt=attempt_identity, endpoint=url)
    client = ComfyTargetClient(ComfyTarget(url, "attached"))
    custody = _trust_from_environment(bundle)
    from astrid.core.generation.target_inventory import declared_rows
    provider = _live_preflight(bundle, run_inputs, url, destination, client=client, target_custody=custody,
                               prepared_models=declared_rows(bundle.workflow) if prepared is not None else None)
    # Unknown connected classes may not survive a canonical rewrite. Report
    # live dependency gaps before deriving the attempt's private save prefixes.
    bundle = local_server_bundle(
        bundle, destination, output_identity=attempt_identity or task_identity,
    )
    bindings = dict(run_inputs or {})
    for name, value in bindings.items():
        spec = bundle.workflow.inputs[name]
        media = str(getattr(spec, "media_semantics", "") or "").lower()
        if media in {"image", "mask", "video", "audio"}:
            source = destination / "engine-input" / str(value)
            if media in {"image", "mask"}:
                bindings[name] = upload_image(url, source, task_id=task_identity)
            else:
                # Same custody as images, plus the digest the managed-assets
                # archive was verified against when it was staged.
                bindings[name] = upload_media(
                    url, source, task_id=task_identity, kind=media,
                    expected_sha256=_staged_digest(destination, str(value)),
                )
    record = _approve_live(bundle, bindings, provider)

    def stop(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    pins = {"VIBECOMFY_COMFY_CONFIGURATION": "{}", "VIBECOMFY_WARM": "never",
            "VIBECOMFY_SCHEMA_WARN_ONLY": "0"}
    previous = {key: os.environ.get(key) for key in pins}
    restore = platform.install_stop_handlers(stop)
    try:
        os.environ.update(pins)
        try:
            context = None
            if prepared is not None:
                from vibecomfy.runtime.run_context import RunContext
                run_id = "run-" + uuid.uuid4().hex
                run_dir = destination / ".vibecomfy-runtime" / "out" / "runs" / run_id
                if any(part.is_symlink() for part in (run_dir, *run_dir.parents)):
                    raise ProductionEngineError("local_server native attempt escaped attempt custody")
                run_dir.mkdir(parents=True, exist_ok=False)
                if not run_dir.resolve().is_relative_to(destination.resolve()):
                    raise ProductionEngineError("local_server native attempt escaped attempt custody")
                context = RunContext(run_id, run_dir, task_id=task_identity,
                                     attempt_id=attempt_identity, execution_id=prepared.get("execution_identity"))
                context.begin(str(bundle.python_path))
                _custody_file(context.receipt_path, destination, "native attempt")
            try:
                result = run_sync(
                    record, bundle, server_url=url, config=config, schema_provider=provider,
                    server_client=TargetRuntimeClient(client),
                    artifact_client=AttachedArtifacts(client, legacy_output_directory=output_directory),
                    **({"run_context": context} if context is not None else {}),
                )
            finally:
                if context is not None:
                    original_error = sys.exc_info()[1]
                    try:
                        _project_native_attempt(context.receipt_path, destination,
                                                alignment=prepared["alignment"], prepared=prepared)
                    except Exception as projection_error:
                        if original_error is None:
                            raise
                        original_error.add_note(f"native attempt projection failed: {projection_error}")
            if not getattr(result, "managed_generation_result_path", None):
                raise ProductionEngineError(
                    "local_server requires VibeComfy managed_generation_result_path; "
                    "check that the selected target exposes /view and declares its final output nodes"
                )
            result_path = Path(result.managed_generation_result_path)
            _custody_file(result_path, destination, "managed result")
            # The pinned producer reports public-input defaults in metadata.
            # Preserve its evidence, but report the detached invocation values
            # (including the chosen seed) that actually reached /prompt.
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            payload["inputs"].update(bindings)
            if custody is not None:
                alignment = {"mode": "trust", "status": "unverified", "verified": False}
                payload["evidence"]["producer"]["alignment"] = alignment
                _project_native_attempt(result_path.with_name("attempt.json"), destination, alignment=alignment)
            if prepared is not None:
                payload["evidence"]["producer"]["alignment"] = prepared["alignment"]
                payload["evidence"]["transport"]["placement"] = "managed"
                payload["evidence"]["producer"]["attempt"]["record"].update(
                    placement="managed", alignment=prepared["alignment"], managed_engine_preparation=prepared)
            if workflow_conversion_object_info_sha256 is not None:
                schema_provenance = payload["evidence"]["producer"]["runtime"]["schema_provenance"]
                schema_provenance["workflow_conversion_object_info_sha256"] = (
                    workflow_conversion_object_info_sha256
                )
            from astrid.core.foundation.atomic_io import write_json_atomic
            _custody_file(result_path, destination, "managed result")
            write_json_atomic(result_path, payload)
            return _result_from_runtime_result(result, tuple(Path(path) for path in result.outputs))
        except BaseException as exc:
            try:
                cleanup_prompts(url, destination)
            except Exception as cleanup_error:
                # Preserve the failure that triggered cleanup (especially a
                # custody refusal) while retaining the cleanup failure as
                # diagnostic context. cleanup_prompts validates journals
                # before trusting their prompt IDs, so an unsafe journal must
                # never replace the original attempt error.
                exc.add_note(f"local_server prompt cleanup failed: {cleanup_error}")
            if "vibecomfy schema refresh" in str(exc) or getattr(exc, "next_action", None) == "vibecomfy schema refresh":
                from astrid.core.generation.asset_gaps import align_argv
                import shlex
                fix = 'Install the missing locked node pack: ' + shlex.join(align_argv())
                raise ProductionEngineError(str(exc).replace("vibecomfy schema refresh", fix)) from exc
            raise
    finally:
        restore()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
