"""A generated, stopped reference data root for ``astrid dev journey --reference generated``.

One project, three short ffmpeg test clips and one media-only timeline.  It is built the way the
Runtime's own tests build realm content: ``astrid_runtime workspace create`` (no launcher, no daemon)
for the catalog, source profile and empty realm, then ``RuntimeService`` in this process to fill it.
The result is shaped like a real reference (``runtime/catalog.json``, ``runtime/source-profiles/astrid.json``,
``runtime/realms/<id>`` with ``realm.sqlite3``), so the journey adopts it exactly as it adopts a film.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_SLUG = "journey"
TIMELINE_ID = "main"
REALM_ID = "realm-journey"
CLIP_COUNT = 3
CLIP_SECONDS = 4
CLIP_SIZE = "640x360"
TIMELINE_SECONDS = CLIP_COUNT * CLIP_SECONDS
FFMPEG_SECONDS = 120
TINY_CLIP_SECONDS = 1
TINY_CLIP_SIZE = "320x180"


class ReferenceBuildError(RuntimeError):
    """The generated reference could not be built (missing tool, refused command, bad content)."""


def ffmpeg_clip_argv(output: Path, index: int, *, seconds: int = CLIP_SECONDS,
                     size: str = CLIP_SIZE) -> list[str]:
    """One test clip: ``testsrc2`` video plus a sine tone (a different pitch per clip), h264/aac."""
    return ["ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency={220 * (index + 1)}:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(output)]


def source_manifest(source_checkout: Path, runtime_checkout: Path, runtime_environment: str) -> dict[str, str]:
    return {"profile": "astrid", "source_checkout": str(source_checkout),
            "runtime_checkout": str(runtime_checkout), "runtime_environment": runtime_environment}


def media_only_publication(project_id: str, media: Sequence[Mapping[str, Any]], *, seconds: int = CLIP_SECONDS) -> dict[str, Any]:
    """The parent-composition publication: the clips end to end on one visual track, nothing else."""
    assets = {f"clip{n}": {"media_id": m["object_id"], "content_sha256": m["object_id"], "type": "video"}
              for n, m in enumerate(media, 1)}
    clips = [{"id": f"clip-{n}", "clipType": "media", "track": "video", "asset": f"clip{n}",
              "at": seconds * (n - 1), "from": 0, "to": seconds, "speed": 1}
             for n in range(1, len(media) + 1)]
    config: dict[str, Any] = {"tracks": [{"id": "video", "kind": "visual", "label": "Video"}], "effects": []}
    if seconds == TINY_CLIP_SECONDS:
        config["theme_overrides"] = {"visual": {"canvas": {"width": 320, "height": 180, "fps": 24}}}
    return {
        "project_id": project_id, "timeline_id": TIMELINE_ID, "expected_head": None,
        "parent_revision_id": "parent-1",
        "dependency_manifest": {"media": [{"media_id": m["object_id"], "content_digest": m["digest"]} for m in media]},
        "parent_composition": {"config": config,
                               "registry": {"assets": assets}, "clips": clips, "occurrences": []},
    }


def timeline_commit_body(media: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The commit_timeline body that seeds the media-only reference timeline.

    Only the ops the repository already uses are emitted: ``media.admit`` with pins shaped
    like the publication's dependency manifest (astrid/sdk/timeline/authoring_commit.py).
    Unconfirmed parts are TODO and must be checked against Runtime 42ae24aff before this
    seed is trusted. The media-only publication in ``media_only_publication`` is still the
    reference shape for the clips.
    """
    return {
        "api_version": 1,
        "timeline_id": TIMELINE_ID,
        # TODO(unverified): base_revision_id for a fresh, empty timeline; confirm the empty-head value.
        "base_revision_id": None,
        # TODO(unverified): the guard field name and shape for a fresh timeline (expected_head in the old publish).
        "expected_head": None,
        "commands": [
            {"schema_version": 1, "op": "media.admit", "version": "1", "select": None,
             "args": {"inputs": [{"pin": {"media_id": m["object_id"], "content_digest": m["digest"]}}
                                 for m in media]}},
            # TODO(unverified): clip placement commands for the three clips at 0, 4 and 8 seconds.
            # No in-tree EditCommand creates a media clip on a fresh timeline, so none are guessed here.
        ],
    }


