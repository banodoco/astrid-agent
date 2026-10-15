"""Runtime entrypoint for VibeComfy validation and execution."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Mapping

from astrid.core.contracts.errors import AstridError, render_astrid_error
from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("vibecomfy.validate")

from astrid.core.cli_choices import add_choice_arg  # noqa: E402
from astrid.core.generation.model_root import (  # noqa: E402
    ModelRootBindingError,
    model_root_binding_from_environment,
    use_attested_vibecomfy_models_root,
)
from astrid.packs.vibecomfy.shared.bundle_inputs import (  # noqa: E402
    staged_workflow_path,
)
from astrid.packs.vibecomfy.shared.python_execution_consent import (  # noqa: E402
    PythonExecutionConsentError,
    validate_python_execution_consent,
)


class WorkflowValidationError(ValueError):
    """A VibeComfy workflow could not be validated."""


def _bundle_declares_models(bundle: Any) -> bool:
    """Return whether the canonical bundle has model work to reconcile."""
    workflow = getattr(bundle, "workflow", None)
    metadata = getattr(workflow, "metadata", {})
    if isinstance(metadata, Mapping) and metadata.get("model_assets"):
        return True
    requirements = getattr(workflow, "requirements", None)
    if getattr(requirements, "models", ()):
        return True
    try:
        from vibecomfy.model_assets import _referenced_model_values

        return bool(_referenced_model_values(workflow))
    except Exception:  # noqa: BLE001 - malformed model declarations fail closed in the compiler
        # Let the authoritative bundle compiler report malformed model
        # declarations instead of silently taking the generic path.
        return True


def _model_root_preflight(bundle: Any, root: Path) -> None:
    """Run the canonical read-only model preflight against the attested root.

    The bundle compiler is still the first authority.  This is only the
    bounded fallback used when custom-node object-info identity is intentionally
    deferred to the runtime target.  Keep its model declarations in lockstep
    with ``vibecomfy.workflow_bundle._approval_preconditions``: picker values,
    authored metadata assets, and explicit requirements all identify model
    custody, and none of them may trigger a fetch at this boundary.
    """
    try:
        from vibecomfy.fetch import is_present, verify
        from vibecomfy.model_assets import _referenced_model_values
        from vibecomfy.registry.models_loader import load_registry, resolve_model_entry

        workflow = bundle.workflow
        metadata = getattr(workflow, "metadata", {})
        requirements = getattr(workflow, "requirements", None)
        references: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add_reference(value: Any, subdir: Any = "") -> None:
            if not isinstance(value, str) or not value.strip():
                raise WorkflowValidationError("workflow model reference is malformed")
            if subdir is not None and not isinstance(subdir, str):
                raise WorkflowValidationError("workflow model reference subdir is malformed")
            normalized_value = value.replace("\\", "/")
            normalized_subdir = (subdir or "").replace("\\", "/")
            key = (normalized_value, normalized_subdir)
            if key not in seen:
                seen.add(key)
                references.append({"value": normalized_value, "subdir": normalized_subdir})

        def declared_subdir(value: Any, subdir: Any = "") -> Any:
            if subdir not in (None, ""):
                return subdir
            if isinstance(value, str):
                for reference in references:
                    if reference["value"] == value and reference["subdir"]:
                        return reference["subdir"]
            return subdir

        for reference in _referenced_model_values(workflow):
            if not isinstance(reference, Mapping):
                raise WorkflowValidationError("workflow model reference is malformed")
            add_reference(reference.get("value"), reference.get("subdir", ""))

        if isinstance(metadata, Mapping) and "model_assets" in metadata:
            assets = metadata["model_assets"]
            if not isinstance(assets, list):
                raise WorkflowValidationError("workflow model_assets are malformed")
            for asset in assets:
                if not isinstance(asset, Mapping):
                    raise WorkflowValidationError("workflow model asset is malformed")
                add_reference(
                    asset.get("name"),
                    declared_subdir(asset.get("name"), asset.get("subdir", asset.get("directory", ""))),
                )

        declared_models = getattr(requirements, "models", ()) if requirements is not None else ()
        if declared_models is None:
            declared_models = ()
        for value in declared_models:
            if isinstance(value, Mapping):
                add_reference(
                    value.get("name"),
                    declared_subdir(value.get("name"), value.get("subdir", value.get("directory", ""))),
                )
            elif isinstance(value, str):
                add_reference(value, declared_subdir(value))
            else:
                raise WorkflowValidationError("workflow requirements.models contains a malformed entry")

        if not references:
            return

        authored_assets = (
            [asset for asset in metadata.get("model_assets", []) if isinstance(asset, Mapping)]
            if isinstance(metadata, Mapping) and isinstance(metadata.get("model_assets"), list)
            else []
        )

        def authored_asset(value: str, subdir: str) -> Mapping[str, Any] | None:
            for asset in authored_assets:
                name = asset.get("name", asset.get("filename"))
                asset_subdir = asset.get("subdir", asset.get("directory", ""))
                if str(name) == value and str(asset_subdir or "") == subdir:
                    return asset
            return None

        registry = None
        for reference in references:
            value = reference["value"]
            subdir = reference["subdir"]
            effective_subdir = subdir
            if not effective_subdir:
                if registry is None:
                    registry = load_registry()
                entry = resolve_model_entry(value, registry=registry, subdir=None)
                if entry is not None and entry.targets:
                    target_path = str(entry.targets[0].path).replace("\\", "/")
                    effective_subdir = target_path.rsplit("/", 1)[0] if "/" in target_path else ""
            if not effective_subdir:
                raise WorkflowValidationError(
                    f"model reference has no deterministic local target: {value}"
                )
            if not is_present({"name": value, "subdir": effective_subdir}, root=root):
                raise WorkflowValidationError(
                    f"model-aware preflight found no model at the attested root: {value}"
                )
            local_asset = authored_asset(value, effective_subdir)
            if local_asset is not None:
                verify(local_asset, root=root)
    except WorkflowValidationError:
        raise
    except Exception as exc:
        raise WorkflowValidationError(f"model-aware preflight failed: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run VibeComfy workflow commands.")
    add_choice_arg(parser, "command", values=("run", "validate"))
    parser.add_argument("workflow", nargs="?", default="")
    parser.add_argument("--python", default="")
    parser.add_argument("--companion", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--python-execution-consent", default="")
    parser.add_argument("--workflow-inputs", default="")
    parser.add_argument("--out", type=Path)
    return parser


def _static_ui_validation(workflow_path: Path) -> dict[str, Any]:
    """Validate UI JSON through VibeComfy's static ingestion and IR checks."""
    from vibecomfy.ingest.loader import load_workflow_json
    from vibecomfy.ingest.normalize import from_ui

    raw = load_workflow_json(workflow_path)
    workflow = from_ui(
        raw,
        source_path=str(workflow_path),
        use_comfy_converter=False,
    )
    report = workflow.validate()
    return {
        "schema_version": 1,
        "authority": "input_ui_graph",
        "validation_mode": "static_ui_graph",
        "workflow_id": workflow.id,
        "status": "ok" if report.ok else "error",
        "ok": report.ok,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "severity": issue.severity,
                "detail": issue.detail or {},
            }
            for issue in report.issues
        ],
        "python_execution_consent": None,
        "security_gate_audit": [],
    }


