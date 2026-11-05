"""Render one admitted iteration selection through the public Rendering action."""
from __future__ import annotations


from astrid.pack_api import owned_by_current_user
from astrid.pack_api import canonical_runtime_entrypoint, guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("video_editing.iteration_video")

import argparse
import hashlib
import json
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from runtime_protocol.dirfd import filesystem as os

from astrid import invoke
from astrid.pack_api import build_manifest, write_manifest
from astrid.core.foundation.paths import REPO_ROOT
from astrid.packs.video_editing.shared.iteration_inputs import (
    MAX_DOCUMENT_BYTES,
    assembly_inputs,
    parse_frozen_inputs,
    pathless_render_registry,
)

# Reuse the migrated offline action, never the historical orchestrator.
with canonical_runtime_entrypoint("iteration.assemble"):
    from astrid.packs.iteration.actions.assemble.run import assemble_iteration


class IterationVideoError(RuntimeError):
    pass


def _read_regular(path: Path, *, size: int | None = None, sha256: str | None = None,
                  maximum: int = MAX_DOCUMENT_BYTES) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not owned_by_current_user(fd, before)
                or before.st_size > maximum or size is not None and before.st_size != size):
            raise IterationVideoError("input is not an admitted regular file of the expected size")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(1048576, maximum - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise IterationVideoError("input exceeds its finite byte bound")
            chunks.append(chunk)
        data = b"".join(chunks)
        after, current = os.fstat(fd), os.stat(path, follow_symlinks=False)
        def identity(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (identity(before) != identity(after) or identity(after) != identity(current)
                or total != before.st_size or sha256 is not None and hashlib.sha256(data).hexdigest() != sha256):
            raise IterationVideoError("input bytes changed or failed identity verification")
        return data
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def _producer(path: Path, output_root: Path, port: str, media_type: str) -> dict[str, str]:
    return {"filename": path.relative_to(output_root).as_posix(),
            "output_port": port, "media_type": media_type}


def run(*, project_id: str, target_run_id: str, frozen_inputs: Path,
        media_dependency: Path, out: Path, theme: Path | None = None,
        force: bool = False) -> dict[str, str]:
    """Consume admitted files locally and await one public rendering child."""
    if type(force) is not bool:
        raise IterationVideoError("force must be a boolean")
    frozen = parse_frozen_inputs(_read_regular(Path(frozen_inputs)),
                                 project=project_id, target_run_id=target_run_id)
    if len(frozen["media_bindings"]) != 1:
        raise IterationVideoError("iteration_video requires exactly one admitted media binding")
    binding = frozen["media_bindings"][0]
    media_path = Path(media_dependency).absolute()
    data = _read_regular(media_path, size=binding["size"], sha256=binding["sha256"])
    root = Path(out).resolve()
    root.mkdir(parents=True, exist_ok=True)
    staged_media = root / "render-inputs" / "media" / binding["filename"]
    _write_new(staged_media, data)
    materialized = {binding["name"]: {"path": str(staged_media), **{
        name: binding[name] for name in ("object_id", "sha256", "size", "media_type", "filename")}}}
    kwargs = assembly_inputs(frozen, materialized, project=project_id, target_run_id=target_run_id)
    assembled = assemble_iteration(out_path=root / "assembly", repo_root=REPO_ROOT,
                                   force=force, **kwargs)
    timeline_source = Path(assembled["hype_timeline_path"]).absolute()
    registry_source = Path(assembled["hype_assets_path"]).absolute()
    if (not timeline_source.resolve(strict=True).is_relative_to(root)
            or not registry_source.resolve(strict=True).is_relative_to(root)):
        raise IterationVideoError("offline assembly returned output outside this attempt")
    registry = pathless_render_registry(json.loads(_read_regular(registry_source)), frozen,
                                       project=project_id, target_run_id=target_run_id)
    for asset in registry["assets"].values():
        asset["binding"] = "media_dependency"
    timeline_path = root / "render-inputs" / "timeline.json"
    registry_path = root / "render-inputs" / "assets.json"
    _write_new(timeline_path, _read_regular(timeline_source))
    _write_new(registry_path, json.dumps(registry, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False).encode())
    render_inputs: dict[str, Any] = {
        "timeline": _producer(timeline_path, root, "timeline", "application/json"),
        "assets_registry": _producer(registry_path, root, "assets_registry", "application/json"),
        "media_dependency": _producer(staged_media, root, "media_dependency", binding["media_type"]),
        "output_name": "hype.mp4",
    }
    if theme is not None:
        theme_path = root / "render-inputs" / "theme.json"
        _write_new(theme_path, _read_regular(Path(theme)))
        render_inputs["theme"] = _producer(theme_path, root, "theme", "application/json")
    result = invoke("rendering.render", kind="action", inputs=render_inputs,
                    child_key="iteration-render", wait=True)
    if result.ok is not True:
        # Preserve the bounded public child error. Collapsing this to a bare
        # boolean made bridge/admission failures indistinguishable from a
        # renderer failure and forced operators into blind retries.
        error = getattr(result, "error", None)
        if isinstance(error, Mapping):
            try:
                detail = json.dumps(dict(error), sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                detail = str(error)
        else:
            detail = str(error) if error else "no structured child error"
        raise IterationVideoError(f"rendering.render did not succeed: {detail}")
    rows = result.outputs.get("managed_outputs")
    if not isinstance(rows, list):
        raise IterationVideoError("rendering.render returned no managed outputs")
    chosen = {}
    for port in ("video", "provenance"):
        matches = [row for row in rows if isinstance(row, Mapping) and row.get("output_port") == port]
        if len(matches) != 1:
            raise IterationVideoError(f"rendering.render requires one {port} output")
        chosen[port] = matches[0]
    outputs = {}
    for port, filename in (("video", "hype.mp4"), ("provenance", "hype.mp4.provenance.json")):
        row = chosen[port]
        local = result.materialize_output(row["association_id"])
        relative = local.filename
        if (type(relative) is not str or not relative or PurePosixPath(relative).is_absolute()
                or "\\" in relative or any(part in {"", ".", ".."} for part in relative.split("/"))
                or dict(local.output) != dict(row)):
            raise IterationVideoError("rendering.render materialized output identity changed")
        source = root / relative
        if not source.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise IterationVideoError("rendering.render materialized output escaped this attempt")
        payload = _read_regular(source, size=row["size"], sha256=row["digest"].removeprefix("sha256:"))
        destination = root / filename
        _write_new(destination, payload)
        outputs[port] = str(destination)
    receipt = build_manifest(
        kind="video_editing.iteration_video",
        inputs={
            "project_id": project_id,
            "target_run_id": target_run_id,
            "frozen_inputs": str(Path(frozen_inputs)),
            "media_dependency": str(media_path),
            "theme": str(Path(theme)) if theme is not None else None,
        },
        outputs=[
            {"name": "video", "path": "hype.mp4", "type": "file",
             "role": "result", "is_primary": True, "output_port": "video",
             "media_type": "video/mp4"},
            {"name": "provenance", "path": "hype.mp4.provenance.json", "type": "file",
             "role": "auxiliary", "is_primary": False, "output_port": "provenance",
             "media_type": "application/json"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(root / "manifest.json", receipt)
    return outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consume one admitted offline iteration-video envelope.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--target-run-id", required=True)
    parser.add_argument("--frozen-inputs", type=Path, required=True)
    parser.add_argument("--media-dependency", type=Path, required=True)
    parser.add_argument("--theme", type=Path)
    parser.add_argument("--force", action="store_true",
                        help="Preserve the existing quality-floor override and record forced=true.")
    parser.add_argument("--out", type=Path, required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()

    def execute() -> int:
        args = parser.parse_args(argv)
        print(json.dumps(run(project_id=args.project_id, target_run_id=args.target_run_id,
                             frozen_inputs=args.frozen_inputs, media_dependency=args.media_dependency,
                             out=args.out, theme=args.theme, force=bool(args.force)), sort_keys=True))
        return 0
    return run_pack_main("video_editing.iteration_video", execute, argv=list(argv or ()))


if __name__ == "__main__":
    raise SystemExit(main())
