"""Working-copy paths and conflict-aware JSON merge helpers."""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from runtime_protocol import platform

from astrid.core.timeline import astrid_ns as _ns
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.doc import _serialize

LOCK_WAIT_ENV = "ASTRID_TIMELINE_LOCK_TIMEOUT"
LOCK_WAIT_SECONDS = 30.0


def _cli_running() -> bool:
    import sys
    return bool(getattr(sys.modules.get("astrid.packs.timeline.cli"), "_IN_VERB", False))


def _is_draft(path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(drafts_root()).resolve())
        return True
    except (OSError, ValueError):
        return False


def _history_dir(path: Path) -> Path:
    return Path(path).with_suffix(".history")


def _redo_dir(path: Path) -> Path:
    return Path(path).with_suffix(".redo")


def _snapshot(checkout: Any) -> str:
    return json.dumps(checkout.bundle, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _stage_step(folder: Path, label: str, state: str) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex[:4]}.json"
    temporary = folder / f".{name}.tmp"
    try:
        temporary.write_text(json.dumps({"label": str(label), "state": state}), encoding="utf-8")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary, folder / name


def _write_step(folder: Path, label: str, state: str) -> None:
    temporary, final = _stage_step(folder, label, state)
    os.replace(temporary, final)


def _read_step(path: Path) -> tuple[str, str]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        value = json.loads(text)
        if isinstance(value, dict) and "state" in value:
            return str(value.get("label") or "an edit"), str(value["state"])
    except ValueError:
        pass
    return "an edit", text


def _discard_half_written(folder: Path) -> None:
    for temporary in Path(folder).glob(".*.tmp"):
        temporary.unlink(missing_ok=True)


def _restore_marker(target: Path) -> Path:
    return target.with_name(f".{target.name}.restore.json")


def _stage_restore(target: Path, temporary: Path, *, redo: bool,
                   sources: list[Path], staged: list[tuple[Path, Path]]) -> None:
    """Describe only this local stack move, before atomically replacing its draft."""
    stat = temporary.stat()
    marker = _restore_marker(target)
    pending = marker.with_suffix(".tmp")
    try:
        pending.write_text(json.dumps({
            "redo": redo, "target_identity": [stat.st_dev, stat.st_ino],
            "temporary": temporary.name, "sources": [step.name for step in sources],
            "reverse": [step.name for _tmp, step in staged],
        }), encoding="utf-8")
        os.replace(pending, marker)
    finally:
        pending.unlink(missing_ok=True)


def _reconcile_restore(target: Path) -> None:
    """Finish an interrupted local restore under the checkout lock; never use JSON equality."""
    marker = _restore_marker(target)
    if not marker.is_file():
        return
    move = json.loads(marker.read_text(encoding="utf-8"))
    source, destination = ((_redo_dir, _history_dir) if move["redo"] else
                           (_history_dir, _redo_dir))
    stat = target.stat()
    replaced = [stat.st_dev, stat.st_ino] == move["target_identity"]
    for name in move["reverse"]:
        step = destination(target) / name
        temporary = step.with_name(f".{name}.tmp")
        if replaced:
            if temporary.is_file():
                os.replace(temporary, step)
            elif not step.is_file():
                raise TimelineEditError("interrupted draft restore is missing a reverse step")
        else:
            temporary.unlink(missing_ok=True)
    if replaced:
        for name in move["sources"]:
            (source(target) / name).unlink(missing_ok=True)
    (target.parent / move["temporary"]).unlink(missing_ok=True)
    marker.unlink()


@contextlib.contextmanager
def _file_lock(target: Path):
    """Serialize compare-and-write saves; the kernel releases the lock if its writer exits."""
    lock = Path(target).with_name(f".{Path(target).name}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+", encoding="utf-8") as handle:
        try:
            wait = max(0.0, float(os.environ.get(LOCK_WAIT_ENV) or LOCK_WAIT_SECONDS))
        except ValueError:
            wait = LOCK_WAIT_SECONDS
        deadline = time.monotonic() + wait
        while True:
            try:
                platform.lock_file(handle.fileno(), blocking=False)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.seek(0)
                    holder = handle.read().strip()
                    who = f"another writer (pid {holder})" if holder else "another writer"
                    raise TimelineEditError(
                        f"{who} has held this working copy's lock for {wait:g} s, so nothing was saved. "
                        f"The lock is {lock}. Wait for that process to finish and save again.")
                time.sleep(0.05)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            platform.unlock_file(handle.fileno())