def _canonical_bundle_validation(
    workflow_path: Path,
    *,
    python_execution_consent: str | None,
    workflow_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validate_python_execution_consent(python_execution_consent)
    from vibecomfy.cli import main as vibecomfy_main
    from vibecomfy.security import current_gate_context, set_gate_context

    previous_gate = current_gate_context()
    captured_stdout = StringIO()
    captured_stderr = StringIO()
    try:
        with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
            return_code = vibecomfy_main(
                ["--yes", "--quiet", "validate", str(workflow_path), "--json", "--no-schema"]
            )
            if return_code == 0 and workflow_inputs is not None:
                from vibecomfy.cli_loader import load_bundle

                bundle = load_bundle(workflow_path)
                bundle.require_canonical_authority("workflow validation")
                model_root = None
                if _bundle_declares_models(bundle):
                    try:
                        model_root = model_root_binding_from_environment(verify_files=True)
                    except ModelRootBindingError as exc:
                        raise WorkflowValidationError(
                            f"model-aware validation requires the attested model-root binding: {exc}"
                        ) from exc
                compile_kwargs: dict[str, Any] = {
                    "run_inputs": dict(workflow_inputs),
                }
                if model_root is None:
                    approval = bundle.compile(**compile_kwargs)
                else:
                    compile_kwargs["models_root"] = str(model_root.path)
                    with use_attested_vibecomfy_models_root(bundle, model_root.path):
                        try:
                            approval = bundle.compile(**compile_kwargs)
                        except Exception as exc:
                            # Some portable authored bundles intentionally defer
                            # custom-node identity to the target-schema phase.
                            # Keep the real model-aware compiler as the first
                            # authority, then use its existing offline model
                            # preflight plus the structural projection when that
                            # later identity check is the only unavailable fact.
                            if "object-info identity" not in str(exc):
                                raise
                            _model_root_preflight(bundle, model_root.path)
                            projection = bundle.workflow.compile(
                                "api",
                                run_inputs=dict(workflow_inputs),
                            )
                        else:
                            projection = getattr(approval, "api_projection", None)
                if model_root is None:
                    projection = getattr(approval, "api_projection", None)
                if not isinstance(projection, Mapping) or not projection:
                    raise WorkflowValidationError(
                        "concrete workflow inputs produced an empty or invalid API graph"
                    )
        gate = current_gate_context()
        audit = list(gate.audit)
    except SystemExit as exc:
        gate = current_gate_context()
        audit = list(gate.audit)
        return_code = int(exc.code or 0)
    finally:
        set_gate_context(previous_gate)

    raw_payload = captured_stdout.getvalue().strip()
    if return_code != 0:
        message = captured_stderr.getvalue().strip() or raw_payload or "VibeComfy returned an error"
        raise WorkflowValidationError(f"VibeComfy rejected the canonical bundle: {message}")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise WorkflowValidationError(
            f"VibeComfy returned invalid JSON validation output: {raw_payload[:500]}"
        ) from exc
    if not isinstance(payload, dict):
        raise WorkflowValidationError("VibeComfy validation output must be a JSON object")
    payload.update(
        {
            "schema_version": 1,
            "authority": "canonical_workflow_bundle",
            "validation_mode": "canonical_bundle_structural",
            "python_execution_consent": "confirmed",
            "security_gate_audit": audit,
            "concrete_workflow_inputs_projected": workflow_inputs is not None,
        }
    )
    return payload


def _write_report(out_dir: Path, report: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "validation-report.json"
    try:
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, default=str)
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise WorkflowValidationError(f"could not publish validation report: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with staged_workflow_path(
            workflow=args.workflow,
            python=args.python,
            companion=args.companion,
            source=args.source,
        ) as (workflow_path, authority):
            if args.command == "run":
                return subprocess.run(
                    [sys.executable, "-m", "vibecomfy.cli", "run", str(workflow_path)]
                ).returncode

            if args.out is None:
                raise WorkflowValidationError("--out is required for validation")
            if authority == "canonical_bundle":
                workflow_inputs = None
                if args.workflow_inputs:
                    try:
                        parsed_inputs = json.loads(args.workflow_inputs)
                    except json.JSONDecodeError as exc:
                        raise WorkflowValidationError("--workflow-inputs must be valid JSON") from exc
                    if not isinstance(parsed_inputs, dict):
                        raise WorkflowValidationError("--workflow-inputs must be a JSON object")
                    workflow_inputs = parsed_inputs
                report = _canonical_bundle_validation(
                    workflow_path,
                    python_execution_consent=args.python_execution_consent,
                    workflow_inputs=workflow_inputs,
                )
            else:
                validate_python_execution_consent(
                    args.python_execution_consent,
                    required=False,
                )
                report = _static_ui_validation(workflow_path)
                report["python_execution_consent"] = (
                    "confirmed" if args.python_execution_consent == "confirmed" else None
                )
            # This offline executor checks structure only. The run adapter owns
            # session attestation and fresh target-schema validation before queueing.
            report["runtime_validation"] = {
                "status": "deferred",
                "executor": "vibecomfy.run",
                "checks": ["session_identity", "target_schema"],
            }
            _write_report(args.out, report)
            if not report.get("ok", report.get("status") == "ok"):
                raise WorkflowValidationError("workflow validation reported errors")
    except (PythonExecutionConsentError, WorkflowValidationError, ValueError) as exc:
        render_astrid_error(
            AstridError(
                f"vibecomfy.validate: {exc}",
                recovery_command="invoke vibecomfy.validate through astrid.sdk.invoke with its declared inputs",
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
