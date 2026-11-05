"""One-call, caller-authorized timeline capture and managed music.duck invocation."""
from __future__ import annotations

import json
import re
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path

from astrid.pack_api import AstridError
from astrid.pack_api import managed_file_digest
from astrid.pack_api import sha256_file
from astrid.core.timeline import clip_fields
from astrid.packs.music.actions.duck._audio import read_duck_tag
from astrid.packs.music.actions.duck.core import Placement, select_music_clip
from astrid.sdk.media_handles import resolve_media_handle, save_media
from astrid.sdk.timeline import Checkout


def prepare_snapshot(client, project: str, timeline: str, target: Path, *,
                     clip: str | None = None, voice_tracks: list[str] | None = None) -> dict:
    """Read a pinned published closure and package digest-verified audio for local admission."""
    tracks = ["vo"] if voice_tracks is None else voice_tracks
    if not isinstance(tracks, list) or not tracks or any(not isinstance(t, str) or not t for t in tracks):
        raise AstridError("voice_tracks must be a nonempty list of track ids")
    opened = client.timelines.open_bundle(project, timeline)
    if not opened.ok:
        raise AstridError(f"could not open duck timeline: {opened.error}")
    tl = Checkout(opened.data["bundle"])
    clips = list(tl.clips())
    selected = select_music_clip([c.data for c in clips], clip)
    bed = next(c for c in clips if c.data is selected)
    if not bed.is_audio:
        raise AstridError("selected music clip is not an audio clip")
    with tempfile.TemporaryDirectory(prefix="music-duck-capture-") as directory:
        root = Path(directory)
        objects = {}
        def materialize(digest: str) -> Path:
            if digest not in objects:
                descriptor, _ = resolve_media_handle(client, project, digest)
                destination = root / descriptor["digest"][7:]
                objects[digest] = save_media(client, descriptor["digest"], destination, project=project)
            return objects[digest]
        def row(c):
            entry = tl._registry(c.shot_id).get(c.asset) or {}
            digest = str(entry.get("media_id") or entry.get("object_id") or entry.get("digest")
                         or entry.get("content_digest") or entry.get("content_sha256") or "")
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                digest = "sha256:" + digest
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise AstridError(f"clip {c.id!r} has no managed source digest; import its asset first")
            data, params = c.data, c.data.get("params") or {}
            def look(name):
                value = clip_fields.read(data, name, fps=tl.fps, audio=True)
                return clip_fields.FIELDS[name].default if value is None else value
            holder = tl._internal(c.shot_id)
            rows = holder.get("tracks") or (holder.get("config") or {}).get("tracks") or []
            track = next((r for r in rows if r.get("id") == c.track), {})
            muted = bool(data.get("mute") or data.get("muted") or track.get("mute") or track.get("muted"))
            volume = float(look("volume")) * float(track.get("volume", 1.))
            placement = Placement(materialize(digest), c.start, c.duration,
                                  from_s=float(data.get("from", 0)), speed=float(look("speed")),
                                  volume=0. if muted else volume,
                                  fade_in=float(look("fade_in") or 0), fade_out=float(look("fade_out") or 0),
                                  id=c.id, fade_in_curve=params.get("fadeInCurve", "linear"),
                                  fade_out_curve=params.get("fadeOutCurve", "linear"))
            fields = asdict(placement)
            fields.pop("path")
            return {"digest": digest, "placement": fields, "clip": data}
        music = row(bed)
        voices = [row(c) for c in clips if c.track in tracks and c.is_audio]
        # Include the original object behind a baked asset; never compound attenuation.
        path = materialize(music["digest"])
        seen = set()
        while tag := read_duck_tag(path):
            digest = tag.get("source_digest")
            if not isinstance(digest, str) or digest in seen:
                raise AstridError("invalid music.duck source tag; restore the original source")
            seen.add(digest)
            try:
                path = materialize(digest)
            except Exception as exc:
                raise AstridError(f"original duck source {digest} cannot be found; restore it before re-running") from exc
        spec = {"schema_version": 1, "project": opened.data["project_id"],
                "timeline": opened.data["timeline_id"], "revision_id": opened.data["revision_id"],
                "voice_tracks": tracks, "music": music, "voices": voices, "objects": sorted(objects)}
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("snapshot.json", json.dumps(spec, sort_keys=True, separators=(",", ":")))
            for digest, path in sorted(objects.items()):
                archive.write(path, "objects/" + digest[7:])
    return spec


def duck_timeline(client, project: str, timeline: str, *, clip: str | None = None,
                  voice_tracks: list[str] | None = None,
                  bed_lu: float = 8., wait: bool = True):
    """One agent call: capture current placements, admit music.duck, return its managed result."""
    with tempfile.TemporaryDirectory(prefix="music-duck-admit-") as directory:
        snapshot = Path(directory) / "snapshot.zip"
        prepare_snapshot(client, project, timeline, snapshot, clip=clip, voice_tracks=voice_tracks)
        imported = client.media.import_file(
            project=project, path=snapshot,
            idempotency_key="music-duck-snapshot-" + sha256_file(snapshot),
        )
        if not imported.ok:
            raise AstridError(f"could not import music.duck snapshot: {imported.error}")
        digest = managed_file_digest(imported.data, "snapshot")
        return client.invoke_result("music.duck", kind="action", project=project,
                                    inputs={"snapshot": digest, "bed_lu": bed_lu}, wait=wait)