def _journaled(function, kind):
    """Make one public mutator one undo step; nested calls join their active outer step."""
    from functools import wraps

    @wraps(function)
    def wrapped(owner, *args, **kwargs):
        checkout = owner if kind == "checkout" else (
            getattr(owner, "owner", None) or getattr(owner, "_tl", None) or getattr(owner, "tl", None)
        )
        if checkout is None:
            return function(owner, *args, **kwargs)
        if getattr(checkout, "_edit_depth", 0):
            return function(owner, *args, **kwargs)
        who = getattr(owner, "address", None) or getattr(owner, "segment", None) or ""
        shown = ", ".join([repr(value)[:40] for value in args]
                           + [f"{key}={value!r}"[:40] for key, value in kwargs.items()])
        label = f"{who + '.' if who else ''}{function.__name__}({shown})"
        with checkout.step(label):
            from . import journal

            capture = None
            if kind == "clip" and function.__name__ == "nudge":
                from inspect import signature

                bound = signature(function).bind(owner, *args, **kwargs)
                bound.apply_defaults()
                capture = journal.begin_nudge(owner, bound.arguments["seconds"], bound.arguments["frames"])
            result = function(owner, *args, **kwargs)
            if capture is not None:
                journal.finish_nudge(checkout, capture)
            return result
    return wrapped


# Runtime and draft seams added to the isolated modular SDK.


def drafts_root() -> Path:
    """Where working copies live: Astrid's data root (``ASTRID_RUNTIME_DATA_ROOT``) / drafts."""
    base = os.environ.get("ASTRID_RUNTIME_DATA_ROOT")
    if base:
        return Path(base) / "drafts"
    from astrid.sdk.storage_root import resolve_runtime_data_root

    return resolve_runtime_data_root() / "drafts"


def resolve_handle_entry(project: str, handle: str, *, client: Any = None) -> dict[str, Any]:
    """A media handle → a registry entry ``{key, media_id, content_sha256, type, resolution}`` (read-only)."""
    from astrid.sdk import AstridClient
    from astrid.sdk import media_handles as mh

    def run(c: Any) -> dict[str, Any]:
        resolver = getattr(mh, "resolve_timeline_asset", None)
        try:
            if callable(resolver):
                return dict(resolver(c, project, handle))
            descriptor, _lineage = mh.resolve_media_handle(c, project, handle)
        except mh.MediaHandleError as exc:
            raise TimelineEditError(f"{handle}: {exc}") from None
        name = Path(str(descriptor.get("filename") or "media")).stem
        return {"key": re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "media", "media_id": descriptor["digest"],
                "content_sha256": descriptor["digest"], "type": descriptor.get("media_type")}

    if client is not None:
        return run(client)
    with AstridClient.open_from_launcher(start_pack_host=False) as c:
        return run(c)


def _with_client(client: Any, fn: Any) -> Any:
    if client is not None:
        return fn(client)
    from astrid.sdk import AstridClient

    with AstridClient.open_from_launcher(start_pack_host=False) as c:
        return fn(c)


def load_beats(project: str, source: Any, *, client: Any = None) -> tuple[dict[str, Any], str]:
    """A beat grid from a beats file: a local JSON path, a media handle (``run:<id>/beats``, ``sha256:…``)
    or the parsed dict. Returns ``(stored grid, where it came from)``."""
    if isinstance(source, Mapping):
        label = str(source.get("source") or "a beats dict")
        return intent.beats_grid(source, label), label
    text = str(source).strip()
    path = Path(text).expanduser()
    if path.is_file():
        try:
            return intent.beats_grid(json.loads(path.read_text(encoding="utf-8")), path.name), path.name
        except (ValueError, OSError) as exc:
            raise TimelineEditError(f"{path}: not a beats file ({exc})") from None
    from astrid.sdk import media_handles as mh

    if not mh.is_media_handle(text):
        raise TimelineEditError(f"--beats {text!r}: not a file and not a media handle (run:<id>/beats, sha256:…)")

    def run(c: Any) -> tuple[dict[str, Any], str]:
        try:
            descriptor, _lineage = mh.resolve_media_handle(c, project, text)
            data = json.loads(c.media.read_bytes(descriptor["digest"]))
        except (mh.MediaHandleError, ValueError) as exc:
            raise TimelineEditError(f"--beats {text}: {exc}") from None
        try:
            return intent.beats_grid(data, text), text
        except ValueError as exc:
            raise TimelineEditError(f"--beats {text}: {exc}") from None

    return _with_client(client, run)


