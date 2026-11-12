"""Pure preparation of a timeline-transfer archive for an atomic Runtime commit."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_duplicate import _copy_source, copy_content, id_map
from astrid.sdk.timeline_restructure import STRUCTURE_KEY

FORMAT = "astrid.timeline/1"
TRANSCRIPT_SCHEMA = "media.transcript/v1"
TRANSCRIPT_REFERENCES_SCHEMA = "media.transcript.references/v1"
TEXT_BINDING_SCHEMA = "workspace.shot.text_binding.identity/v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _document_id(project: str, media_id: str, source_id: str) -> str:
    identity = [TRANSCRIPT_SCHEMA, project, media_id, source_id]
    return "transcript:" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _fail(message: str) -> None:
    raise TimelineEditError(message)


def _descriptor_key(value: Mapping[str, Any]) -> tuple[str, str, str]:
    return str(value.get("packId") or ""), str(value.get("kind") or ""), str(value.get("id") or "")


def _installed_elements(elements: Any) -> list[dict[str, Any]]:
    if elements is None:
        from astrid.core.element.catalog import list_element_descriptors

        rows = list_element_descriptors()
    elif isinstance(elements, Mapping):
        rows = list(elements.values())
    else:
        rows = list(elements)
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def check_import_manifest(manifest: Mapping[str, Any], *, elements: Any = None) -> dict[str, list[dict[str, Any]]]:
    """Check a manifest against this installation before any archive bytes are uploaded."""
    if not isinstance(manifest, Mapping):
        _fail("timeline archive manifest must be a JSON object")
    if manifest.get("format") != FORMAT:
        _fail("This timeline was made with a newer Astrid; update Astrid to open it.")
    declared = manifest.get("elements", [])
    if not isinstance(declared, list):
        _fail("timeline manifest elements must be an array")
    installed = _installed_elements(elements)
    installed_by_key = {_descriptor_key(row): row for row in installed}
    missing: list[dict[str, Any]] = []
    different: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(declared):
        if not isinstance(raw, Mapping):
            _fail(f"timeline manifest element {index} must be an object")
        item = dict(raw)
        if not isinstance(item.get("packId"), str):
            _fail(f"timeline manifest element {index} requires packId to be a string")
        for field in ("kind", "id"):
            if not isinstance(item.get(field), str) or not item[field]:
                _fail(f"timeline manifest element {index} requires a non-empty {field}")
        if "pinned" in item and not isinstance(item["pinned"], bool):
            _fail(f"timeline manifest element {index}.pinned must be a boolean")
        if item.get("revision") is not None and not isinstance(item.get("revision"), str):
            _fail(f"timeline manifest element {index}.revision must be a string")
        key = _descriptor_key(item)
        if not key[0] and item.get("pinned") is not False:
            _fail(f"timeline manifest element {index} requires pinned=false when packId is empty")
        if key in seen:
            _fail(f"timeline manifest repeats element {key[2]!r}")
        seen.add(key)
        current = installed_by_key.get(key)
        source = str((current or {}).get("source") or "").lower()
        if key[0].startswith("project_") or "project-pack" in source or source.startswith("project:"):
            _fail(f"This timeline uses custom effect {key[2]} from a project pack; custom effects can't travel yet.")
        if current is None:
            if item.get("pinned"):
                _fail(f"This timeline pins {key[2]} at a revision this Astrid installation cannot provide.")
            missing.append(item)
            continue
        wanted_revision = item.get("revision")
        current_revision = current.get("revision")
        if item.get("pinned"):
            if not isinstance(wanted_revision, str) or not wanted_revision:
                _fail(f"This timeline pins {key[2]} without a revision; update or repair the archive before importing it.")
            if not isinstance(current_revision, str) or not current_revision or wanted_revision != current_revision:
                _fail(f"This timeline pins {key[2]} at a different revision; update Astrid before importing it.")
        elif wanted_revision and current_revision and wanted_revision != current_revision:
            different.append(item)
    return {"missing_effects": missing, "different_effects": different}


def _source_element_inventory(bundle: Mapping[str, Any], *, elements: Any = None) -> list[dict[str, Any]]:
    """Use the export module's authoritative inventory walker (shared by export/import)."""
    from astrid.sdk.timeline_export import inventory_elements

    return inventory_elements(bundle, elements=elements)


