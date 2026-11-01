#!/usr/bin/env python3
"""Run the installed-only T-11 Timeline example and materialize its render."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


def _json_command(argv: list[str], *, env: dict[str, str], cwd: Path) -> dict:
    completed = subprocess.run(
        argv, cwd=cwd, env=env, text=True, capture_output=True, check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {argv!r}; "
            f"stderr={completed.stderr[-4000:]!r}"
        )
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command returned invalid JSON: {argv!r}") from exc
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise RuntimeError(f"command returned unsuccessful envelope: {argv!r}: {envelope!r}")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"command returned no data object: {argv!r}")
    return data


def _module_origin(name: str) -> str | None:
    spec = importlib.util.find_spec(name)
    return str(Path(spec.origin).resolve()) if spec and spec.origin else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--agent-source", required=True, type=Path)
    parser.add_argument("--runtime-source", required=True, type=Path)
    parser.add_argument("--app-source", required=True, type=Path)
    parser.add_argument("--agent-sha", required=True)
    parser.add_argument("--runtime-sha", required=True)
    parser.add_argument("--app-sha", required=True)
    parser.add_argument("--pinokio-sha", required=True)
    args = parser.parse_args()

    work = args.work_dir.resolve()
    output = args.output.resolve()
    data_root = args.data_root.resolve()
    profile_path = args.profile.resolve()
    source_roots = [args.agent_source.resolve(), args.runtime_source.resolve(), args.app_source.resolve()]
    if any(work == root or work.is_relative_to(root) for root in source_roots):
        raise SystemExit("work directory must be outside all three source checkouts")
    if not work.is_dir():
        raise SystemExit("work directory must already exist")
    if output.parent != work:
        raise SystemExit("render output must be directly inside the isolated work directory")

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    python = Path(profile["runtime_environment"]) / "bin" / "python"
    if not python.is_file() or python.resolve() != Path(sys.executable).resolve():
        raise SystemExit("runner must use the Python recorded by the installed source profile")

    env = dict(os.environ)
    env.update({
        "ASTRID_RUNTIME_SOURCE_MANIFEST": str(profile_path),
        "ASTRID_RUNTIME_DATA_ROOT": str(data_root),
        "HOME": str(data_root.parent / "home"),
        "PATH": str(python.parent) + os.pathsep + os.defpath,
        "PYTHONPATH": "",
        "PYTHONHOME": "",
    })
    receipt = {
        "status": "partial-not-qualified",
        "tuple": {
            "agent_sha": args.agent_sha,
            "runtime_sha": args.runtime_sha,
            "app_sha": args.app_sha,
            "pinokio_sha": args.pinokio_sha,
        },
        "working_directory": str(work),
        "interpreter": str(Path(sys.executable).resolve()),
        "sys_path": list(sys.path),
        "module_origins": {
            name: _module_origin(name)
            for name in ("astrid", "astrid.sdk.timeline.api", "runtime_protocol")
        },
        "checkout_imports": "unobserved-until-origin-check",
        "verified_schema": "recorded by installer harness static tuple comparison",
        "schema_consumption": "unobserved",
        "recorded_composition_archive": "recorded by installer harness static archive comparison",
        "composition_execution": "unobserved",
        "example": {},
        "render": {},
    }
    phase = "origin-check"

    try:
        imported_paths = [Path(value).resolve() for value in sys.path if value]
        if any(path == root or path.is_relative_to(root) for path in imported_paths for root in source_roots):
            raise RuntimeError("sys.path contains a product source checkout")
        origins = [Path(value).resolve() for value in receipt["module_origins"].values() if value]
        if any(origin == root or origin.is_relative_to(root) for origin in origins for root in source_roots):
            raise RuntimeError("an imported module originated in a product source checkout")
        receipt["checkout_imports"] = "none-observed-in-sys-path-or-module-origins"

        phase = "project-create"
        suffix = uuid.uuid4().hex[:12]
        project_slug = f"t11-example-{suffix}"
        timeline_id = f"t11-{suffix}"
        project_data = _json_command(
            [str(python), "-I", "-m", "astrid", "projects", "create", project_slug,
             "--name", "T-11 installed example", "--json"], env=env, cwd=work,
        )
        project_record = project_data.get("project") if isinstance(project_data.get("project"), dict) else project_data
        project_id = project_record.get("project_id") or project_record.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise RuntimeError("project create returned no project_id or id")
        timeline_data = _json_command(
            [str(python), "-I", "-m", "astrid", "timelines", "create", "--project", project_id,
             "--slug", timeline_id, "--name", "T-11 example", "--canvas", "640x360", "--json"],
            env=env, cwd=work,
        )
        created_timeline_id = timeline_data.get("timeline_id")
        if not isinstance(created_timeline_id, str) or not created_timeline_id:
            raise RuntimeError("timeline create returned no timeline_id")
        timeline_id = created_timeline_id

        phase = "timeline-commit-readback"
        from astrid.sdk.timeline.api import Timeline

        timeline = Timeline.open(project_id, timeline_id)
        commit = timeline.commit(
            lambda edit: edit.add_text("Installed Timeline example", at=0, hold=2, id="t11-title"),
            label="T-11 installed example",
            idempotency_key=f"t11-example-{suffix}",
        )
        commit_data = commit.get("data") if isinstance(commit, dict) else None
        if not isinstance(commit_data, dict) or not commit_data.get("accepted_revision_id"):
            raise RuntimeError("Timeline.commit returned no accepted_revision_id")
        accepted_revision = str(commit_data["accepted_revision_id"])
        publication_id = (commit.get("receipt") or {}).get("receipt_id") if isinstance(commit, dict) else None

        readback = Timeline.open(project_id, timeline_id)
        clip = readback.clip("t11-title")
        head_revision = str(readback.head.get("revision_id") or "")
        if clip.text != "Installed Timeline example" or head_revision != accepted_revision:
            raise RuntimeError("Runtime readback did not match the committed text and accepted revision")
        receipt["example"] = {
            "project_id": project_id,
            "project_slug": project_slug,
            "timeline_id": timeline_id,
            "commit_id": publication_id,
            "accepted_revision_id": accepted_revision,
            "readback_head_revision_id": head_revision,
            "readback_clip_id": clip.id,
            "readback_text": clip.text,
            "result": "committed-and-read-back",
        }

        phase = "render"
        render_data = _json_command(
            [str(python), "-I", "-m", "astrid", "timelines", "render", timeline_id,
             "--project", project_id, "--backend", "rendering.remotion", "--wait", "--json"],
            env=env, cwd=work,
        )
        run_id = render_data.get("kernel_run_id") or render_data.get("run_id")
        task_id = render_data.get("kernel_task_id")
        if not run_id or render_data.get("state") != "completed":
            raise RuntimeError("render did not return a completed state and run identity")
        opened = _json_command(
            [str(python), "-I", "-m", "astrid", "runs", "open", str(run_id), "--project", project_id,
             "--timeline", timeline_id, "--no-open", "--json"], env=env, cwd=work,
        )
        materialized = Path(str(opened.get("local_path") or "")).resolve(strict=True)
        if not materialized.is_file():
            raise RuntimeError("runs open did not materialize a regular file")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(materialized, output)
        payload = output.read_bytes()
        if not payload:
            raise RuntimeError("materialized render is empty")
        receipt["render"] = {
            "run_id": str(run_id),
            "task_id": task_id,
            "result": "completed",
            "managed_output_reference": opened.get("managed_object_reference"),
            "source_path": str(materialized),
            "output_path": str(output),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "independently_readable": True,
        }
    except Exception as exc:
        receipt["status"] = "partial-failed"
        receipt["error"] = {"phase": phase, "type": type(exc).__name__}
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 1

    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