def handle_beats(project: str, handle: str, *, client: Any = None) -> tuple[dict[str, Any], str] | None:
    """The beat grid made with this media: the ``beats`` output of the run (or task) that made it
    (``chiptune.compose`` writes music + beats). None when that run made no beats file."""
    from astrid.sdk import media_handles as mh

    def run(c: Any) -> tuple[dict[str, Any], str] | None:
        try:
            _descriptor, lineage = mh.resolve_media_handle(c, project, handle)
        except mh.MediaHandleError:
            return None
        made = lineage.get("output") if isinstance(lineage.get("output"), Mapping) else {}
        kind, ident = ("run", made.get("run_id")) if made.get("run_id") else ("task", made.get("task_id"))
        if not ident:
            return None
        try:
            rows = mh._output_rows(c, kind, str(ident))
        except mh.MediaHandleError:
            return None
        if not any(r.get("output_port") == "beats" for r in rows):
            return None
        return load_beats(project, f"{kind}:{ident}/beats", client=c)

    try:
        return _with_client(client, run)
    except TimelineEditError:
        raise
    except Exception:  # noqa: BLE001 - no runtime: the caller says the beats did not come along
        return None


def file_beats(path: Path) -> tuple[dict[str, Any], str] | None:
    """A beats file beside a local music file: ``NAME.beats.json`` or the folder's ``beats.json``."""
    for candidate in (path.with_name(path.stem + ".beats.json"), path.with_name("beats.json")):
        if candidate.is_file():
            try:
                return load_beats("", candidate)
            except TimelineEditError:
                continue
    return None


def media_seconds(project: str, media: Any, *, client: Any = None) -> float | None:
    """How long an audio/video file plays: a local path, or a registry entry / digest in the project's
    media store. None when it cannot be read (no runtime, not media)."""
    import subprocess
    import tempfile

    from astrid.core.media import ffprobe_duration_seconds

    if isinstance(media, (str, Path)) and Path(str(media)).expanduser().is_file():
        try:
            return float(ffprobe_duration_seconds(Path(str(media)).expanduser()))
        except (subprocess.SubprocessError, ValueError, OSError):
            return None
    digest = str((media or {}).get("media_id") or (media or {}).get("content_sha256") or "") \
        if isinstance(media, Mapping) else str(media or "")
    if not digest.startswith("sha256:"):
        return None
    kind = str((media or {}).get("type") or "") if isinstance(media, Mapping) else ""

    def run(c: Any) -> float | None:
        data = c.media.read_bytes(digest)
        if kind in ("audio/wav", "audio/x-wav", "audio/wave") or data[:4] == b"RIFF":
            import io
            import wave

            try:
                with wave.open(io.BytesIO(data)) as w:
                    return w.getnframes() / float(w.getframerate())
            except (wave.Error, EOFError):
                pass
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=Path(kind.replace("/", ".")).suffix or ".bin", delete=False
            ) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)
            return float(ffprobe_duration_seconds(tmp_path))
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    try:
        return _with_client(client, run)
    except Exception:  # noqa: BLE001 - a length we cannot read is reported, never fatal
        return None


def _draft_text(snapshot: str) -> str:
    from astrid.sdk.timeline.checkout import Checkout
    return _serialize(Checkout(json.loads(snapshot))._annotated())


def _lock_wait() -> float:
    raw = os.environ.get(LOCK_WAIT_ENV)
    try:
        return max(0.0, float(raw)) if raw not in (None, "") else LOCK_WAIT_SECONDS
    except ValueError:
        return LOCK_WAIT_SECONDS