def _crosscheck_inventory(bundle: Mapping[str, Any], manifest: Mapping[str, Any], *, elements: Any = None) -> list[dict[str, Any]]:
    actual = _source_element_inventory(bundle, elements=elements)
    declared = manifest.get("elements", [])
    if not isinstance(declared, list):
        _fail("timeline manifest elements must be an array")

    def normalized(rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            _fail("timeline element inventory must be an array")
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                _fail("timeline element inventory contains a non-object entry")
            key = _descriptor_key(row)
            if not key[1] or not key[2]:
                _fail("timeline element inventory entry is missing kind or id")
            pinned = bool(row.get("pinned"))
            result.append({"packId": key[0], "kind": key[1], "id": key[2],
                           "revision": str(row.get("revision") or "") if pinned else "", "pinned": pinned})
        return result

    actual_rows, declared_rows = normalized(actual), normalized(declared)
    unmatched = list(declared_rows)
    valid = len(actual_rows) == len(declared_rows)
    for actual_row in actual_rows:
        match = next((row for row in unmatched
                      if row["kind"] == actual_row["kind"] and row["id"] == actual_row["id"]
                      and row["pinned"] == actual_row["pinned"]
                      and (not actual_row["packId"] or row["packId"] == actual_row["packId"])
                      and (not actual_row["pinned"] or row["revision"] == actual_row["revision"])), None)
        if match is None:
            valid = False
            break
        unmatched.remove(match)
    if not valid or unmatched:
        _fail("timeline manifest element inventory does not match timeline.json; refusing a potentially hidden or stale pin")
    return actual


def _transcript_documents(transcripts: Any, source_project: str) -> dict[str, dict[str, Any]]:
    if isinstance(transcripts, Mapping):
        values = list(transcripts.values())
    elif isinstance(transcripts, list):
        values = transcripts
    else:
        _fail("timeline transcripts must be an object or array")
    if values and not source_project:
        _fail("timeline bundle is missing the source project id needed to verify transcript identities")
    by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, Mapping):
            _fail(f"transcript {index} must be an object")
        row = dict(raw)
        content = row.get("content", row)
        if not isinstance(content, Mapping) or content.get("schema") != TRANSCRIPT_SCHEMA:
            _fail(f"transcript {index} has unsupported content")
        if row.get("kind", "media.transcript") != "media.transcript":
            _fail(f"transcript {index} has an unsupported document kind")
        media_id, source_id = content.get("media_id"), content.get("source_id")
        if (not isinstance(media_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", media_id)
                or not isinstance(source_id, str) or not source_id):
            _fail(f"transcript {index} must include media_id and source_id")
        old_id = row.get("document_id")
        if not isinstance(old_id, str) or not old_id:
            old_id = _document_id(source_project, media_id, source_id)
        elif old_id != _document_id(source_project, media_id, source_id):
            _fail(f"transcript {index} document_id does not match its source project, media, and source identity")
        if old_id in by_id:
            _fail(f"timeline archive repeats transcript document {old_id}")
        by_id[old_id] = {"source_document_id": old_id, "kind": "media.transcript", "content": copy.deepcopy(dict(content))}
    return by_id


def _rewrite_transcript_references(value: Any, source_docs: Mapping[str, Mapping[str, Any]], target_project: str,
                                   target_docs: dict[str, dict[str, Any]], seen_refs: set[str]) -> None:
    if isinstance(value, dict):
        references = value.get("transcriptReferences")
        if isinstance(references, dict):
            if references.get("schema") != TRANSCRIPT_REFERENCES_SCHEMA:
                _fail("timeline contains unsupported transcriptReferences schema")
            associations = references.get("associations")
            if not isinstance(associations, list):
                _fail("timeline transcriptReferences.associations must be an array")
            for association in associations:
                if not isinstance(association, dict) or not isinstance(association.get("documentId"), str):
                    _fail("timeline contains a malformed transcript association")
                source_id = association["documentId"]
                document = source_docs.get(source_id)
                if document is None:
                    _fail(f"timeline transcript reference {source_id!r} has no carried transcript")
                content = document["content"]
                new_id = _document_id(target_project, str(content["media_id"]), str(content["source_id"]))
                association["documentId"] = new_id
                seen_refs.add(source_id)
                target_docs[new_id] = {"document_id": new_id, "kind": "media.transcript", "content": copy.deepcopy(content)}
        for child in value.values():
            _rewrite_transcript_references(child, source_docs, target_project, target_docs, seen_refs)
    elif isinstance(value, list):
        for child in value:
            _rewrite_transcript_references(child, source_docs, target_project, target_docs, seen_refs)


def _retarget_text_bindings(source: Mapping[str, Any], copied: dict[str, Any], target_project: str,
                            timeline_id: str) -> list[dict[str, Any]]:
    placements = source.get("placements") or []
    source_shots = source.get("shots") or {}
    mapping = id_map(dict.fromkeys(str(row["shot_id"]) for row in placements), timeline_id)
    registrations: list[dict[str, Any]] = []
    for source_shot_id, target_shot_id in mapping.items():
        shot = source_shots.get(source_shot_id)
        payload = shot.get("payload") or shot.get("base_payload") or {} if isinstance(shot, Mapping) else {}
        pins = payload.get("text_bindings", []) if isinstance(payload, Mapping) else []
        if not isinstance(pins, list):
            _fail(f"shot {source_shot_id!r} text_bindings must be an array")
        target_shot = (copied.get("shots") or {}).get(target_shot_id)
        if not isinstance(target_shot, dict):
            _fail(f"copied shot {target_shot_id!r} is missing")
        target_payload = target_shot.setdefault("payload", {})
        new_pins = []
        for pin in pins:
            if not isinstance(pin, Mapping):
                _fail(f"shot {source_shot_id!r} contains a malformed text binding")
            kind, media_id = pin.get("kind"), pin.get("media_id")
            content_hash, byte_size, mime_type = pin.get("content_hash"), pin.get("byte_size"), pin.get("mime_type")
            if not isinstance(kind, str) or not kind or not isinstance(media_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", media_id):
                _fail(f"shot {source_shot_id!r} text binding is missing kind or managed media_id")
            if not isinstance(content_hash, str) or not content_hash.startswith("sha256:"):
                _fail(f"shot {source_shot_id!r} text binding is missing content_hash")
            if content_hash != media_id:
                _fail(f"shot {source_shot_id!r} text binding content_hash does not match its media_id")
            if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0 or not isinstance(mime_type, str) or not mime_type:
                _fail(f"shot {source_shot_id!r} text binding has invalid byte_size or mime_type")
            slot = pin.get("slot")
            if slot is not None and (not isinstance(slot, str) or not slot):
                _fail(f"shot {source_shot_id!r} text binding slot must be null or a non-empty string")
            identity = {"schema": TEXT_BINDING_SCHEMA, "project_id": target_project,
                        "shot_id": target_shot_id, "kind": kind, "slot": slot}
            binding_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _canonical(identity).decode("utf-8")))
            # Carry only the immutable pin descriptor; source identity and timestamps never cross realms.
            new_pins.append({"binding_id": binding_id, "kind": kind, "slot": slot, "head": 1,
                             "media_id": media_id, "content_hash": content_hash,
                             "byte_size": byte_size, "mime_type": mime_type})
            registrations.append({"shot_id": target_shot_id, "kind": kind, "slot": slot, "media_id": media_id})
        if new_pins:
            target_payload["text_bindings"] = new_pins
    return registrations


