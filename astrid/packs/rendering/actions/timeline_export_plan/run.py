"""Read-only Runtime timeline export planner."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from astrid.pack_api import guard_canonical_entrypoint
from astrid.packs.rendering.actions.timeline_transfer_common import (
    open_worker_runtime_client,
    output_root,
    run_cli,
    write_artifact,
    write_receipt,
)
from astrid.sdk.timeline_transfer import export_plan

guard_canonical_entrypoint("rendering.timeline_export_plan")


def _summary(plan: dict[str, Any], *, project: str, timeline: str) -> dict[str, Any]:
    manifest = plan.get("manifest") or {}
    plan_timeline = plan.get("timeline") or {}
    media = plan.get("media") or []
    transcripts = plan.get("transcripts") or {}
    return {
        "project": project,
        "timeline": timeline,
        "name": str(manifest.get("name") or plan_timeline.get("timeline_id") or "Timeline"),
        "media_count": len(media),
        "media_bytes": sum(int(row.get("size") or 0) for row in media if isinstance(row, dict)),
        "transcript_count": len(transcripts),
    }


def build_export_plan_artifacts(client: Any, project: str, timeline: str, out_dir: str | Path) -> dict[str, Any]:
    root = output_root(str(out_dir))
    plan = export_plan(client, project, timeline)
    summary = _summary(plan, project=project, timeline=timeline)
    plan_path, result_path = root / "plan.json", root / "result.json"
    write_artifact(plan_path, plan)
    write_artifact(result_path, summary)
    write_receipt(root, kind="rendering.timeline_export_plan", inputs={"project": project, "timeline": timeline}, outputs=[
        {"name": "plan", "path": plan_path.name, "role": "result", "is_primary": True, "type": "file"},
        {"name": "result", "path": result_path.name, "role": "auxiliary", "is_primary": False, "type": "file"},
    ])
    return summary


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        parser = argparse.ArgumentParser(description="Build a read-only timeline export plan.")
        parser.add_argument("--project", required=True)
        parser.add_argument("--timeline", required=True)
        parser.add_argument("--out", required=True)
        args = parser.parse_args(argv)
        client = open_worker_runtime_client()
        build_export_plan_artifacts(client, args.project, args.timeline, args.out)
        return 0

    return run_cli("rendering.timeline_export_plan", _run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