def _lock_holder(lock: Path) -> int | None:
    try:
        return int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _acquire_lock(handle: Any, lock: Path) -> None:
    import time as _time

    wait = _lock_wait()
    deadline = _time.monotonic() + wait
    while True:
        try:
            platform.lock_file(handle.fileno(), blocking=False)
            return
        except BlockingIOError:
            if _time.monotonic() >= deadline:
                break
            _time.sleep(0.05)
    holder = _lock_holder(lock)
    who = f"another writer (pid {holder})" if holder else "another writer"
    raise TimelineEditError(
        f"{who} has held this working copy's lock for {wait:g} s, so nothing was saved. The lock is {lock}. "
        "Wait for that process to finish (the lock frees when it exits); if it is stuck, stop it and save again.")


def draft_path(project_id: str, timeline_id: str, name: str = "main") -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name) or "main"
    return drafts_root() / str(project_id) / str(timeline_id) / f"{safe}.json"


def resolve_ids(project: str, timeline: str, *, client: Any = None) -> tuple[str, str, str]:
    """(project_id, timeline_id, head) for a project slug/id and a timeline slug/id."""
    from inspect import Parameter, signature

    from astrid.sdk import AstridClient

    def read(c: Any) -> tuple[str, str, str]:
        reader = c.timelines.open_composition
        # Custom readers may retain the two-argument interface.
        raw = any(parameter.name == "_raw_projection" or parameter.kind == Parameter.VAR_KEYWORD
                  for parameter in signature(reader).parameters.values())
        shown = reader(project, timeline, **({"_raw_projection": True} if raw else {}))
        if not shown.ok or not isinstance(shown.data, Mapping):
            raise TimelineEditError(f"could not open timeline {timeline!r} in project {project!r}: {shown.error}")
        native = shown.data.get("native_inspection") or {}
        return str(native.get("project_id")), str(native.get("timeline_id")), str(shown.data["summary"]["head_revision_id"])

    if client is not None:
        return read(client)
    with AstridClient.open_from_launcher(start_pack_host=False) as c:
        return read(c)


def find_draft(project: str, timeline: str, name: str | None = None, *, client: Any = None) -> Path | None:
    """The working copy for this timeline, if one exists: the named one, else the CURRENT one (the
    last checked out, ``checkout --draft NAME``), else "main"."""
    project_id, timeline_id, _head = resolve_ids(project, timeline, client=client)
    path = draft_path(project_id, timeline_id, name or current_draft(project_id, timeline_id))
    return path if path.is_file() else None


def current_draft(project_id: str, timeline_id: str) -> str:
    """The working copy commands use when none is named: the one last checked out (default "main")."""
    pointer = draft_path(project_id, timeline_id, "main").parent / ".current"
    try:
        name = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return "main"
    return name if name and draft_path(project_id, timeline_id, name).is_file() else "main"


def set_current_draft(project_id: str, timeline_id: str, name: str) -> None:
    pointer = draft_path(project_id, timeline_id, "main").parent / ".current"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(name, encoding="utf-8")