def _empty_target(project: str, timeline_id: str, config: Mapping[str, Any]) -> dict[str, Any]:
    parent = {"config": {}, "clips": [], "registry": {}}
    digest = "sha256:" + hashlib.sha256(_canonical(parent)).hexdigest()
    return {
        "schema_version": 1,
        "project_id": project,
        "timeline_id": timeline_id,
        "base_parent": {"revision_id": "import-empty-" + digest.removeprefix("sha256:")[:20], "content_digest": digest},
        "base_parent_payload": copy.deepcopy(parent),
        "base_placements": {},
        "parent": {"config": copy.deepcopy(dict(config)), "clips": [], "registry": {}},
        "placements": [],
        "shots": {},
        "source_mapping": {"placements": {}, "shots": {}},
    }


def _normalize_legacy_internal_assets(source: dict[str, Any]) -> None:
    """Express legacy keyed internal assets as records without losing values.

    Only the detached archive copy is changed; the source archive and the
    distinct registry map remain untouched. An existing id is rejected
    because otherwise a flat round trip could not distinguish it from the key
    added here. Unknown entry types are rejected rather than discarded.
    """
    shots = source.get("shots") or {}
    shot_ids = set(shots)
    for shot in shots.values():
        internal = shot.get("internal_timeline") if isinstance(shot, dict) else None
        if not isinstance(internal, dict) or not isinstance(internal.get("assets"), dict):
            continue
        result = []
        for key, entry in internal["assets"].items():
            if not isinstance(key, str) or not key or key in shot_ids or not isinstance(entry, Mapping) or "id" in entry:
                _fail("legacy internal assets require non-empty, non-shot keys and object entries without id")
            result.append({**copy.deepcopy(dict(entry)), "id": key})
        internal["assets"] = result


