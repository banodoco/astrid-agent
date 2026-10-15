"""Working-copy paths and conflict-aware JSON merge helpers."""
from __future__ import annotations

import copy
import contextlib
import json
import os
import re
from pathlib import Path
import time
import uuid
from typing import Any, Mapping

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
    # A stable step identity is part of the semantic journal, while its numeric
    # prefix orders the local stack. It is independent of wall-clock/UUID noise.
    import hashlib
    sequence = max((int(p.name.split("-", 1)[0]) for p in folder.glob("*.json")
                    if p.name.split("-", 1)[0].isdigit()), default=0) + 1
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]
    name = f"{sequence:020d}-{digest}.json"
    temporary = folder / f".{name}.tmp"
    temporary.write_text(json.dumps({"label": str(label), "state": state}), encoding="utf-8")
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


@contextlib.contextmanager
def _file_lock(target: Path):
    """Serialize compare-and-write saves; the kernel releases the lock if its writer exits."""
    try:
        import fcntl
    except ImportError:
        yield
        return
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
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)



def _journaled(function, kind):
    """Legacy private export; delegate to the single command runner."""
    from astrid.sdk.timeline.commands import Operation, REGISTRY, _adapter
    if getattr(function, "__edit_operation__", None):
        return function
    kind = "timeline" if kind == "checkout" else kind
    operation = f"{kind}.{function.__name__.replace('_', '-').rstrip('-')}"
    entry = REGISTRY.setdefault(operation, Operation(operation, kind, function.__name__, function))
    return _adapter(entry)


def _merge(base: Any, head: Any, draft: Any, path: str, conflicts: list[str], force: bool):
    if head == base:
        return copy.deepcopy(draft)
    if draft == base or draft == head:
        return copy.deepcopy(draft if force and draft != base else head)
    if all(isinstance(value, dict) for value in (base, head, draft)):
        out = {}
        missing = object()
        for key in set(base) | set(head) | set(draft):
            b, h, d = base.get(key, missing), head.get(key, missing), draft.get(key, missing)
            item_path = f"{path}.{key}" if path else str(key)
            if h is missing and d is missing:
                continue
            if b is missing or h is missing or d is missing:
                if h == b:
                    value = d
                elif d == b or h == d:
                    value = h
                else:
                    conflicts.append(item_path)
                    value = d if force else h
            else:
                value = _merge(b, h, d, item_path, conflicts, force)
            if value is not missing:
                out[key] = copy.deepcopy(value)
        return out
    conflicts.append(path or "bundle")
    return copy.deepcopy(draft if force else head)