def fetch_bundle(project: str, timeline: str, *, revision_id: str | None = None,
                 publication_id: str | None = None, client: Any = None) -> dict[str, Any]:
    """The complete pinned closure (parent → shots → internal timelines) of a head or revision."""
    from astrid.sdk import AstridClient

    def read(c: Any) -> dict[str, Any]:
        options = {"publication_id": publication_id} if publication_id is not None else {}
        opened = c.timelines.open_bundle(project, timeline, revision_id=revision_id, **options)
        if not opened.ok or not isinstance(opened.data, Mapping):
            raise TimelineEditError(f"could not open {timeline!r} in {project!r}: {opened.error}")
        bundle = copy.deepcopy(dict(opened.data["bundle"]))
        if revision_id is None and publication_id is None:
            # A restored revision can belong to several publications. Only
            # Runtime's scoped head pair identifies the current publication.
            project_id, timeline_id = bundle["project_id"], bundle["timeline_id"]
            page = c.timelines.history(project_id, timeline_id, cursor=None, limit=1)
            if not page.ok:
                raise TimelineEditError(f"could not pin checkout publication: {page.error}")
            head = page.data.get("head") if isinstance(page.data, Mapping) else None
            if (not isinstance(head, Mapping)
                    or not isinstance(head.get("revision_id"), str) or not head["revision_id"]
                    or not isinstance(head.get("publication_id"), str) or not head["publication_id"]):
                raise TimelineEditError("checkout history omitted its exact head pair")
            if head["revision_id"] != bundle["base_parent"]["revision_id"]:
                raise TimelineEditError("checkout head moved while reading; reopen the timeline")
            hydrated = c.timelines.open_bundle(
                project_id, timeline_id, revision_id=head["revision_id"],
                publication_id=head["publication_id"],
            )
            if not hydrated.ok or not isinstance(hydrated.data, Mapping):
                raise TimelineEditError(f"could not hydrate checkout publication: {hydrated.error}")
            bundle = copy.deepcopy(dict(hydrated.data["bundle"]))
            bundle["base_parent"]["publication_id"] = head["publication_id"]
        pin = bundle.get("base_parent", {})
        if pin.get("publication_id") and "_runtime_document" not in bundle.get("source_mapping", {}):
            hydrated = c.timelines.open_bundle(project, timeline, revision_id=pin["revision_id"],
                                               publication_id=pin["publication_id"])
            if not hydrated.ok or not isinstance(hydrated.data, Mapping):
                raise TimelineEditError(f"could not hydrate original checkout publication: {hydrated.error}")
            bundle = copy.deepcopy(dict(hydrated.data["bundle"]))
            bundle["base_parent"]["publication_id"] = pin["publication_id"]
        return bundle

    if client is not None:
        return read(client)
    with AstridClient.open_from_launcher(start_pack_host=False) as c:
        return read(c)


def _units(bundle: Mapping[str, Any], *, whole: bool = False) -> dict[tuple[str, ...], Any]:
    """The bundle as mergeable units: every internal clip, every placement row, every parent clip
    (each film setting in ``parent.config`` too, merged on publish; ``whole``: also each shot's payload, for working-copy saves)."""
    units: dict[tuple[str, ...], Any] = {}
    config = (bundle.get("parent") or {}).get("config") or {}
    film = _ns.film_intent(config)  # chapters, slots, cut notes: app.astrid, or loose keys of a legacy revision
    for cid, note in intent.cut_notes(bundle).items():  # a cut's notes merge cut by cut, on publish and on save
        units[("cut-note", cid)] = note
    for key, value in film.items():
        if key == "cut_notes":
            continue
        # a key outside the known list (e.g. app.astrid.groups) still belongs to the namespace, not to loose config
        units[("parent-config" if key in _ns.FILM_INTENT_KEYS else "film-intent", str(key))] = value
    for key, value in config.items():  # film settings merge on publish too
        if key == "app":  # other owners' app keys (render clock, shot composition, ...) merge key by key
            for akey, avalue in (value.items() if isinstance(value, Mapping) else ()):
                if akey != _ns.NS:
                    units[("parent-app", str(akey))] = avalue
        elif key not in _ns.FILM_INTENT_KEYS:
            units[("parent-config", str(key))] = value
    if whole:
        for sid, shot in (bundle.get("shots") or {}).items():
            units[("payload", str(sid))] = shot.get("payload")
    for row in bundle.get("placements") or []:
        units[("placement", str(row.get("shot_id")))] = row
    for sid, shot in (bundle.get("shots") or {}).items():
        internal = shot.get("internal_timeline") or {}
        for clip in internal.get("clips") or []:
            units[("clip", str(sid), str(clip.get("id")))] = clip
        units[("tracks", str(sid))] = internal.get("tracks")
        units[("registry", str(sid))] = internal.get("registry")
    parent = bundle.get("parent") or {}
    for clip in parent.get("clips") or (parent.get("config") or {}).get("clips") or []:
        units[("parent-clip", str(clip.get("id")))] = clip
    return units


def import_media(path: Path, project: Any) -> dict[str, Any]:
    """Import a local file into the project's media store; return a registry entry."""
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "astrid", "media", "import", str(path), "--project", str(project), "--json"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        data = json.loads(out.stdout)["data"]
    except Exception as exc:
        raise TimelineEditError(f"media import of {path} failed: {(out.stderr or out.stdout)[-300:]}") from exc
    entry = {"media_id": data["object_id"], "content_sha256": data.get("digest") or data["object_id"],
             "type": data.get("media_type") or "application/octet-stream"}
    if data.get("resolution"):
        entry["resolution"] = data["resolution"]
    return entry
