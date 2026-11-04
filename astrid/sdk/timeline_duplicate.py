"""Duplicate a timeline into a new one, with new shot and occurrence ids.

Shot ids are global per project, so a duplicate can never reuse the source's
shot ids. The copy is built the way ``production/scripts/build_timeline.py``
builds a candidate: an authoring shot per source placement, each with its own
internal timeline, registry and placement row, published as one revision of
the new (empty) timeline.

Pure parts (``id_map``, ``copy_content``) take and return plain dicts; only
``duplicate_timeline`` talks to the runtime.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from astrid.core.timeline.astrid_ns import migrate_bundle
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from astrid.sdk.timeline_editing import add_authoring_shot
from astrid.sdk.timeline_restructure import STRUCTURE_KEY, flatten

__all__ = ["duplicate_timeline", "id_map", "copy_content"]

# Payload fields that point at the source's own runtime records. They are not
# copied: the new shot gets its own records on the next publish.
_SOURCE_BOUND_PAYLOAD_KEYS = ("text_bindings", "internal_timeline_revision_id")


class _CopyOutcomeUnknown(TimelineEditError):
    """The durable copy flight must be reconciled before destination cleanup.

    Presented exactly like an unknown commit: same headline and detail as
    CommitOutcomeUnknown, outcome "unknown", and a fix naming the duplicate call.
    """

    def __init__(self, timeline: str, cause: BaseException):
        from astrid.sdk.timeline._commit_wording import present_failure

        failure = {"code": getattr(cause, "code", type(cause).__name__),
                   "message": str(cause), "details": copy.deepcopy(getattr(cause, "details", {}))}
        self.presentation = present_failure(failure, unknown=True)
        self.presentation["fix"] = ("Call timeline.duplicate(...) again with the same name to confirm "
                                    "the saved request; the edit is not re-run blindly.")
        self.headline = self.presentation["headline"]
        self.detail = self.presentation["detail"]
        self.fix = self.presentation["fix"]
        self.outcome = "unknown"
        self.cause = self.presentation["cause"]
        self.timeline = timeline
        super().__init__(f"{self.headline}. The commit outcome is unknown. {self.fix}")


def _duplicate_key(project: str, source: str, slug: str) -> str:
    """Return the stable operation key used by the friendly duplicate facade."""
    identity = json.dumps([project, source, slug], ensure_ascii=False, separators=(",", ":"))
    return "timeline-duplicate-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _commit_bundle(project: str, timeline: str, desired: Mapping[str, Any], *, label: str,
                   client: Any, idempotency_key: str) -> dict[str, Any]:
    """Submit the authenticated copy through the normal durable command session."""
    from astrid.sdk.timeline import open as open_timeline

    session = open_timeline(project, timeline, client=client)
    with session.session.outbox.locked():
        state = session.session._state()
    replay = session.session.retry(
        idempotency_key,
        matches=lambda body: body.get("commands") == [desired["_duplicate_command"]],
    )
    if replay is not None:
        result = replay
    else:
        try:
            if state["pending"] or state["flight"] is not None:
                result = session.commit(label, merge="atomic", idempotency_key=idempotency_key)
            else:
                result = session.session.submit(
                    [copy.deepcopy(desired["_duplicate_command"])], label=label, merge="atomic", reads=[],
                    steps=[{"id": "copy", "label": label, "intent": "Copy authenticated source",
                            "tags": ["duplicate"], "command_ordinals": [0], "patch": [], "reads": []}],
                    expected_state=state, idempotency_key=idempotency_key)
                session._accepted(result)
        except Exception as exc:
            with session.session.outbox.locked():
                pending = session.session._state()
            if pending["flight"] is not None and not pending.get("conflict"):
                raise _CopyOutcomeUnknown(timeline, exc) from exc
            raise
    return {"old_head": result["data"].get("previous_revision_id"),
            "new_head": result["data"].get("accepted_revision_id"),
            "receipt": result.get("receipt")}


def _source_reference(client: Any, project: str, source: str, content: Mapping[str, Any]) -> dict[str, Any]:
    """Select an exact accepted publication; the private port verifies authority."""
    source_revision = content["base_parent"]["revision_id"]
    cursor, seen = None, set()
    while True:
        history = client.timelines.history(project, source, cursor=cursor, limit=200)
        if not history.ok:
            raise TimelineEditError(f"could not authenticate duplicate source publication: {history.error}")
        entry = next((item for item in history.data["items"] if item["revision_id"] == source_revision), None)
        if entry is not None:
            source_publication = entry["publication_id"]
            break
        cursor = history.data.get("next_cursor")
        if cursor is None or cursor in seen:
            raise TimelineEditError("the exact source publication is unavailable")
        seen.add(cursor)
    return {"project_id": content["project_id"], "timeline_id": content["timeline_id"],
            "revision_id": source_revision, "publication_id": source_publication,
            "content_digest": content["base_parent"]["content_digest"]}


def _common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        while not value.startswith(prefix):
            prefix = prefix[:-1]
    # Cut back to a '-' boundary so suffixes stay whole words.
    return prefix[: prefix.rfind("-") + 1] if "-" in prefix else ""


def id_map(old_ids: Iterable[str], new_slug: str) -> dict[str, str]:
    """Map each old id to ``<new_slug>-<suffix>``, where suffix is the id after the ids' shared prefix.

    ``["p-ch01-a", "p-ch02-b"]`` with slug ``dup`` gives ``{"p-ch01-a": "dup-ch01-a", "p-ch02-b": "dup-ch02-b"}``.
    A single id keeps its whole text as the suffix. Old ids must be unique; the result is injective.
    """
    ids = [str(i) for i in old_ids]
    if len(set(ids)) != len(ids):
        raise TimelineEditError("cannot map duplicate source ids")
    prefix = _common_prefix(ids) if len(ids) > 1 else ""
    mapping: dict[str, str] = {}
    for old in ids:
        suffix = old[len(prefix):] or old
        mapping[old] = f"{new_slug}-{suffix}"
    if len(set(mapping.values())) != len(mapping):
        raise TimelineEditError("id mapping is not one-to-one; choose another slug")
    return mapping


def _remap_values(value: Any, mapping: Mapping[str, str]) -> Any:
    """Replace every string that is exactly a mapped id (values only, never substrings)."""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_remap_values(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _remap_values(v, mapping) for k, v in value.items()}
    return value


def _copy_source(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Normalize the shared copy path's source bundle, including an archive JSON file."""
    if isinstance(source, Mapping):
        return copy.deepcopy(dict(source))
    path = Path(source)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TimelineEditError(f"cannot read timeline bundle {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise TimelineEditError(f"timeline bundle {path} must contain a JSON object")
    return copy.deepcopy(dict(value))


def copy_content(source: Mapping[str, Any] | str | Path, target: Mapping[str, Any], *, new_slug: str) -> tuple[dict[str, Any], list[str]]:
    """Return a copy of ``target`` (keeping its provenance and identity) holding ``source``'s content.

    Shots, placements and occurrences get new ids (``id_map``). Returns ``(bundle, notes)``
    where notes say what was not carried over.
    """
    source = _copy_source(source)
    notes: list[str] = []
    placements = [dict(row) for row in source.get("placements") or []]
    shot_ids = [str(row["shot_id"]) for row in placements]
    occurrence_ids = [str(row["occurrence_id"]) for row in placements]
    shot_map = id_map(list(dict.fromkeys(shot_ids)), new_slug)  # a shot the source places twice is ONE shot
    occ_map = id_map(occurrence_ids, f"occ-{new_slug}")
    ids = {**shot_map, **occ_map}

    out = copy.deepcopy(dict(target))
    src_parent = source.get("parent") or {}
    if not isinstance(src_parent, Mapping):
        raise TimelineEditError("source parent must be an object")
    # Parent revision payloads may contain authored extension fields beyond
    # config/registry/clips. Preserve those fields too, remapping any exact
    # shot/occurrence references. Occurrences themselves are compiler-derived.
    copied_parent = copy.deepcopy(dict(src_parent))
    copied_parent.pop("occurrences", None)
    out["parent"] = _remap_values(copied_parent, ids)
    out["shots"] = {}
    out["placements"] = []
    out["source_mapping"] = {"placements": {}, "shots": {}}

    text_bindings = 0
    for row in placements:
        old_shot = str(row["shot_id"])
        new_shot = shot_map[old_shot]
        src_shot = (source.get("shots") or {}).get(old_shot)
        if not isinstance(src_shot, Mapping):
            raise TimelineEditError(f"source shot {old_shot!r} has no content")
        if new_shot in out["shots"]:  # placed again: same shot, another occurrence
            again = _remap_values(copy.deepcopy(row), ids)
            again["shot_id"], again["occurrence_id"] = new_shot, occ_map[str(row["occurrence_id"])]
            out["placements"].append(again)
            out["source_mapping"]["placements"][again["occurrence_id"]] = copy.deepcopy(again)
            continue
        # Seed the shot the way build_timeline does (add_authoring_shot). Its base_* stays the empty seed
        # and the current content is the copy: that difference is what makes the compiler materialize it.
        shot = add_authoring_shot(out, shot_id=new_shot, occurrence_id=occ_map[str(row["occurrence_id"])],
                                  start_ms=(row.get("placement") or {}).get("start_ms", 0))
        payload = copy.deepcopy(dict(src_shot.get("payload") or src_shot.get("base_payload") or {}))
        # Item ids are globally unique inside a compiled authoring bundle.
        # Reuse the compiler's canonical placement remapper so copied shots
        # get fresh item identities and known item references stay coherent.
        try:
            from astrid.core.timeline.authoring_bundle import _copy_shot_for_placement

            payload, _item_ids = _copy_shot_for_placement(
                payload,
                project_id=str(out.get("project_id") or ""),
                timeline_id=str(out.get("timeline_id") or new_slug),
                occurrence_id=occ_map[str(row["occurrence_id"])],
                authoring_shot_id=new_shot,
                source_shot_id=old_shot,
            )
        except Exception as exc:
            raise TimelineEditError(f"cannot remap copied shot item identities for {old_shot!r}: {exc}") from exc
        text_bindings += len(payload.get("text_bindings") or [])
        for key in _SOURCE_BOUND_PAYLOAD_KEYS:
            payload.pop(key, None)
        payload["text_bindings"] = []
        shot["payload"] = payload
        from astrid.sdk.timeline.native_timeline_duplicate import adopt_parent_assets

        internal = _remap_values(copy.deepcopy(dict(src_shot.get("internal_timeline") or {})), shot_map)
        shot["internal_timeline"] = adopt_parent_assets(internal, source.get("parent") or {})
        new_row = _remap_values(copy.deepcopy(row), ids)
        new_row["shot_id"] = new_shot
        new_row["occurrence_id"] = occ_map[str(row["occurrence_id"])]
        out["placements"][-1] = new_row  # replaces the placement add_authoring_shot appended
        out["source_mapping"]["placements"][new_row["occurrence_id"]] = copy.deepcopy(new_row)

    if text_bindings:
        from astrid.sdk.timeline_checkout import Checkout

        bound = Checkout(copy.deepcopy(out)).narration()
        if len(bound) == len(out.get("shots") or {}):
            notes.append(f"narration: bound again from each line's script text ({len(bound)} shots)")
        else:
            notes.append(f"{text_bindings} narration text binding(s) not copied (these lines declare no script text); "
                         "the words themselves are copied in each VO clip's app.words")
    migrate_bundle(out)  # the source may be an old revision: the copy carries intent under app.astrid
    assert_copy_equals_source(source, out, shot_map)
    return out, notes


def assert_copy_equals_source(source: Mapping[str, Any], copied: Mapping[str, Any], shot_map: Mapping[str, str]) -> None:
    """Prove ``copied`` is a faithful copy of ``source`` (its input) under ``shot_map``, or raise.

    This proves the copy equals the bundle it was given, not that the bundle is the right form of the film;
    ``_duplicate`` separately compares against what the source itself stores.

    Same number of placements; distinct source shots map to distinct new shots (a shot placed twice stays one shot
    placed twice); each placement keeps its start and length; each shot's clips are the source's, ids remapped.
    """
    src_rows, new_rows = list(source.get("placements") or []), list(copied.get("placements") or [])
    if len(src_rows) != len(new_rows):
        raise TimelineEditError(f"the copy has {len(new_rows)} placements, its source has {len(src_rows)}")
    if len(set(shot_map.values())) != len(shot_map):
        raise TimelineEditError("the copy maps distinct source shots onto one shot id")
    for a, b in zip(src_rows, new_rows):
        if shot_map.get(str(a["shot_id"])) != str(b["shot_id"]):
            raise TimelineEditError(f"placement of shot {a['shot_id']!r} was copied onto {b['shot_id']!r}, not {shot_map.get(str(a['shot_id']))!r}")
        if (a.get("placement") or {}) != (b.get("placement") or {}) or a.get("duration_ms") != b.get("duration_ms"):
            raise TimelineEditError(f"placement of shot {a['shot_id']!r} changed its start or length in the copy")
    if len({str(r["shot_id"]) for r in new_rows}) != len({str(r["shot_id"]) for r in src_rows}):
        raise TimelineEditError("the copy has a different number of distinct shots than its source")
    migrated: dict[str, Any] = copy.deepcopy(dict(source))
    migrate_bundle(migrated)  # the copy carries intent under app.astrid
    for old, new in shot_map.items():
        want = _remap_values(copy.deepcopy(dict((migrated["shots"][old].get("internal_timeline")) or {})), shot_map)
        got = (copied.get("shots") or {}).get(new, {}).get("internal_timeline") or {}
        if (want.get("clips") or []) != (got.get("clips") or []):
            raise TimelineEditError(f"shot {old!r} was not copied faithfully into {new!r} (its clips differ)")


def duplicate_timeline(project: str, source: str | Mapping[str, Any] | Path, *, slug: str | None = None, name: str | None = None,
                       revision_id: str | None = None, client: Any = None,
                       idempotency_key: str | None = None) -> dict[str, Any]:
    """Create a timeline with a copy of ``source``'s head (new shot ids) and publish it.

    Returns ``{"timeline_id", "slug", "source", "shots", "old_head", "new_head", "notes"}``.
    If the copy fails after the timeline exists, the error names that timeline so it can be archived.
    """
    if client is not None:
        return _duplicate(client, project, source, slug=slug, name=name, revision_id=revision_id,
                          idempotency_key=idempotency_key)
    from astrid.sdk import AstridClient

    with AstridClient.open_from_launcher(start_pack_host=False) as opened:
        return _duplicate(opened, project, source, slug=slug, name=name, revision_id=revision_id,
                          idempotency_key=idempotency_key)


def _duplicate(client: Any, project: str, source: str | Mapping[str, Any] | Path, *, slug: str | None, name: str | None,
               revision_id: str | None, idempotency_key: str | None = None) -> dict[str, Any]:
    is_file_source = isinstance(source, (Mapping, Path))
    if isinstance(source, str):
        try:
            is_file_source = Path(source).is_file()
        except OSError:
            is_file_source = False
    if is_file_source:
        if revision_id is not None:
            raise TimelineEditError("a file-backed timeline source cannot select a Runtime revision")
        # A detached source has no authenticated publication for timeline.copy.
        # Use the same atomic import bootstrap as archive transfers instead.
        from astrid.sdk.timeline_export import _list_transcript_documents, inventory_elements
        from astrid.sdk.timeline_import import _project_id, _result_data
        from astrid.sdk.timeline_import_prepare import prepare_import

        source_bundle = _copy_source(source)
        target_project = _project_id(client, project)
        source_project = str(source_bundle.get("project_id") or target_project)
        transcripts = _list_transcript_documents(client, source_project)
        prepared = prepare_import(
            source_bundle,
            {"format": "astrid.timeline/1", "name": name or "Copied timeline",
             "elements": inventory_elements(source_bundle)},
            transcripts, target_project, name=name, timeline_id=slug,
        )
        imported = _result_data(client.timelines.import_timeline(
            target_project, {**prepared, "source": {"via": "python"}},
            idempotency_key=f"dup-{uuid.uuid4().hex}"), "timeline copy")
        return {**dict(imported), "slug": slug or prepared["timeline_id"], "source": str(source),
                "shots": len(prepared["publication"].get("shot_revisions", [])),
                "old_head": None, "notes": []}
    src = Checkout.open(project, str(source), revision_id=revision_id, client=client)
    report = src._check_core()  # refuse an invalid head before creating anything
    if not report.valid:
        raise TimelineEditError(f"not duplicating {source}: its head is not valid ("
                                + "; ".join((report.blocking or report.problems or ["invalid"])[:3]) + "); nothing was created")
    operation_key = idempotency_key or ("dup-" + uuid.uuid4().hex)
    create_key = f"{operation_key}-destination"
    copy_key = f"{operation_key}-copy"
    created = client.timelines.create_empty(
        project=project,
        timeline_id=slug or None,
        **({"name": name} if name is not None else {}),
        config=copy.deepcopy(dict((src.bundle.get("parent") or {}).get("config") or {})),
        idempotency_key=create_key,
    )
    if not created.ok:
        error = getattr(created.error, "message", created.error)
        raise TimelineEditError(f"could not create the timeline: {error}")
    row = created.data if isinstance(created.data, Mapping) else {}
    new_id = str(row.get("timeline_id") or slug or "")
    if not new_id:
        raise TimelineEditError("the runtime created a timeline but returned no id")
    try:
        dst = Checkout.open(project, new_id, client=client)
        # A chapter film is read flat (ONE shot; its stored form is kept under structure_base). Copy the stored
        # form: copying the flat shot would store the duplicate as a single shot whose chapters all share one id.
        from astrid.sdk.timeline.native_timeline_duplicate import physical_content

        content = physical_content(src.bundle.get(STRUCTURE_KEY) or src.bundle)
        bundle, notes = copy_content(content, dst.bundle, new_slug=slug or new_id)
        dst.bundle = flatten(bundle)  # a chapter film is edited and published flat; publish splits it back into its stored form
        dst.notes.extend(notes)
        # What publish will store for the copy must have as many distinct shots as what the SOURCE stores. The source
        # side is read independently of `content`, so copying the wrong form of the source is caught here.
        copy_shots = {str(r["shot_id"]) for r in dst.stored_document()["placements"]}
        stored_source = src.stored_document()
        origins = stored_source["source_mapping"]["shots"]
        source_shots = {str(origins.get(r["shot_id"], {}).get("shot_id", r["shot_id"]))
                        for r in stored_source["placements"]}
        if len(copy_shots) != len(source_shots):
            raise TimelineEditError(f"the copy would be stored with {len(copy_shots)} distinct shots, its source stores {len(source_shots)}")
        report = dst._check_core()
        if not report.valid:
            raise TimelineEditError("the copy is invalid: " + "; ".join(report.problems or [str(report)]))
        from astrid.sdk.timeline.native_timeline_duplicate import copy_command

        bundle["_duplicate_command"] = copy_command(
            _source_reference(client, project, source, content),
            id_map(list(dict.fromkeys(str(row["shot_id"]) for row in content["placements"])), slug or new_id),
            id_map([str(row["occurrence_id"]) for row in content["placements"]], f"occ-{slug or new_id}"))
        receipt = _commit_bundle(project, new_id, bundle, label=f"duplicate of {source}",
                                 client=client, idempotency_key=copy_key)
    except _CopyOutcomeUnknown:
        raise
    except Exception as exc:
        if idempotency_key is not None:
            raise TimelineEditError(f"{exc} (destination setup is retained for retry: {new_id!r})") from exc
        archived = client.timelines.archive(project, new_id)  # all or nothing: the empty timeline goes too
        if getattr(archived, "ok", False):
            raise TimelineEditError(f"{exc} (nothing kept: the new timeline {new_id!r} was archived again)") from exc
        raise TimelineEditError(f"{exc} (the empty timeline {new_id!r} was created and could not be archived: "
                                f"`timelines archive {new_id} --project {project}`)") from exc
    setup_receipt = created.receipt
    if hasattr(setup_receipt, "as_dict"):
        setup_receipt = setup_receipt.as_dict()
    return {
        "timeline_id": new_id,
        "slug": slug,
        "source": source,
        "shots": len(bundle["shots"]),
        "old_head": receipt.get("old_head"),
        "new_head": receipt.get("new_head"),
        "destination_setup": {"timeline_id": new_id, "idempotency_key": create_key, "receipt": setup_receipt},
        "copy_receipt": receipt.get("receipt") or receipt,
        "notes": list(dst.notes),
    }