def prepare_import(timeline: Mapping[str, Any] | str | Path, manifest: Mapping[str, Any], transcripts: Any,
                   project: str, name: str | None = None, *, timeline_id: str | None = None,
                   elements: Any = None) -> dict[str, Any]:
    """Build and compile a detached import candidate; performs no Runtime writes."""
    source_bundle = _copy_source(timeline)
    source = _copy_source(source_bundle.get(STRUCTURE_KEY) or source_bundle)
    if not isinstance(project, str) or not project:
        _fail("target project must be a resolved project id")
    if not isinstance(manifest, Mapping):
        _fail("timeline archive manifest must be a JSON object")
    # Refuse bad/missing effects before building a candidate. Verify that the
    # manifest's list is derived from the actual bundle so pins cannot be hidden.
    differences = check_import_manifest(manifest, elements=elements)
    _crosscheck_inventory(source_bundle, manifest, elements=elements)
    _normalize_legacy_internal_assets(source)
    target_id = timeline_id or uuid.uuid4().hex
    if not isinstance(target_id, str) or not target_id:
        _fail("target timeline id must be a non-empty string")
    config = ((source.get("parent") or {}).get("config") or {})
    if not isinstance(config, Mapping):
        _fail("timeline parent config must be an object")
    candidate = _empty_target(project, target_id, config)
    try:
        copied, _notes = copy_content(source, candidate, new_slug=target_id, _archive_baselines=True)
        # Text bindings are source-project records. Carry their existing object
        # identities, but assign deterministic target-project binding identities.
        registrations = _retarget_text_bindings(source, copied, project, target_id)
        source_docs = _transcript_documents(transcripts, str(source_bundle.get("project_id") or source.get("project_id") or ""))
        target_docs: dict[str, dict[str, Any]] = {}
        for document in source_docs.values():
            content = document["content"]
            new_id = _document_id(project, str(content["media_id"]), str(content["source_id"]))
            target_docs[new_id] = {"document_id": new_id, "kind": "media.transcript", "content": copy.deepcopy(content)}
        seen_refs: set[str] = set()
        _rewrite_transcript_references(copied, source_docs, project, target_docs, seen_refs)
        # `copy_content` returns the complete stored authoring candidate. Do not
        # route it through the editor's flat Checkout representation here:
        # flatten/split intentionally derives chapter boundaries and film clips,
        # which cannot preserve all archive-authored parent config fields.
        compiled = compile_authoring_candidate(copied).as_dict()
    except TimelineEditError:
        raise
    except Exception as exc:  # keep compiler details useful at the archive boundary
        raise TimelineEditError(f"could not prepare timeline import: {exc}") from exc
    publication = dict(compiled["publication"])
    publication["expected_head"] = None
    # Every copied child must materialize as a fresh target-owned revision.
    source_shot_ids = {str(row["shot_id"]) for row in source.get("placements") or []}
    target_shot_ids = {str(row.get("shot_id")) for row in publication.get("shot_revisions", [])}
    if len(target_shot_ids) != len(source_shot_ids) or not target_shot_ids.isdisjoint(source_shot_ids):
        _fail("compiled import did not materialize every copied shot with fresh identities")
    for revision in publication.get("internal_timeline_revisions", []):
        if revision.get("timeline_id") != target_id:
            _fail("compiled import contains an internal timeline owned by another timeline")
    return {
        "timeline_id": target_id,
        "name": name or str(manifest.get("name") or "Imported timeline"),
        "publication": publication,
        "transcripts": [target_docs[key] for key in sorted(target_docs)],
        "text_bindings": registrations,
        "differences": differences,
    }


__all__ = ["check_import_manifest", "prepare_import"]