def three_way(base: Mapping[str, Any], head: Mapping[str, Any], candidate: Mapping[str, Any], *,
              force: bool = False, whole: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Merge the candidate's changes (base → candidate) onto ``head``. Returns (merged, conflicts).

    With conflicts and no ``force`` the merge is not applied. With ``force`` your version wins
    on the conflicting units; every other unit of the head is kept."""
    # compare as if all three sides were migrated (a published head may still hold intent in the old
    # places); the merge itself is applied to the head as it is, so units nobody touched stay verbatim
    b, h, c = (_units(_ns.normalized(x), whole=whole) for x in (base, head, candidate))
    same = lambda x, y: json.dumps(x, sort_keys=True, default=str) == json.dumps(y, sort_keys=True, default=str)
    # Reservations are joined by maximum, never treated as competing authored edits.
    from astrid.sdk.timeline.identity import KEY, retain_cut_reservations
    reservation = ("parent-config", KEY)
    for units in (b, h, c):
        units.pop(reservation, None)
    ours = {k for k in set(b) | set(c) if not same(b.get(k), c.get(k))}
    theirs = {k for k in set(b) | set(h) if not same(b.get(k), h.get(k))}
    conflicts = [k for k in sorted(ours & theirs) if not same(c.get(k), h.get(k))]
    lines = [f"{'/'.join(k[1:])} ({k[0]}) changed in the head since your checkout and in your edit" for k in conflicts]
    merged = copy.deepcopy(dict(head))
    if lines and not force:
        return merged, lines
    for key in sorted(ours):
        value = copy.deepcopy(c.get(key))
        kind = key[0]
        if kind == "placement":
            rows = merged.setdefault("placements", [])
            rows[:] = [r for r in rows if _placement_identity(r) != key[1]] + ([value] if value is not None else [])
        elif kind in ("tracks", "registry"):
            shot = merged["shots"].setdefault(key[1], copy.deepcopy(candidate["shots"][key[1]]))
            shot.setdefault("internal_timeline", {})[kind] = value
        elif kind == "clip":
            sid, cid = key[1], key[2]
            if sid not in merged["shots"]:
                merged["shots"][sid] = copy.deepcopy(candidate["shots"][sid])
                continue
            clips = merged["shots"][sid].setdefault("internal_timeline", {}).setdefault("clips", [])
            index = next((i for i, x in enumerate(clips) if str(x.get("id")) == cid), None)
            if value is None and index is not None:
                clips.pop(index)
            elif value is not None and index is None:
                clips.append(value)
            elif value is not None:
                clips[index] = value
        elif kind == "parent-clip":
            clips = merged.setdefault("parent", {}).setdefault("clips", [])
            clips[:] = [x for x in clips if str(x.get("id")) != key[1]] + ([value] if value is not None else [])
        elif kind == "parent-config":
            config = merged.setdefault("parent", {}).setdefault("config", {})
            if key[1] in _ns.FILM_INTENT_KEYS:  # Astrid's film intent is written under app.astrid only
                ns = _ns.film_ns(config)
                if value is None:
                    ns.pop(key[1], None)
                    _ns.tidy_film(config)
                else:
                    ns[key[1]] = value
            elif value is None:
                config.pop(key[1], None)
            else:
                config[key[1]] = value
        elif kind == "film-intent":
            config = merged.setdefault("parent", {}).setdefault("config", {})
            ns = _ns.film_ns(config)
            if value is None:
                ns.pop(key[1], None)
                _ns.tidy_film(config)
            else:
                ns[key[1]] = value
        elif kind == "parent-app":
            config = merged.setdefault("parent", {}).setdefault("config", {})
            app = config.get("app")
            if not isinstance(app, dict):
                app = config["app"] = {}
            if value is None:
                app.pop(key[1], None)
                _ns.tidy_film(config)
            else:
                app[key[1]] = value
        elif kind == "payload" and key[1] in merged.get("shots", {}):
            merged["shots"][key[1]]["payload"] = value
        elif kind == "cut-note":
            ns = _ns.film_ns(merged.setdefault("parent", {}).setdefault("config", {}))
            notes = ns.setdefault("cut_notes", {})
            if value is None:
                notes.pop(key[1], None)
            else:
                notes[key[1]] = value
            if not notes:
                ns.pop("cut_notes", None)
                _ns.tidy_film(merged["parent"]["config"])
    retain_cut_reservations(merged, base, head, candidate)
    return merged, lines


def publish_bundle(candidate: Mapping[str, Any], idempotency_key: str, *, client: Any = None, force: bool = False) -> dict[str, Any]:
    from astrid.sdk.timeline.checkout import Checkout
    """One parent compare-and-swap publication with an overwrite guard (see ``Checkout.publish``)."""
    from astrid.sdk import AstridClient
    from astrid.sdk.authoring_bundle import publish_authoring_candidate
    from astrid.sdk.autobootstrap import ensure_runtime
    from astrid.sdk.workspace_client import WorkspaceClient, resolve_runtime_connection

    def head(c: Any) -> str:
        shown = c.timelines.open_composition(candidate["project_id"], candidate["timeline_id"])
        if not shown.ok:
            raise TimelineEditError(f"could not read the current head: {shown.error}")
        return str(shown.data["summary"]["head_revision_id"])

    if client is not None:
        current = head(client)
    else:
        with AstridClient.open_from_launcher(start_pack_host=False) as c:
            current = head(c)
    base = str((candidate.get("base_parent") or {}).get("revision_id"))
    from astrid.core.timeline.compatibility import TimelineCompatibilityError, require_writable_candidate

    # A current-head check is required even when this checkout is not stale.
    # Pin the bundle read to the ID just inspected; the final Runtime CAS
    # protects a concurrent change after this compatibility check.
    head_bundle = fetch_bundle(str(candidate["project_id"]), str(candidate["timeline_id"]),
                               revision_id=current, client=client)
    if str((head_bundle.get("base_parent") or {}).get("revision_id")) != current:
        raise TimelineEditError("not published: current head changed during compatibility inspection; retry")
    try:
        require_writable_candidate(head_bundle)
        require_writable_candidate(candidate)
    except TimelineCompatibilityError as exc:
        raise TimelineEditError(str(exc)) from exc
    merged_note = None
    if current != base:
        from astrid.sdk.timeline_cuts import base_bundle

        merged, conflicts = three_way(base_bundle(candidate), head_bundle, candidate, force=force)
        if conflicts and not force:
            names = {c.id: c.address for c in Checkout(copy.deepcopy(dict(candidate))).clips()}

            def plain(line: str) -> str:
                m = re.match(r"^\S+/(\S+) \(clip\) (.*)$", line)
                return f"{names.get(m.group(1), m.group(1))}  {m.group(2)}" if m else line

            short = lambda rev: str(rev).removeprefix("authoring-parent-revision-")[:8]  # noqa: E731
            raise TimelineEditError(f"not published: someone published {short(current)} after your checkout ({short(base)}), "
                                    "and you both changed:\n  " + "\n  ".join(plain(c) for c in conflicts)
                                    + "\nnext: timelines discard, checkout and re-apply your edit (or publish --force to overwrite theirs)")
        candidate = merged
        merged_note = f"merged onto head {current} (your checkout was {base})" + (" with force" if conflicts else "")
        base = current
    receipt = ensure_runtime(start_pack_host=False)
    endpoint, token = resolve_runtime_connection(receipt["endpoint"], Path(receipt["credential_file"]))
    transport = WorkspaceClient(endpoint, token)

    class _Writer:
        def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
            response = transport.publish_parent_composition(project_id, timeline_id, publication, idempotency_key=idempotency_key)
            return response.get("data", response)

    result = publish_authoring_candidate(dict(candidate), _Writer(), idempotency_key=idempotency_key)
    publication = result.get("publication") if isinstance(result, Mapping) else None
    return {"old_head": base, "new_head": (publication or {}).get("new_head"), "receipt": result,
            "idempotency_key": idempotency_key, "merged": merged_note}


# Runtime and draft seams added to the isolated modular SDK.


def drafts_root() -> Path:
    """Where working copies live: Astrid's data root (``BANODOCO_LOCAL_DATA_ROOT``) / drafts."""
    base = os.environ.get("BANODOCO_LOCAL_DATA_ROOT")
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
        with tempfile.NamedTemporaryFile(suffix=Path(kind.replace("/", ".")).suffix or ".bin") as tmp:
            tmp.write(data)
            tmp.flush()
            return float(ffprobe_duration_seconds(tmp.name))

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


def _acquire_lock(handle: Any, fcntl: Any, lock: Path) -> None:
    import time as _time

    wait = _lock_wait()
    deadline = _time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    from astrid.sdk import AstridClient

    def read(c: Any) -> tuple[str, str, str]:
        shown = c.timelines.open_composition(project, timeline)
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


def fetch_bundle(project: str, timeline: str, *, revision_id: str | None = None, client: Any = None) -> dict[str, Any]:
    """The complete pinned closure (parent → shots → internal timelines) of a head or revision."""
    from astrid.sdk import AstridClient

    def read(c: Any) -> dict[str, Any]:
        opened = c.timelines.open_bundle(project, timeline, revision_id=revision_id)
        if not opened.ok or not isinstance(opened.data, Mapping):
            raise TimelineEditError(f"could not open {timeline!r} in {project!r}: {opened.error}")
        return copy.deepcopy(dict(opened.data["bundle"]))

    if client is not None:
        return read(client)
    with AstridClient.open_from_launcher(start_pack_host=False) as c:
        return read(c)


def _placement_identity(row):
    # Explicit occurrence IDs distinguish repeated placements of a shared shot.
    # Legacy singleton rows remain mergeable by their only available identity.
    return str(row.get("occurrence_id") or row.get("id") or row.get("shot_id"))


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
        key = ("placement", _placement_identity(row))
        if key in units:
            raise TimelineEditError(f"ambiguous placement identity {key[1]!r}")
        units[key] = row
    for sid, shot in (bundle.get("shots") or {}).items():
        internal = shot.get("internal_timeline") or {}
        for clip in internal.get("clips") or []:
            units[("clip", str(sid), str(clip.get("id")))] = clip
        units[("tracks", str(sid))] = internal.get("tracks")
        units[("registry", str(sid))] = internal.get("registry")
    for clip in (bundle.get("parent") or {}).get("clips") or []:
        units[("parent-clip", str(clip.get("id")))] = clip
    return units


def import_media(path: Path, project: Any) -> dict[str, Any]:
    """Import a local file into the project's media store; return a registry entry."""
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "astrid", "media", "import", str(path), "--project", str(project), "--json"],
                         capture_output=True, text=True)
    try:
        data = json.loads(out.stdout)["data"]
    except Exception as exc:
        raise TimelineEditError(f"media import of {path} failed: {(out.stderr or out.stdout)[-300:]}") from exc
    entry = {"media_id": data["object_id"], "content_sha256": data.get("digest") or data["object_id"],
             "type": data.get("media_type") or "application/octet-stream"}
    if data.get("resolution"):
        entry["resolution"] = data["resolution"]
    return entry
