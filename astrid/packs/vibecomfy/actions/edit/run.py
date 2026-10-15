"""Canonical runtime entrypoint for ``vibecomfy.edit`` transitions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core.contracts.errors import AstridError, render_astrid_error
from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("vibecomfy.edit")

from astrid.packs.vibecomfy.shared.bundle_inputs import (  # noqa: E402
    staged_workflow_path,
)


class WorkflowTransitionError(ValueError):
    """A canonical workflow transition could not be admitted or published."""


_TRANSITION_KINDS = frozenset({"typed_edit", "manual_capture"})


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _member_bytes(python_path: Path) -> dict[str, bytes]:
    paths = {
        "workflow.py": python_path,
        "workflow.vibe.json": python_path.with_name("workflow.vibe.json"),
        "source.json": python_path.with_name("source.json"),
    }
    try:
        return {name: path.read_bytes() for name, path in paths.items()}
    except OSError as exc:
        raise WorkflowTransitionError(f"canonical bundle member is unavailable: {exc}") from exc


def _member_digests(members: Mapping[str, bytes]) -> dict[str, str]:
    return {name: _sha256(payload) for name, payload in members.items()}


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowTransitionError(f"{label} must be readable UTF-8 JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise WorkflowTransitionError(f"{label} must contain a JSON object")
    return dict(value)


def _parse_operations(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    document = _json_object(path, label="operations")
    unknown = sorted(set(document) - {"schema_version", "expected_revision", "ops"})
    if unknown:
        raise WorkflowTransitionError(
            "operations does not accept keys: " + ", ".join(unknown)
        )
    if document.get("schema_version", 1) != 1:
        raise WorkflowTransitionError("operations.schema_version must be 1")
    expected_revision = document.get("expected_revision", 0)
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise WorkflowTransitionError("operations.expected_revision must be an integer")
    if expected_revision != 0:
        raise WorkflowTransitionError(
            "operations.expected_revision must be 0 for a new parent-bundle edit task"
        )
    raw_ops = document.get("ops")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise WorkflowTransitionError("operations.ops must be a non-empty list")
    if any(not isinstance(item, Mapping) for item in raw_ops):
        raise WorkflowTransitionError("every operations.ops entry must be an object")
    ops = [dict(item) for item in raw_ops]
    if any(item.get("op") == "edit_batch" for item in ops):
        raise WorkflowTransitionError("operations.ops cannot contain a nested edit_batch")
    return ops, [dict(item) for item in ops]


def _atomic_publish(out_dir: Path, files: Mapping[str, bytes]) -> dict[str, Path]:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise WorkflowTransitionError(f"output directory is not empty: {out_dir}")
    staging: Path | None = None
    try:
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.", dir=out_dir.parent))
        paths: dict[str, Path] = {}
        for name, payload in files.items():
            staging_path = staging / name
            staging_path.write_bytes(payload)
            paths[name] = out_dir / name
        if out_dir.exists():
            out_dir.rmdir()
        os.replace(staging, out_dir)
        staging = None
        return paths
    except OSError as exc:
        raise WorkflowTransitionError(f"could not publish complete transition outputs: {exc}") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _load_capture_graph(path: Path) -> dict[str, Any]:
    graph = _json_object(path, label="capture_graph")
    return graph


def _optional_path(value: str) -> Path | None:
    return Path(value) if value.strip() else None


def _edit_workflow_with_gate(
    *,
    python_path: Path,
    companion_path: Path,
    source_path: Path,
    workflow_id: str,
    parent_revision: str,
    parent_task_id: str,
    origin_task_id: str,
    transition_kind: str,
    operations_path: Path | None,
    capture_python_path: Path | None,
    capture_graph_path: Path | None,
    out_dir: Path,
    gate_audit: list[dict[str, Any]],
) -> dict[str, Path]:
    """Create one history-linked canonical successor from immutable members."""
    scalars = {
        "workflow_id": workflow_id,
        "parent_revision": parent_revision,
        "parent_task_id": parent_task_id,
        "origin_task_id": origin_task_id,
    }
    if any(not value.strip() for value in scalars.values()):
        raise WorkflowTransitionError("workflow and Astrid lineage fields must be non-empty")
    if transition_kind not in _TRANSITION_KINDS:
        raise WorkflowTransitionError(
            "transition_kind must be typed_edit or manual_capture"
        )
    with staged_workflow_path(
        workflow=None,
        python=python_path,
        companion=companion_path,
        source=source_path,
    ) as (staged_python, _authority):
        from vibecomfy.porting.edit.bundle_service import transition_bundle
        from vibecomfy.workflow_bundle import load_bundle

        parent_bundle = load_bundle(staged_python)
        identity = str(parent_bundle.workflow_identity)
        if identity != workflow_id:
            raise WorkflowTransitionError(
                f"workflow identity mismatch: task names {workflow_id!r}, bundle is {identity!r}"
            )
        if parent_bundle.revision_id != parent_revision:
            raise WorkflowTransitionError(
                "parent revision does not match the admitted canonical bundle; refresh inputs"
            )
        before_members = _member_bytes(staged_python)
        before_digests = _member_digests(before_members)
        requested_ops: list[dict[str, Any]] = []
        capture_source: Path | None = None
        transition_kwargs: dict[str, Any]
        if transition_kind == "typed_edit":
            if operations_path is None or capture_python_path is not None or capture_graph_path is not None:
                raise WorkflowTransitionError(
                    "typed_edit requires operations and does not accept capture candidates"
                )
            ops, requested_ops = _parse_operations(operations_path)
            transition_kwargs = {
                "tool_calls": [{"tool": "edit_batch", "args": {"ops": ops}}],
                "expected_parent_revision": parent_revision,
            }
        else:
            if operations_path is not None or (capture_python_path is None) == (capture_graph_path is None):
                raise WorkflowTransitionError(
                    "manual_capture requires exactly one of capture_python or capture_graph"
                )
            if capture_python_path is not None:
                capture_root = staged_python.parent / "capture-candidate"
                capture_root.mkdir()
                capture_source = capture_root / "workflow.py"
                try:
                    capture_source.write_bytes(capture_python_path.read_bytes())
                    (capture_root / "workflow.vibe.json").write_bytes(
                        before_members["workflow.vibe.json"]
                    )
                    (capture_root / "source.json").write_bytes(before_members["source.json"])
                except OSError as exc:
                    raise WorkflowTransitionError(
                        f"could not stage direct Python capture candidate: {exc}"
                    ) from exc
                transition_kwargs = {
                    "capture": True,
                    "candidate_python": capture_source,
                    "expected_parent_revision": parent_revision,
                }
            else:
                assert capture_graph_path is not None
                transition_kwargs = {
                    "capture_graph": _load_capture_graph(capture_graph_path),
                    "expected_parent_revision": parent_revision,
                }

        published = staged_python.parent / "published" / "workflow.py"
        try:
            result = transition_bundle(staged_python, output=published, **transition_kwargs)
        except Exception as exc:
            raise WorkflowTransitionError(f"VibeComfy rejected workflow transition: {exc}") from exc
        if getattr(result, "status", None) != "saved":
            raise WorkflowTransitionError(
                f"VibeComfy transition did not save a successor (status={getattr(result, 'status', None)!r})"
            )
        result_python = published
        after_bundle = load_bundle(result_python)
        if after_bundle.workflow_identity != identity:
            raise WorkflowTransitionError("saved successor changed workflow identity")
        if after_bundle.revision_id != result.revision_id:
            raise WorkflowTransitionError("saved successor reload has a different revision ID")
        after_members = _member_bytes(result_python)
        if after_members["source.json"] != before_members["source.json"]:
            raise WorkflowTransitionError("transition changed byte-identical source.json")
        after_digests = _member_digests(after_members)
        result_dict = result.to_dict()
        report = {
            "schema_version": 1,
            "transition_kind": transition_kind,
            "workflow_id": workflow_id,
            "workflow_identity": identity,
            "parent_task_id": parent_task_id,
            "origin_task_id": origin_task_id,
            "parent_revision": parent_revision,
            "revision_id": after_bundle.revision_id,
            "before": {
                "revision_id": parent_bundle.revision_id,
                "parent_revision": parent_bundle.parent_revision or None,
                "semantic_digest": parent_bundle.semantic_digest,
                "ui_digest": parent_bundle.ui_digest,
                "members": before_digests,
            },
            "after": {
                "revision_id": after_bundle.revision_id,
                "parent_revision": parent_bundle.revision_id,
                "semantic_digest": after_bundle.semantic_digest,
                "ui_digest": after_bundle.ui_digest,
                "members": after_digests,
            },
            "members": after_digests,
            "requested_operations": requested_ops,
            "operations": result_dict.get("operations", []),
            "diff": result_dict.get("diff"),
            "validation": {"status": "canonical_bundle_reload_verified"},
            "readiness": {"status": "bundle_ready", "run_readiness": "not_assessed"},
            "diagnostics": result_dict.get("diagnostics", []),
            "security_gate_audit": gate_audit,
            "python_execution_consent": "confirmed",
            "service_result": result_dict,
        }
        try:
            report_bytes = (
                json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise WorkflowTransitionError(f"VibeComfy result is not JSON serializable: {exc}") from exc
        output_files = {
            "workflow.py": after_members["workflow.py"],
            "workflow.vibe.json": after_members["workflow.vibe.json"],
            "source.json": after_members["source.json"],
            "edit-report.json": report_bytes,
        }

    published_files = _atomic_publish(out_dir, output_files)
    return {
        "python": published_files["workflow.py"],
        "companion": published_files["workflow.vibe.json"],
        "source": published_files["source.json"],
        "report": published_files["edit-report.json"],
    }


def edit_workflow(
    *,
    python_path: Path,
    companion_path: Path,
    source_path: Path,
    workflow_id: str,
    parent_revision: str,
    parent_task_id: str,
    origin_task_id: str,
    transition_kind: str,
    operations_path: Path | None,
    capture_python_path: Path | None,
    capture_graph_path: Path | None,
    out_dir: Path,
    python_execution_consent: str | None,
) -> dict[str, Path]:
    """Create one consented canonical successor and record the gate audit."""
    from astrid.packs.vibecomfy.shared.python_execution_consent import (
        PythonExecutionConsentError,
        confirmed_python_execution_scope,
    )

    try:
        with confirmed_python_execution_scope(python_execution_consent) as gate:
            return _edit_workflow_with_gate(
                python_path=python_path,
                companion_path=companion_path,
                source_path=source_path,
                workflow_id=workflow_id,
                parent_revision=parent_revision,
                parent_task_id=parent_task_id,
                origin_task_id=origin_task_id,
                transition_kind=transition_kind,
                operations_path=operations_path,
                capture_python_path=capture_python_path,
                capture_graph_path=capture_graph_path,
                out_dir=out_dir,
                gate_audit=gate.audit,
            )
    except PythonExecutionConsentError as exc:
        raise WorkflowTransitionError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a canonical VibeComfy successor through typed edits or explicit capture."
    )
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--companion", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--parent-revision", required=True)
    parser.add_argument("--parent-task-id", required=True)
    parser.add_argument("--origin-task-id", required=True)
    parser.add_argument("--transition-kind", required=True)
    parser.add_argument("--python-execution-consent", required=True)
    parser.add_argument("--operations", type=_optional_path, default=None)
    parser.add_argument("--capture-python", type=_optional_path, default=None)
    parser.add_argument("--capture-graph", type=_optional_path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        edit_workflow(
            python_path=args.python,
            companion_path=args.companion,
            source_path=args.source,
            workflow_id=args.workflow_id,
            parent_revision=args.parent_revision,
            parent_task_id=args.parent_task_id,
            origin_task_id=args.origin_task_id,
            transition_kind=args.transition_kind,
            operations_path=args.operations,
            capture_python_path=args.capture_python,
            capture_graph_path=args.capture_graph,
            out_dir=args.out,
            python_execution_consent=args.python_execution_consent,
        )
    except WorkflowTransitionError as exc:
        render_astrid_error(
            AstridError(
                f"vibecomfy.edit: {exc}",
                recovery_command="invoke vibecomfy.edit through astrid.sdk.invoke with its declared inputs",
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
