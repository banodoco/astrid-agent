"""A generated, stopped reference data root for ``astrid dev journey --reference generated``.

One project, three short ffmpeg test clips and one media-only timeline.  It is built the way the
Runtime's own tests build realm content: ``astrid_runtime workspace create`` (no launcher, no daemon)
for the catalog, source profile and empty realm, then ``RuntimeService`` in this process to fill it.
The result is shaped like a real reference (``runtime/catalog.json``, ``runtime/source-profiles/astrid.json``,
``runtime/realms/<id>`` with ``realm.sqlite3``), so the journey adopts it exactly as it adopts a film.
"""
from __future__ import annotations

import copy
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
REFERENCE_PUBLISHER = "journey-reference"


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
    """The reference timeline: the clips end to end on one visual track, nothing else.

    ``_seed_timeline`` commits its ``parent_composition`` and admits its ``dependency_manifest`` media.
    """
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
        "dependency_manifest": {"media": [{"media_id": m["object_id"], "content_digest": m["digest"]} for m in media]},
        "parent_composition": {"config": config,
                               "registry": {"assets": assets}, "clips": clips, "occurrences": []},
    }


def seeded_parent(publication: Mapping[str, Any]) -> dict[str, Any]:
    """The parent payload the seed commit must leave at the head: the publication's composition.

    The one difference from the retired publish is Runtime-owned: a committed parent keeps
    ``config.clips`` as an exact mirror of the root ``clips`` (the bootstrap head already carries both).
    """
    target = copy.deepcopy(dict(publication["parent_composition"]))
    target["config"] = {**target["config"], "clips": copy.deepcopy(target["clips"])}
    return target


def timeline_commit_body(publication: Mapping[str, Any], base_document: Mapping[str, Any],
                         base_publication_id: str) -> dict[str, Any]:
    """The guarded commitTimeline request that turns a fresh timeline's bootstrap head into ``publication``.

    ``base_document`` is the canonical closure of the bootstrap head that ``create_timeline`` made and
    ``base_publication_id`` its publication; both pins are the stale-head guard. ``doc.patch`` changes,
    each carrying its ``before`` guard, are diffed from that base (the clips at 0, 4 and 8 s, the
    registry assets and the visual track replacing the bootstrap V1/A1), then ``media.admit`` captures
    the ingested objects the clips name.
    """
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.emitter import emit_document_commands

    before = bundle_from_document(base_document)  # verifies the base closure and its digests
    target = seeded_parent(publication)
    after = copy.deepcopy(before)
    after["parent"].update({key: copy.deepcopy(target[key]) for key in ("config", "registry", "clips")})
    patches = emit_document_commands(before, after)
    admit = {"schema_version": 1, "op": "media.admit", "version": "1", "select": None,
             "args": {"inputs": [{"pin": dict(pin)} for pin in publication["dependency_manifest"]["media"]]}}
    # The admission is its own step: it records the inputs consumed at the final boundary.
    steps = [{"id": "seed-timeline", "label": "Seed reference clips", "intent": "", "tags": [],
              "command_ordinals": list(range(len(patches))),
              "patch": [copy.deepcopy(change) for call in patches for change in call["args"]["changes"]]},
             {"id": "seed-media", "label": "Admit reference media", "intent": "", "tags": [],
              "command_ordinals": [len(patches)], "patch": []}]
    return {"api_version": 1, "project_id": publication["project_id"], "timeline_id": TIMELINE_ID,
            "base_revision_id": base_document["parent_revision"]["revision_id"],
            "base_publication_id": base_publication_id, "commands": [*patches, admit],
            "label": "Seed reference timeline", "steps": steps, "merge": "atomic", "dry_run": False}


def _offline_authorize() -> None:
    """The reference is built offline in this process: there is no bearer to check."""


def _seed_timeline(service: Any, publication: Mapping[str, Any]) -> dict[str, Any]:
    """Commit ``publication`` onto the fresh timeline's bootstrap head and return the accepted result."""
    from runtime_protocol.timeline_commit_port import route_path

    project_id = publication["project_id"]
    head = service.list_timeline_commits(project_id, TIMELINE_ID, authorize=_offline_authorize)["head"]
    parent = service.get_project_parent_composition_revision(project_id, TIMELINE_ID, head["revision_id"])
    base = {"schema_version": 1, "project_id": project_id, "timeline_id": TIMELINE_ID,
            "parent_revision": {key: parent[key] for key in ("revision_id", "content_digest", "payload")},
            "shot_revisions": [], "internal_timeline_revisions": [],
            "dependency_manifest": parent["dependency_manifest"]}
    accepted = service.commit_timeline(
        project_id, TIMELINE_ID, timeline_commit_body(publication, base, head["publication_id"]),
        idempotency_key="journey-publish", publisher=REFERENCE_PUBLISHER,
        invocation_path=route_path(project_id, TIMELINE_ID), authorize=_offline_authorize)["data"]
    if accepted["status"] != "accepted":
        raise ReferenceBuildError(f"seed commit was not accepted: {accepted['status']}")
    payload = copy.deepcopy(accepted["canonical_document"]["parent_revision"]["payload"])
    if payload != seeded_parent(publication):
        raise ReferenceBuildError("seed commit left a different timeline than the reference publication")
    return accepted


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

    from astrid.sdk.timeline.commit import (
        create_runtime_commit_service,  # the Agent's commit provider
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
        # The Agent's provider executes commitTimeline. Registration is startup-only and must precede
        # create_timeline, which then bootstraps the editable head the seed commit is guarded on.
        service.register_timeline_commit_provider(create_runtime_commit_service())
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
        _seed_timeline(service, media_only_publication(project["id"], media, seconds=seconds))
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
