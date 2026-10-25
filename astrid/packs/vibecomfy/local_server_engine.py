"""`local_server` profile: run a workflow on the user's already-running ComfyUI.

Split from production_engine so the attach-only path (live preflight, private
save prefixes, image upload, owned-prompt cleanup) reads as one unit.
"""

from __future__ import annotations

import json
import os
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


def _live_preflight(bundle: Any, run_inputs: Mapping[str, Any] | None, url: str, destination: Path) -> Any:
    """Read the live schema, without preparing or installing dependencies."""
    from vibecomfy.node_packs import missing_packs_for_workflow, resolve_node_packs
    from vibecomfy.schema.provider import get_target_schema_provider
    from vibecomfy.schema.validate import (
        _choice_scope,
        validate_api_against_schema,
        validate_api_link_shapes,
    )

    provider = get_target_schema_provider(url, cache_dir=destination / ".vibecomfy-schema")
    # Inspect an unapproved projection first, so a missing live class gets
    # our actionable gap report before the approval gate can reject it.
    api = bundle.workflow.compile("api", run_inputs=dict(run_inputs or {}))
    # Force a fresh read, including an empty registry (which VibeComfy's
    # general validator treats as an unavailable, skippable schema).
    provider.object_info()
    issues = [*validate_api_against_schema(api, provider), *validate_api_link_shapes(api, provider)]
    missing_nodes = sorted({
        node["class_type"] for node in api.values()
        if provider.get_schema(node["class_type"]) is None
    })
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
        raise ProductionEngineError(
            f"ComfyUI at {url}: {'; '.join(gaps)}. "
            "See 'Missing nodes and models' in the VibeComfy skill "
            "(packs/vibecomfy/docs/SKILL.md)."
        )
    errors = [issue.message for issue in issues if issue.severity == "error"]
    if errors:
        raise ProductionEngineError("ComfyUI workflow preflight failed: " + "; ".join(errors))
    return provider


def _approve_live(bundle: Any, bindings: dict[str, Any], provider: Any) -> Any:
    """Bind the canonical projections to the live target's inventory.

    The pinned bundle.compile() also requires every model in the agent's
    local model registry/filesystem, even with a live target provider. Those
    files belong to the user's server. Our read-only live preflight replaces
    that local inventory gate; VibeComfy still verifies the record against
    the unchanged canonical bundle at its production queue boundary.
    """
    from vibecomfy.workflow_bundle import ApprovedProjectionRecord, canonical_digest

    api = bundle.workflow.compile("api", run_inputs=bindings)
    record = ApprovedProjectionRecord(
        revision_id=bundle.revision_id,
        selected_variant=bundle.workflow.default_variant,
        input_binding=bindings,
        api_projection=api,
        ui_projection=bundle.materialize_ui(schema_provider=provider),
        api_digest=canonical_digest(api),
    )
    record.assert_matches(
        bundle, run_inputs=bindings, api_projection=api,
        ui_projection=bundle.materialize_ui(schema_provider=provider),
    )
    return record


def local_server_bundle(resolved: Any, destination: Path, *, output_identity: str | None = None) -> Any:
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
            if (isinstance(value, str) and not any(
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
    derived = emit_bundle_with_candidate(
        workflow, root / "workflow.py", Provenance.USER_CONFIRMED, bundle.ui_sidecar,
        parent_revision=bundle.revision_id, operation="authored",
        parent_evidence={"revision_id": bundle.revision_id, "workflow_identity": bundle.workflow_identity},
        source_format="ready_template",
        # A full path: a bare name is published relative to the process cwd.
        extra_members={root / "source.json": source.read_bytes()} if source and source.is_file() else None,
    )
    return load_bundle(derived.python_path, trust=Provenance.USER_CONFIRMED)


def run_local_server(
    resolved: Any, destination: Path, *, comfy_url: str, comfy_output_dir: str,
    task_identity: str, attempt_identity: str | None, run_inputs: Mapping[str, Any] | None,
) -> ProductionRunResult:
    from vibecomfy.runtime.run import run_sync
    from vibecomfy.runtime.session import SessionConfig

    from astrid.core.generation.backends.vibecomfy_local_server import (
        cleanup_prompts,
        upload_image,
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
    if output_directory:
        values["output_directory"] = output_directory
    if attempt_identity is not None:
        values["attempt_id"] = attempt_identity
    config = SessionConfig.from_dict(values)
    provider = _live_preflight(bundle, run_inputs, url, destination)
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
            if media not in {"image", "mask"}:
                raise ProductionEngineError("local_server managed assets must be images")
            bindings[name] = upload_image(url, source, task_id=task_identity)
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
            result = run_sync(record, bundle, server_url=url, config=config, schema_provider=provider)
            if not getattr(result, "managed_generation_result_path", None):
                raise ProductionEngineError(
                    "local_server requires VibeComfy managed_generation_result_path; "
                    "set comfy_output_dir to the server's output directory and give each "
                    "save node a filename_prefix when the workflow has multiple outputs"
                )
            result_path = Path(result.managed_generation_result_path)
            if (result_path.is_symlink() or not result_path.resolve().is_relative_to(destination.resolve())
                    or any(part.is_symlink() for part in result_path.parents)):
                raise ProductionEngineError("local_server managed result escaped attempt custody")
            # The pinned producer reports public-input defaults in metadata.
            # Preserve its evidence, but report the detached invocation values
            # (including the chosen seed) that actually reached /prompt.
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            payload["inputs"].update(bindings)
            result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            return _result_from_runtime_result(result, tuple(Path(path) for path in result.outputs))
        except BaseException as exc:
            cleanup_prompts(url, destination)
            if "vibecomfy schema refresh" in str(exc) or getattr(exc, "next_action", None) == "vibecomfy schema refresh":
                fix = "See 'Missing nodes and models' in the VibeComfy skill (packs/vibecomfy/docs/SKILL.md)"
                raise ProductionEngineError(str(exc).replace("vibecomfy schema refresh", fix)) from exc
            raise
    finally:
        restore()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