def _run(argv: list[str], env: Mapping[str, str], what: str, timeout: float | None) -> str:
    done = subprocess.run(argv, env=dict(env), capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode:
        told = " | ".join(f"{label}: {text.strip()[-800:]}" for label, text in (("stderr", done.stderr), ("stdout", done.stdout)) if text.strip())
        raise ReferenceBuildError(f"{what} failed (exit {done.returncode}): {told or 'no output'}")
    return done.stdout


def build_reference(workdir: Path, *, env: Mapping[str, str], source_checkout: Path, runtime_checkout: Path,
                    runtime_environment: str, tiny: bool = False) -> Path:
    """Build the reference under ``workdir/reference`` and return that data root (stopped, never started)."""
    if shutil.which("ffmpeg", path=env.get("PATH")) is None:
        raise ReferenceBuildError("ffmpeg is not on PATH; the generated reference needs it for its test clips")
    from runtime_protocol.service import (
        RuntimeService,  # the same Runtime the journey will adopt with
    )

    root = workdir / "reference"
    realm = root / "runtime" / "realms" / REALM_ID
    clips = workdir / "clips"
    clips.mkdir(parents=True)
    manifest = workdir / "reference-source-profile.json"
    manifest.write_text(json.dumps(source_manifest(source_checkout, runtime_checkout, runtime_environment)))
    child = dict(env, ASTRID_RUNTIME_DATA_ROOT=str(root))
    _run([sys.executable, "-m", "astrid_runtime", "workspace", "create", "--profile", "astrid", "--data-root", str(root),
          "--realm-root", str(realm), "--realm-id", REALM_ID, "--source-manifest", str(manifest), "--json"],
         child, "workspace create", None if tiny else 120)
    service = RuntimeService(realm)
    try:
        project = service.create_project({"slug": PROJECT_SLUG, "name": "Journey"}, idempotency_key="journey-project")
        service.create_timeline(project["id"], TIMELINE_ID, idempotency_key="journey-timeline")
        media = []
        count = 1 if tiny else CLIP_COUNT
        seconds = TINY_CLIP_SECONDS if tiny else CLIP_SECONDS
        size = TINY_CLIP_SIZE if tiny else CLIP_SIZE
        for index in range(count):
            clip = clips / f"clip{index + 1}.mp4"
            _run(ffmpeg_clip_argv(clip, index, seconds=seconds, size=size), env,
                 f"ffmpeg clip {index + 1}", None if tiny else FFMPEG_SECONDS)
            media.append(service.ingest(project["id"], clip.read_bytes(), media_type="video/mp4", original_name=clip.name,
                                        idempotency_key=f"journey-clip-{index + 1}")["data"])
        # The retired publish_parent_composition route is replaced by the canonical commit route.
        service.commit_timeline(project["id"], TIMELINE_ID, timeline_commit_body(media),
                                idempotency_key="journey-publish")
    finally:
        service.close()
    shutil.rmtree(clips, ignore_errors=True)
    return root


def describe_reference(reference: Path) -> dict[str, Any]:
    """Open the reference's realm in this process and list what it holds (a read check, no daemon)."""
    from runtime_protocol.service import RuntimeService

    catalog = json.loads((reference / "runtime" / "catalog.json").read_text())
    realm = Path(catalog["realms"][0]["data_root"])
    service = RuntimeService(realm)
    try:
        projects = []
        for row in service.list_projects(limit=100)["items"]:
            pid = row["project_id"]
            timelines = [{"timeline_id": t["timeline_id"], "head": t["head_revision_id"]}
                         for t in service.list_timelines(pid, limit=100)["items"]]
            projects.append({"project_id": pid, "slug": row["slug"], "timelines": timelines,
                             "objects": len(service.list_project_objects(pid, limit=100)["items"])})
        return {"realm_id": catalog["selected_realm_id"], "projects": projects}
    finally:
        service.close()
