"""Canonical runtime entrypoint for ``vibecomfy.import``."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

from astrid.core.contracts.errors import AstridError, render_astrid_error
from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("vibecomfy.import")


class WorkflowImportError(ValueError):
    """The source or canonical import result violates the origin contract."""


_MEMBER_NAMES = ("workflow.py", "workflow.vibe.json", "source.json")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_artifacts(artifacts: Any, *, source_bytes: bytes, workflow_id: str) -> tuple[dict[str, bytes], dict[str, Any]]:
    required_bytes = {
        "workflow.py": getattr(artifacts, "python_bytes", None),
        "workflow.vibe.json": getattr(artifacts, "companion_bytes", None),
        "source.json": getattr(artifacts, "source_bytes", None),
    }
    if any(not isinstance(value, bytes) or not value for value in required_bytes.values()):
        raise WorkflowImportError("VibeComfy import service returned a missing or empty bundle member")
    if required_bytes["source.json"] != source_bytes:
        raise WorkflowImportError("VibeComfy import service changed the admitted source bytes")

    report = getattr(artifacts, "report", None)
    if not isinstance(report, Mapping):
        raise WorkflowImportError("VibeComfy import service returned no origin report object")
    report = dict(report)
    if report.get("schema_version") != 1 or report.get("transition_kind") != "origin":
        raise WorkflowImportError("VibeComfy import report must describe schema version 1 origin")
    if report.get("workflow_id") != workflow_id:
        raise WorkflowImportError("VibeComfy import report workflow_id does not match the request")
    if not report.get("workflow_identity") or not report.get("revision_id"):
        raise WorkflowImportError("VibeComfy import report is missing workflow or revision identity")
    for field in ("parent_revision", "parent_task_id", "origin_task_id"):
        if report.get(field) is not None:
            raise WorkflowImportError(f"origin report {field} must be null")
    if "readiness" not in report or "validation" not in report:
        raise WorkflowImportError("VibeComfy import report must include readiness and validation status")

    after = report.get("after")
    after_members = after.get("members") if isinstance(after, Mapping) else None
    members = report.get("members")
    if not isinstance(after, Mapping) or after.get("revision_id") != report["revision_id"]:
        raise WorkflowImportError("VibeComfy import report after revision does not match its origin revision")
    if not isinstance(after_members, Mapping) or not isinstance(members, Mapping):
        raise WorkflowImportError("VibeComfy import report must include exact after/member digests")
    expected = {name: _sha256(data) for name, data in required_bytes.items()}
    if any(members.get(name) != digest for name, digest in expected.items()):
        raise WorkflowImportError("VibeComfy import report member digests do not match returned bytes")
    if any(after_members.get(name) != digest for name, digest in expected.items()):
        raise WorkflowImportError("VibeComfy after revision member digests do not match returned bytes")
    if set(members) != set(_MEMBER_NAMES) or set(after_members) != set(_MEMBER_NAMES):
        raise WorkflowImportError("VibeComfy import report must name exactly the canonical bundle members")
    return required_bytes, report


def import_workflow(source_path: Path, workflow_id: str, out_dir: Path) -> dict[str, Path]:
    """Import raw source through the shared VibeComfy service and publish four outputs."""
    workflow_id = workflow_id.strip()
    if not workflow_id:
        raise WorkflowImportError("workflow_id must be a non-empty stable identity")
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise WorkflowImportError(f"source must be a readable JSON file: {exc}") from exc

    try:
        from vibecomfy.porting.import_service import import_workflow_bytes

        artifacts = import_workflow_bytes(source_bytes, workflow_id=workflow_id)
    except WorkflowImportError:
        raise
    except Exception as exc:
        raise WorkflowImportError(f"VibeComfy import service rejected source: {exc}") from exc

    member_bytes, report = _validate_artifacts(
        artifacts,
        source_bytes=source_bytes,
        workflow_id=workflow_id,
    )
    try:
        report_bytes = (
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkflowImportError(f"VibeComfy import report is not JSON serializable: {exc}") from exc

    outputs = {
        "python": ("workflow.py", member_bytes["workflow.py"]),
        "companion": ("workflow.vibe.json", member_bytes["workflow.vibe.json"]),
        "source": ("source.json", member_bytes["source.json"]),
        "report": ("edit-report.json", report_bytes),
    }
    if out_dir.exists() and any(out_dir.iterdir()):
        raise WorkflowImportError(f"output directory is not empty: {out_dir}")

    staging_path: Path | None = None
    try:
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_path = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.", dir=out_dir.parent))
        output_paths: dict[str, Path] = {}
        for name, (filename, data) in outputs.items():
            path = staging_path / filename
            path.write_bytes(data)
            output_paths[name] = out_dir / filename
        if out_dir.exists():
            out_dir.rmdir()
        os.replace(staging_path, out_dir)
        staging_path = None
    except OSError as exc:
        raise WorkflowImportError(f"could not publish complete canonical origin bundle: {exc}") from exc
    finally:
        if staging_path is not None:
            shutil.rmtree(staging_path, ignore_errors=True)
    return output_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the canonical editable VibeComfy bundle from admitted source JSON."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        import_workflow(args.source, args.workflow_id, args.out)
    except WorkflowImportError as exc:
        render_astrid_error(
            AstridError(
                f"vibecomfy.import: {exc}",
                recovery_command="invoke vibecomfy.import through astrid.sdk.invoke with its declared inputs",
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
