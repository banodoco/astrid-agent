"""Renderer-independent semantics for Runtime's fixed timeline commit port.

Runtime owns authority, persistence and completion reconciliation. This provider
only converts exact immutable closures and executes the recorded Agent plan.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping

from astrid.core.timeline.authoring_bundle import (
    AuthoringBundleError,
    _collect_media,
    _timeline_media_view,
    _without_meta,
    compile_authoring_candidate,
    open_authoring_bundle,
    preview_authoring_candidate,
)

PROVIDER_VERSION = "astrid-v1"
COMPILER_VERSION = "authoring-bundle-1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
PARENT_IMAGE_FIELDS = frozenset({"revision_id", "content_digest", "payload"})
SHOT_IMAGE_FIELDS = PARENT_IMAGE_FIELDS | {"shot_id", "internal_timeline_revision_id"}
INTERNAL_IMAGE_FIELDS = PARENT_IMAGE_FIELDS | {"timeline_id"}


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AuthoringBundleError("canonical document must be finite JSON") from exc


def _digest(value):
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _require(condition, message):
    if not condition:
        raise AuthoringBundleError(message)


def _id(value):
    return isinstance(value, str) and bool(value.strip())


def _document_pin(pin):
    _require(isinstance(pin, Mapping) and set(pin) == {
        "document_id", "version", "content_digest"}, "invalid document pin")
    _require(_id(pin["document_id"]) and type(pin["version"]) is int
             and pin["version"] > 0 and isinstance(pin["content_digest"], str)
             and _DIGEST.fullmatch(pin["content_digest"]), "invalid document pin")
    return dict(pin)


def _manifest(shots, internals, media, documents):
    return {
        "shots": sorted(({
            key: row[key] for key in ("shot_id", "revision_id",
                                     "internal_timeline_revision_id", "content_digest")
        } for row in shots), key=lambda row: (row["shot_id"], row["revision_id"])),
        "internal_timelines": sorted(({
            key: row[key] for key in ("timeline_id", "revision_id", "content_digest")
        } for row in internals), key=lambda row: (row["timeline_id"], row["revision_id"])),
        "media": sorted(media, key=lambda row: row["media_id"]),
        "documents": sorted(documents, key=lambda row: (
            row["document_id"], row["version"], row["content_digest"])),
    }


def _verify_document(document):
    _require(isinstance(document, Mapping) and set(document) == {
        "schema_version", "project_id", "timeline_id", "parent_revision", "shot_revisions",
        "internal_timeline_revisions", "dependency_manifest"}, "invalid canonical document")
    _require(type(document["schema_version"]) is int and document["schema_version"] == 1,
             "unsupported canonical document schema")
    _require(_id(document["project_id"]) and _id(document["timeline_id"]),
             "canonical project and timeline IDs are required")
    _json(document)
    parent = document["parent_revision"]
    shots, internals = document["shot_revisions"], document["internal_timeline_revisions"]
    _require(isinstance(shots, list) and isinstance(internals, list), "invalid closure images")
    for row in [parent, *shots, *internals]:
        _require(isinstance(row, Mapping) and _id(row.get("revision_id"))
                 and isinstance(row.get("payload"), Mapping), "invalid revision image")
        _require(row.get("content_digest") == _digest(row["payload"]),
                 "immutable payload digest mismatch")
        _require("project_id" not in row, "project scope belongs on the canonical document")
    _require(set(parent) == PARENT_IMAGE_FIELDS, "invalid parent image")
    payload = parent["payload"]
    config = payload.get("config", {})
    _require(isinstance(config, Mapping), "invalid parent config")
    if payload.get("clips") and config.get("clips"):
        _require(payload["clips"] == config["clips"], "parent clips and config.clips disagree")
    for row in shots:
        _require(set(row) == SHOT_IMAGE_FIELDS and _id(row["shot_id"]),
                 "invalid shot image")
    for row in internals:
        _require(set(row) == INTERNAL_IMAGE_FIELDS
                 and _id(row["timeline_id"]), "invalid internal image")
    shot_index = {(row["shot_id"], row["revision_id"]): row for row in shots}
    internal_index = {row["revision_id"]: row for row in internals}
    _require(len(shot_index) == len(shots) and len(internal_index) == len(internals)
             and len({row["revision_id"] for row in shots}) == len(shots),
             "duplicate closure identity")
    occurrences = parent["payload"].get("occurrences")
    _require(isinstance(occurrences, list), "parent occurrences are required")
    used_shots = set()
    for row in occurrences:
        _require(isinstance(row, Mapping), "invalid occurrence")
        key = (row.get("shot_id"), row.get("shot_revision_id"))
        _require(key in shot_index, "missing exact occurrence shot revision")
        used_shots.add(key)
    _require(used_shots == set(shot_index), "shot closure is incomplete or contains orphans")
    used_internals = set()
    for row in shots:
        revision = row["internal_timeline_revision_id"]
        _require(row["payload"].get("internal_timeline_revision_id", revision) == revision,
                 "shot internal revision link mismatch")
        _require(revision in internal_index, "missing exact internal revision")
        used_internals.add(revision)
    _require(used_internals == set(internal_index), "internal closure contains orphans")
    manifest = document["dependency_manifest"]
    _require(isinstance(manifest, Mapping) and set(manifest) == {
        "shots", "internal_timelines", "media", "documents"}, "invalid dependency manifest")
    documents = [_document_pin(pin) for pin in manifest["documents"]]
    _require(len({_json(pin) for pin in documents}) == len(documents), "duplicate document pin")
    media = manifest["media"]
    _require(isinstance(media, list), "invalid media manifest")
    for row in media:
        _require(isinstance(row, Mapping) and set(row) == {"media_id", "content_digest"}
                 and isinstance(row["media_id"], str) and _DIGEST.fullmatch(row["media_id"])
                 and row["content_digest"] == row["media_id"], "invalid immutable media pin")
    _require(len({row["media_id"] for row in media}) == len(media), "duplicate media pin")
    required_media = (
        _collect_media(_timeline_media_view(parent["payload"], include_parent_locations=True))
        | set().union(*(_collect_media(_without_meta(row["payload"])) for row in shots))
        | set().union(*(_collect_media(_timeline_media_view(row["payload"])) for row in internals))
    )
    _require(required_media <= {row["media_id"] for row in media}, "missing media dependency")
    _require(manifest == _manifest(shots, internals, media, documents),
             "dependency manifest does not match the immutable closure")
    _require(shots == sorted(shots, key=lambda row: (row["shot_id"], row["revision_id"]))
             and internals == sorted(internals, key=lambda row: (
                 row["timeline_id"], row["revision_id"])), "closure images must be sorted")


def bundle_from_document(document):
    """Open a complete immutable Runtime document without a Runtime dependency."""
    document = copy.deepcopy(document)
    _verify_document(document)
    project, timeline = document["project_id"], document["timeline_id"]
    bundle = open_authoring_bundle(
        {**document["parent_revision"], "project_id": project, "timeline_id": timeline},
        shot_revisions=[{**row, "project_id": project} for row in document["shot_revisions"]],
        internal_timeline_revisions=[{**row, "project_id": project}
                                     for row in document["internal_timeline_revisions"]],
    )
    bundle["source_mapping"]["_runtime_document"] = document
    return bundle


def _inverse_media_removals(records, bundle):
    """Only accepted edges can prove that a retained pin was introduced by writes."""
    from ._collections import AcceptedRecords, arrange, restoration_orders
    from ._command_state import ABSENT, CommandError, State, equal, root_identity
    from ._recording import Recording
    from ._step_undo import entry_records

    if not records:
        return set()
    from ._root_inverse import inverse_root_manifest

    if inverse_root_manifest(bundle, records) is not None:
        return set()
    if not isinstance(records, AcceptedRecords):
        raise CommandError('media inverse needs accepted boundaries', kind='history_unavailable')
    if not equal(list(records), [record for entry in records.entries
                                for record in entry_records(records, entry)]):
        raise CommandError('media inverse records differ from accepted history', kind='history_unavailable')
    removed = set()
    for entry in reversed(records.entries):
        before, after = records.load_boundary(entry)
        images = [value['source_mapping']['_runtime_document'] for value in (before, after)]
        for image, pin in zip(images, (entry['previous'], entry)):
            _verify_document(image)
            _require((image['project_id'], image['timeline_id']) ==
                     (bundle['project_id'], bundle['timeline_id'])
                     and image['parent_revision']['revision_id'] == pin['revision_id'],
                     'inverse media boundary mismatch')
        commands = entry['records']['commands']
        # Whole-closure restoration already selects its exact source manifest.
        if any(write['ref'] == 'timeline' for record in commands for write in record['writes']):
            continue
        prior_media, accepted_media = ({row['media_id'] for row in image['dependency_manifest']['media']}
                                       for image in images)
        introduced = accepted_media - prior_media
        if prior_media - accepted_media and any(record['command']['op'] != 'timeline.inverse'
                                                for record in commands):
            raise CommandError('unproven media removal boundary', kind='history_unavailable')
        removed.difference_update(prior_media - accepted_media)
        if not introduced:
            continue
        accepted = AcceptedRecords([entry], commands, records.load_boundary)
        state = State(copy.deepcopy(before))
        for record in commands:
            for write in [*record['writes'], *record['derived_writes']]:
                if not state.matches(write['ref'], write['path'], write['before']):
                    raise CommandError('media boundary differs from accepted writes', kind='history_unavailable')
                root = root_identity(write['ref'])
                created = ({'ref': write['ref'], 'kind': root[0],
                            'owner': None if root[0] == 'shot' else 'parent'} if root else None)
                if (root is not None and not write['path'] and write['before'] == ABSENT
                        and write['after']['state'] == 'present' and created in record['created']):
                    # Replaying authenticated semantic birth must retain the
                    # recorder's source-free compiler marker, just like execution.
                    Recording(state.bundle, record['command'], record['ordinal']).create_entity(
                        write['ref'], kind=root[0], owner=created['owner'],
                        value=write['after']['value'], restoring=True)
                else:
                    state.set(write['ref'], write['path'], write['after'])
            for (owner, kind), (_, expected) in restoration_orders(accepted, only=record).items():
                arrange(state, owner, kind, expected)
        replayed = document_from_bundle(state.bundle, source_bundles=(after,))
        # Scalar media_id/object_id writes carry their codec in the address;
        # retain that current guard and also account for the bounded payload
        # mirrors emitted by the native same-alias command.
        written_media = _collect_media([
            {write['path'][-1]: write['after'].get('value')} if write['path'] else write['after']
            for record in commands for write in record['writes']
        ])
        if (not equal(replayed['parent_revision']['payload'], images[1]['parent_revision']['payload'])
                or any(not equal(replayed[key], images[1][key]) for key in (
                    'shot_revisions', 'internal_timeline_revisions'))
                or not equal(replayed['dependency_manifest']['documents'],
                             images[1]['dependency_manifest']['documents'])
                or not introduced <= written_media | _collect_media([
                    write['after'] for record in commands for write in record['writes']
                ])):
            raise CommandError('unproven media boundary scope', kind='history_unavailable')
        selected = entry_records(records, entry)
        selected_media = _collect_media([
            {write['path'][-1]: write['after'].get('value')} if write['path'] else write['after']
            for record in selected for write in record['writes']
        ])
        surviving_media = _collect_media([
            {write['path'][-1]: write['after'].get('value')} if write['path'] else write['after']
            for record in commands if record['ordinal'] not in {item['ordinal'] for item in selected}
            for write in record['writes']
        ])
        removed.update(introduced & selected_media - surviving_media)
    return removed


def _inverse_clip_transcripts(records, bundle, *, pending=False):
    """Release first-use pins owned by a created clip removed by this inverse."""
    from ._collections import AcceptedRecords
    from ._command_state import ABSENT, State, equal
    from ._step_undo import entry_records
    from .inverse import coalesce

    if not isinstance(records, AcceptedRecords):
        return []
    removed = {write['ref'] for write in coalesce(records) if not write['path']
               and write['ref'].startswith('parent/clip:') and write['before'] == ABSENT
               and (pending or State(bundle).get(write['ref'], []) == ABSENT)}
    if not removed:
        return []
    parent = copy.deepcopy(bundle['parent'])
    for container in (parent, parent.get('config', {})):
        if isinstance(container.get('clips'), list):
            container['clips'] = [row for row in container['clips']
                                  if 'parent/clip:' + row['id'] not in removed]

    def contains(value, pin):
        if isinstance(value, dict):
            return equal(value, pin) or any(contains(child, pin) for child in value.values())
        return isinstance(value, list) and any(contains(child, pin) for child in value)

    result = []
    for entry in records.entries:
        choices = [write['after'].get('value') for record in entry_records(records, entry)
                   for write in record['writes'] if write['ref'] in removed
                   and write['path'][:3] == ['app', 'astrid', 'word_anchors']]
        before, after = records.load_boundary(entry)
        prior, accepted = (source['source_mapping']['_runtime_document']['dependency_manifest']['documents']
                           for source in (before, after))
        for pin in accepted:
            if (pin not in prior and pin['document_id'].startswith('transcript:')
                    and any(isinstance(choice, dict) and choice.get('document') == pin for choice in choices)
                    and not contains([parent, bundle['shots'], bundle['placements']], pin)):
                result.append(pin)
    return result


def document_from_bundle(bundle, *, document_pins=None, source_bundles=(), inverse_records=()):
    """Compile exact inputs; inverse pin removal requires provider-verified records.

    Historical source images alone never authorize dropping retained media.
    """
    # Immutable reuse follows exact encoded content, independently of value guards.
    source = bundle["source_mapping"]["_runtime_document"]
    publication = compile_authoring_candidate(bundle).publication
    from ._doc_patch_materialize import project
    publication = project(bundle, publication)
    shot_index = {row["revision_id"]: copy.deepcopy(row) for row in source["shot_revisions"]}
    internal_index = {row["revision_id"]: copy.deepcopy(row)
                      for row in source["internal_timeline_revisions"]}
    shot_index.update({row["revision_id"]: copy.deepcopy(row)
                       for row in publication["shot_revisions"]})
    internal_index.update({row["revision_id"]: copy.deepcopy(row)
                           for row in publication["internal_timeline_revisions"]})
    # Only authenticated publication boundaries may supply earlier immutable
    # images. Reuse them after exact authored-content comparison, never restore
    # a parent snapshot or replace a sibling's currently accepted pins.
    for historical in source_bundles:
        document = historical['source_mapping']['_runtime_document']
        _verify_document(document)
        _require((document['project_id'], document['timeline_id']) ==
                 (bundle['project_id'], bundle['timeline_id']), 'foreign inverse source')
        shot_index.update({row['revision_id']: copy.deepcopy(row) for row in document['shot_revisions']})
        internal_index.update({row['revision_id']: copy.deepcopy(row)
                               for row in document['internal_timeline_revisions']})
    parent = copy.deepcopy(publication["parent_composition"])
    # Opening shared occurrences gives them independent editing identities. A
    # parent-only edit must still reuse their ORIGINAL immutable shot images.
    for occurrence in parent["occurrences"]:
        identity = next(row["shot_id"] for row in bundle["placements"]
                        if row["occurrence_id"] == occurrence["occurrence_id"])
        shot = bundle["shots"][identity]
        origin = bundle["source_mapping"]["shots"].get(identity)
        if (origin is not None and _json(shot["payload"]) == _json(shot["base_payload"])
                and _json(shot["internal_timeline"]) == _json(shot["base_internal_timeline"])
                and origin.get("revision_id") in shot_index):
            occurrence["shot_id"] = origin["shot_id"]
            occurrence["shot_revision_id"] = origin["revision_id"]
            continue
        for historical in source_bundles:
            prior = next((row for row in historical['placements']
                          if row['occurrence_id'] == occurrence['occurrence_id']), None)
            if prior is None or prior['shot_id'] != identity:
                continue
            old = historical['shots'][identity]
            payload, old_payload = copy.deepcopy(shot['payload']), copy.deepcopy(old['payload'])
            payload.pop('internal_timeline_revision_id', None)
            old_payload.pop('internal_timeline_revision_id', None)
            if (_json(payload) != _json(old_payload)
                    or _json(shot['internal_timeline']) != _json(old['internal_timeline'])):
                continue
            origin = historical['source_mapping']['shots'][identity]
            occurrence['shot_id'] = origin['shot_id']
            occurrence['shot_revision_id'] = origin['revision_id']
            break
    shot_ids = {row["shot_revision_id"] for row in parent["occurrences"]}
    shots = sorted((shot_index[key] for key in shot_ids),
                   key=lambda row: (row["shot_id"], row["revision_id"]))
    internal_ids = {row["internal_timeline_revision_id"] for row in shots}
    internals = sorted((internal_index[key] for key in internal_ids),
                       key=lambda row: (row["timeline_id"], row["revision_id"]))
    copy_manifest = bundle["source_mapping"].get("_copy_manifest", {})
    documents = (copy_manifest.get("documents", source["dependency_manifest"]["documents"])
                 if document_pins is None else document_pins)
    if document_pins is None:
        documents = copy.deepcopy(documents)
        # A sealed manifest advances when a command consumes a verified source;
        # merely hydrating request inputs must preserve its predecessor revision.
        if '_copy_manifest' not in bundle['source_mapping']:
            for capture in bundle['source_mapping'].get('pinned_documents', []):
                if capture['pin'] not in documents:
                    documents.append(copy.deepcopy(capture['pin']))
        removals = _inverse_clip_transcripts(inverse_records, bundle)
        documents = [pin for pin in documents if pin not in removals]
    # Retained inputs may include media consumed through a transcript or other
    # immutable binding rather than a selected clip. Keep those existing pins.
    media = {row["media_id"]: row for row in copy_manifest.get(
        "media", source["dependency_manifest"]["media"])}
    media.update({row["media_id"]: row for row in publication["dependency_manifest"]["media"]})
    media.update({row["media_id"]: row for row in copy_manifest.get("media", [])})
    removals = _inverse_media_removals(inverse_records, bundle)
    if removals:
        from ._command_state import CommandError

        required = _collect_media([parent, *[row['payload'] for row in shots + internals]])
        captures = bundle['source_mapping'].get('pinned_documents', [])
        if any(not any(capture['pin'] == pin for capture in captures) for pin in documents):
            raise CommandError('media inverse source scope is unavailable', kind='history_unavailable')
        required.update(_collect_media([capture['document']['content'] for capture in captures
                                       if capture['pin'] in documents]))
        if required & removals:
            raise CommandError('introduced media has surviving dependents', kind='surviving_dependents')
        for identity in removals:
            media.pop(identity, None)
    manifest = _manifest(shots, internals, list(media.values()), documents)
    digest = _digest(parent)
    # A historical publication boundary wins over the opened source image.
    priors = [historical["source_mapping"]["_runtime_document"]
              for historical in source_bundles] + [source]
    revision = next((prior["parent_revision"]["revision_id"] for prior in priors
                     if digest == prior["parent_revision"]["content_digest"]
                     and _json(manifest) == _json(prior["dependency_manifest"])), None)
    if revision is None:
        revision = "authoring-parent-revision-" + _digest({
            "project_id": bundle["project_id"], "timeline_id": bundle["timeline_id"],
            "parent": parent, "manifest": manifest,
        }).removeprefix("sha256:")
    document = {
        "schema_version": 1, "project_id": bundle["project_id"],
        "timeline_id": bundle["timeline_id"],
        "parent_revision": {"revision_id": revision, "content_digest": digest, "payload": parent},
        "shot_revisions": shots, "internal_timeline_revisions": internals,
        "dependency_manifest": manifest,
    }
    _verify_document(document)
    return copy.deepcopy(document)


def _failure(reason, message, base, current=None, *, conflicts=None, path=(),
             hint=None, suggested_command=None, invalid=None, status=None):
    from ._commit_errors import failure

    return failure(reason, message, base, current, conflicts=conflicts, path=path,
                   hint=hint, suggested_command=suggested_command, invalid=invalid,
                   status=status)


def _load(port, context, revision):
    document = port.revision_closure(revision)
    request = context["request"]
    _require(document.get("project_id") == request["project_id"]
             and document.get("timeline_id") == request["timeline_id"]
             and document.get("parent_revision", {}).get("revision_id") == revision,
             "Runtime returned a different revision or project/timeline scope")
    return bundle_from_document(document)


def _hydrate(port, bundle, calls=(), *, inverse_records=(), inverse_sources=()):
    captures, schema_inputs = [], []
    document = bundle["source_mapping"]["_runtime_document"]
    pins = copy.deepcopy(document["dependency_manifest"]["documents"])
    from ._root_inverse import inverse_root_manifest

    root_manifest = inverse_root_manifest(bundle, inverse_records)
    if root_manifest is not None:
        # Hydrating a redo's retained documents must not add them to its input
        # boundary. The inverse installs their accepted manifest after writes.
        bundle['source_mapping']['_copy_manifest'] = copy.deepcopy(document['dependency_manifest'])
        pins = copy.deepcopy(root_manifest['documents'])
        for pin in root_manifest['media']:
            port.raw_media_info(copy.deepcopy(pin))
    removals = _inverse_clip_transcripts(inverse_records, bundle, pending=True)
    pins = [pin for pin in pins if pin not in removals]
    # Redo may restore the word source's media together with its anchored clip.
    # Only the same pin/media pair in an authenticated accepted closure qualifies.
    restored_pins = []
    if inverse_records:
        from .inverse import coalesce
        from .native_word_anchor import _namespace

        for write in coalesce(inverse_records):
            if (write['ref'].startswith('parent/clip:') and not write['path']
                    and write['before']['state'] == 'present'):
                for choice in _namespace(write['before']['value']).get('word_anchors', {}).values():
                    pin = _document_pin(choice['document'])
                    restored_pins.append(pin)
                    if pin not in pins:
                        pins.append(pin)
    for call in calls:
        pin = call.get('args', {}).get('schema_source')
        if call['op'] in ('clip.set-params', 'clip.set-element') and pin is not None:
            pin = _document_pin(pin)
            if pin not in pins:
                pins.append(pin)
        if call['op'] == 'clip.anchor' and call.get('args', {}).get('choice') is not None:
            pin = _document_pin(call['args']['choice']['document'])
            if pin not in pins:
                pins.append(pin)
    for pin in pins:
        transcript = pin["document_id"].startswith("transcript:")
        verified = (port.transcript_source(pin) if transcript else port.config_source(pin))
        _require(isinstance(verified, Mapping) and isinstance(verified.get("document"), Mapping),
                 "invalid scoped source capture")
        source = verified["document"]
        _require(verified["pin"] == pin and source.get("document_id") == pin["document_id"]
                 and source.get("project_id") == bundle["project_id"]
                 and type(source.get("version")) is int
                 and source.get("version") == pin["version"]
                 and _digest(source.get("content")) == pin["content_digest"],
                 "captured document does not match its exact scoped pin")
        if transcript:
            from astrid.sdk.media_transcript import transcript_document_id
            content = source["content"]
            _require(isinstance(content, Mapping) and source.get("kind") == "media.transcript"
                     and content.get("schema") == "media.transcript/v1"
                     and isinstance(content.get("media_id"), str)
                     and _DIGEST.fullmatch(content["media_id"])
                     and _id(content.get("source_id"))
                     and (content["media_id"] in {
                         row["media_id"] for row in document["dependency_manifest"]["media"]}
                         or pin in restored_pins and any(
                             pin in source['source_mapping']['_runtime_document']['dependency_manifest']['documents']
                             and content['media_id'] in {row['media_id'] for row in
                                 source['source_mapping']['_runtime_document']['dependency_manifest']['media']}
                             for source in inverse_sources))
                     and pin["document_id"] == transcript_document_id(
                         bundle["project_id"], content.get("media_id"), content.get("source_id")),
                     "invalid transcript source identity")
        elif source.get('kind') == 'astrid.project-pack':
            schema_inputs.append((copy.deepcopy(verified), {}))
        else:
            _require(isinstance(source.get("kind"), str) and source["kind"].endswith(".config"),
                     "unregistered config source kind")
        captures.append(copy.deepcopy(verified))
    bundle["source_mapping"]["pinned_documents"] = captures
    if not inverse_records and ('_copy_manifest' in bundle['source_mapping'] or calls and any(
            capture['pin'] not in document['dependency_manifest']['documents'] for capture in captures)):
        # Hydration trusts only Runtime's closure, never a caller's copy manifest.
        # Verified request inputs become closure dependencies only when consumed.
        bundle['source_mapping']['_copy_manifest'] = copy.deepcopy(document['dependency_manifest'])
    return schema_inputs


def _inverse_element_sources(port, records, *, context):
    """Recover restored PARAM declarations from their accepted input boundary."""
    from ._collections import AcceptedRecords
    from ._command_state import CommandError, State
    from ._selected_param_authority import resolve_selected_param_source
    from ._step_undo import entry_records
    from .inverse import coalesce

    if not records:
        return []
    if not isinstance(records, AcceptedRecords):
        raise CommandError('element inverse needs accepted boundaries', kind='history_unavailable')
    writes = coalesce(records)
    params = {write['ref'] for write in writes if write['path'][:1] == ['params']}
    restored = {write['ref']: write['before']['value'] for write in writes
                if write['ref'] in params and write['path'] in (['elementRef'], ['element_ref'])
                and write['before']['state'] == 'present' and write['before'] != write['after']
                and isinstance(write['before']['value'], dict)
                and write['before']['value'].get('packId')}
    sources = []
    for entry in records.entries:
        refs = {write['ref'] for record in entry_records(records, entry) for write in record['writes']
                if write['ref'] in restored and write['path'] in (['elementRef'], ['element_ref'])}
        if not refs:
            continue
        before, _ = records.load_boundary(entry)
        state = State(before)
        for ref in sorted(refs):
            selected = restored.pop(ref)
            row = state.entity(ref, optional=True)
            if row is None or (row.get('elementRef') or row.get('element_ref')) != selected:
                raise CommandError('restored element differs from accepted input',
                                   kind='history_unavailable', ref=ref)
            authority = resolve_selected_param_source(
                port, context=context, through=entry['previous'], select=ref)
            if (authority['element'] != selected or authority['source'] not in before[
                    'source_mapping']['_runtime_document']['dependency_manifest']['documents']):
                raise CommandError('restored element source differs from accepted input',
                                   kind='history_unavailable', ref=ref)
            sources.append((authority['source'], selected, authority['object']))
    if restored:
        raise CommandError('restored element boundary is unavailable', kind='history_unavailable')
    return sources


def _schema_resolver(port, inputs, inverse_records, *, context, through):
    """Resolve bytes at the real command stage, never by simulating handlers."""
    from astrid.core.element.project_packs import admitted_element_snapshot_descriptor

    from ._command_state import CommandError

    def resolve(ctx):
        command = ctx.record['command']
        required = []
        authority = None
        if command['op'] == 'clip.set-params':
            row = ctx.state.entity(command['select'])
            selected = row.get('elementRef') or row.get('element_ref')
            if isinstance(selected, dict) and selected.get('packId'):
                from ._selected_param_authority import resolve_selected_param_source

                authority = resolve_selected_param_source(
                    port, context=context, through=through, select=command['select'],
                    staged=ctx.prior_records)
                if 'schema_source' not in command['args']:
                    raise CommandError('selected project pack requires exact schema_source',
                                       ref=command['select'])
        if command['op'] in ('clip.set-params', 'clip.set-element') and 'schema_source' in command['args']:
            row = ctx.state.entity(command['select'])
            selected = (command['args']['elementRef'] if command['op'] == 'clip.set-element'
                        else row.get('elementRef') or row.get('element_ref'))
            if isinstance(selected, dict) and selected.get('packId'):
                required.append((command['args']['schema_source'], selected, None))
        elif command['op'] == 'timeline.inverse':
            for record in inverse_records:
                snapshots = [dep['pin'] for dep in record['dependencies']
                             if dep['key'] == 'params.snapshot']
                required.extend((pin['source'], pin['element'], pin['object']) for pin in snapshots)
                # Older accepted PARAM records already contain exact source and identity reads.
                if not snapshots and record['command']['op'] == 'clip.set-params':
                    pin = record['command']['args'].get('schema_source')
                    identity = {read['path'][0]: read['state'].get('value')
                                for read in record['reads']
                                if read['ref'] == record['command']['select']
                                and read['path'] in (['elementRef'], ['element_ref'])}
                    selected = identity.get('elementRef') or identity.get('element_ref')
                    if pin and isinstance(selected, dict) and selected.get('packId'):
                        required.append((pin, selected, None))
            required.extend(_inverse_element_sources(port, inverse_records, context=context))
        for pin, selected, retained_object in required:
            matches = [(verified, objects) for verified, objects in inputs
                       if verified['pin'] == pin and verified['document']['content'].get(
                           'pack_id') == selected.get('packId')]
            if len(matches) != 1:
                raise CommandError('selected pack snapshot input unavailable')
            verified, objects = matches[0]
            admissions = [item for item in verified['document']['content']['admissions']
                          if all(item.get(key) == selected.get(key)
                                 for key in ('id', 'kind', 'revision'))]
            descriptor = admissions[0].get('snapshot_object') if len(admissions) == 1 else None
            if retained_object is not None and descriptor != retained_object:
                raise CommandError('retained PARAM snapshot association is unavailable',
                                   kind='history_unavailable')
            snapshot = None
            if descriptor is not None:
                _require(callable(getattr(port, 'document_object', None)),
                         'Runtime document_object support is required for pack snapshots')
                object_id = descriptor['object_id']
                if object_id not in objects:
                    objects[object_id] = port.document_object(pin, object_id)
                snapshot = objects[object_id]
            if command['op'] == 'timeline.inverse' or authority is not None:
                try:
                    admitted_element_snapshot_descriptor(
                        ctx.realm_id, ctx.state.bundle['project_id'], selected, verified, snapshot)
                except ValueError as exc:
                    raise CommandError(str(exc)) from exc
            if authority is not None:
                if pin != authority['source'] or selected != authority['element']:
                    raise CommandError('selected PARAM source changed', kind='selection_changed',
                                       ref=command['select'])
                if authority['object'] != descriptor:
                    raise CommandError('selected PARAM snapshot association is unavailable',
                                       kind='history_unavailable', ref=command['select'])
            if command['op'] == 'timeline.inverse':
                ctx.dependency('params.schema', 'source', pin)
                ctx.dependency('params.snapshot', 'entity', {
                    'source': pin, 'element': selected, 'object': descriptor})
        return inputs

    return resolve


def _history(port, through, after, rejected, base):
    """Read a complete, bounded ancestry segment, never infer intent from bytes."""
    previous = copy.deepcopy(after)
    entries, cursor, cursors = [], None, set()
    while previous != through:
        page = port.history(through, after_publication_id=after["publication_id"] if after else None,
                            cursor=cursor, limit=100)
        if page["head"] != through:
            raise rejected(_failure("history_unavailable", "history head changed", base, through))
        for entry in page["items"]:
            if entry["previous"] != previous or entry["publication_id"] in {
                    row["publication_id"] for row in entries}:
                raise rejected(_failure("history_unavailable", "incomplete publication ancestry",
                                        base, through))
            entries.append(copy.deepcopy(entry))
            previous = {"revision_id": entry["revision_id"],
                        "publication_id": entry["publication_id"]}
        cursor = page["next_cursor"]
        if cursor is None:
            if previous != through:
                raise rejected(_failure("history_unavailable", "missing publication ancestry",
                                        base, through))
            break
        if cursor in cursors or previous == through:
            raise rejected(_failure("history_unavailable", "invalid history cursor", base, through))
        cursors.add(cursor)
    return entries


def _records(entries, rejected, base, current):
    records = []
    for entry in entries:
        recorder = entry.get("records")
        if (entry.get("intent") != "recorded" or not isinstance(recorder, Mapping)
                or recorder.get("schema_version") != 1
                or not isinstance(recorder.get("commands"), list) or not recorder["commands"]
                or any(not isinstance(record, Mapping) or record.get("ordinal") != ordinal
                       or set(record) != {"ordinal", "command", "writes", "reads", "selections",
                                          "bindings", "created", "dependencies", "derived_writes"}
                       for ordinal, record in enumerate(recorder["commands"]))):
            raise rejected(_failure("history_unavailable", "intervening write intent is unavailable",
                                    base, current))
        records.extend(copy.deepcopy(recorder["commands"]))
    return records


def _reservation_refs(state):
    """Known authored identities only; opaque payloads never allocate IDs."""
    bundle = state.bundle
    refs = {'shot:' + bundle['source_mapping']['shots'].get(identity, {}).get('shot_id', identity)
            for identity in bundle['shots']}
    owners = ['parent', *('occurrence:' + row['occurrence_id'] for row in bundle['placements'])]
    refs.update(owners[1:])
    for owner in owners:
        assets = state.registry(owner).get('assets', {})
        _require(isinstance(assets, dict), 'invalid asset identity collection')
        refs.update(owner + '/asset:' + identity for identity in assets)
        for kind in ('clip', 'track'):
            collection = state.collection(owner, kind)
            if collection is None:
                continue
            _require(isinstance(collection, list), 'invalid semantic identity collection')
            for row in collection:
                _require(isinstance(row, Mapping) and _id(row.get('id')),
                         'missing semantic identity')
                refs.add(owner + '/' + kind + ':' + row['id'])
    return refs


def _check_reservations(port, plan, current, rejected, base):
    from astrid.sdk.timeline.commands import CommandError

    from ._command_state import State, equal, one, root_identity

    def valid(item):
        if (not isinstance(item, Mapping) or set(item) != {'ref', 'kind', 'owner'}
                or not _id(item['ref']) or not _id(item['kind'])):
            return False
        root = root_identity(item['ref'])
        if root is not None:
            return (item['kind'] == root[0]
                    and item['owner'] == (None if root[0] == 'shot' else 'parent'))
        owner = item['owner']
        if not _id(owner) or not (owner == 'parent' or (
                root_identity(owner) is not None and root_identity(owner)[0] == 'occurrence')):
            return False
        return (item['kind'] in ('track', 'clip', 'asset')
                and re.fullmatch(re.escape(owner + '/' + item['kind'] + ':') + r'(?!.*::)[^/]+',
                                 item['ref']) is not None)

    for record in plan.records:
        if (any(not valid(item) for item in record['created'])
                or record['command'].get('created') != [item['ref'] for item in record['created']]):
            raise CommandError('invalid created identity scope', kind='identity_collision')

    # This scan is timeline-local. Runtime atomic admission must additionally
    # check project-wide shot occupancy/reservations before persisting a closure.
    # Inverse creates restore authenticated accepted writes. Ordinary creates
    # must never claim an identity retired by an earlier accepted publication.
    created = [item["ref"] for record in plan.records
               if record["command"]["op"] != "timeline.inverse" for item in record["created"]]
    if not created:
        return
    reserved, recorded = set(), False
    context = {'request': {key: plan.bundle[key] for key in ('project_id', 'timeline_id')}}

    def boundary(revision):
        return _load(port, context, revision)

    def restore_native_fork(state, record, write, accepted_base, requested_base, recorder_abi):
        """Retain the native fork proof while replaying an accepted birth."""
        command = record.get('command')
        if (not isinstance(command, Mapping)
                or command.get('op') != 'occurrence.fork'):
            return
        root = root_identity(write.get('ref'))
        args = command.get('args')
        owner = command.get('select')
        owner_root = root_identity(owner)
        if (root != ('shot', args.get('shot_id') if isinstance(args, Mapping) else None)
                or owner_root is None or owner_root[0] != 'occurrence'
                or not isinstance(args, Mapping) or set(args) != {'shot_id'}):
            raise CommandError('fork allocation proof is unavailable', kind='history_unavailable')
        destination = root[1]
        identity = owner_root[1]
        document = state.bundle['source_mapping'].get('_runtime_document')
        _verify_document(document)
        parent = document['parent_revision']
        _require((document['project_id'], document['timeline_id']) ==
                 (state.bundle['project_id'], state.bundle['timeline_id']),
                 'fork source scope changed')
        _require(state.bundle['base_parent'] == {
            key: parent[key] for key in ('revision_id', 'content_digest')
        }, 'fork source boundary changed')
        occurrence = one(parent['payload']['occurrences'], 'occurrence_id', identity)
        source_shot_id = occurrence['shot_id']
        source_revision_id = occurrence['shot_revision_id']
        source_shot = one(document['shot_revisions'], 'revision_id', source_revision_id)
        _require(source_shot['shot_id'] == source_shot_id,
                 'fork source shot binding changed')
        source_internal = one(document['internal_timeline_revisions'], 'revision_id',
                              source_shot['internal_timeline_revision_id'])
        from astrid.core.timeline.authoring_bundle import _CopiedShot, _copy_shot_for_placement

        copied_payload, _ = _copy_shot_for_placement(
            source_shot['payload'], project_id=document['project_id'],
            timeline_id=document['timeline_id'], occurrence_id=identity,
            authoring_shot_id=destination, source_shot_id=source_shot_id)
        expected = {'payload': copied_payload,
                    'internal_timeline': copy.deepcopy(source_internal['payload'])}
        _require(write['before'] == {'state': 'absent'}
                 and write['after']['state'] == 'present'
                 and equal(write['after']['value'], expected),
                 'fork native copy value changed')
        owner_row = state.occurrence(owner)
        _require(command.get('version') == '1'
                 and command.get('schema_version') == 1
                 and command.get('intent') == {'ownership': 'occurrence'}
                 and command.get('resolved') == [owner],
                 'fork command boundary changed')
        _require(command.get('created') == [write['ref']]
                 and record.get('created') == [{'ref': write['ref'], 'kind': 'shot', 'owner': None}],
                 'fork allocation record changed')

        owner_writes = [item for item in record.get('writes', [])
                        if item.get('ref') == owner and item.get('path') == ['shot_id']]
        _require(len(owner_writes) == 1
                 and owner_writes[0]['before'] == {
                     'state': 'present', 'value': owner_row['shot_id']}
                 and owner_writes[0]['after'] == {'state': 'present', 'value': destination},
                 'fork owner edge changed')

        required_reads = {
            (owner, ()): {'state': 'present', 'value': owner_row},
            (owner + '/shot', ('shot_id',)): {
                'state': 'present', 'value': source_shot_id},
            (owner + '/source', ()): {
                'state': 'present', 'value': source_internal['payload']},
            (owner + '/shot', ('payload',)): {
                'state': 'present', 'value': source_shot['payload']},
            (write['ref'], ()): {'state': 'absent'},
        }
        reads = record.get('reads', [])
        for (ref, path), expected_read in required_reads.items():
            matches = [item for item in reads
                       if item.get('ref') == ref and tuple(item.get('path', ())) == path]
            _require(len(matches) == 1 and equal(matches[0].get('state'), expected_read),
                     'fork source observation changed')

        from ._recording import selection

        selections = record.get('selections', [])
        _require({item.get('key') for item in selections} ==
                 {'collection.clips', 'collection.tracks'}
                 and all(item.get('owner') == owner for item in selections),
                 'fork collection order proof is unavailable')
        for selected in selections:
            _require(equal(selection(state, selected['key'], owner, selected['members']), selected),
                     'fork collection order changed')

        bindings = record.get('bindings', [])
        _require(isinstance(bindings, list)
                 and all(isinstance(item, Mapping) for item in bindings),
                 'fork binding proof is malformed')
        source_bindings = [item for item in bindings if item.get('key') == 'fork.source']
        owner_bindings = [item for item in bindings if item.get('key') == 'fork.owner']
        source_shots = {row['revision_id']: row for row in document['shot_revisions']}
        source_owners = ['occurrence:' + row['occurrence_id'] + '/shot'
                         for row in parent['payload']['occurrences']
                         if source_shots[row['shot_revision_id']]['internal_timeline_revision_id']
                         == source_internal['revision_id']]
        _require(len(source_bindings) == len(owner_bindings) == 1,
                 'fork source bindings are unavailable')
        source_binding = source_bindings[0]
        native_bindings = [
            {'key': key, 'ref': ref, 'policy': 'fixed', 'dependencies': []}
            for key, ref in (('collection.owner:collection.clips', owner),
                             ('collection.owner:collection.tracks', owner),
                             ('fork.source', owner + '/source'), ('fork.owner', owner))]
        _require(isinstance(accepted_base, Mapping)
                 and set(accepted_base) == {'revision_id', 'publication_id'}
                 and accepted_base['revision_id'] == parent['revision_id'],
                 'fork predecessor boundary changed')
        # ABI-1 native captures predate physical enrichment. Only the complete
        # native record shape may use that proof; mixed physical facts fail closed.
        native = bindings == native_bindings
        _require(not native or recorder_abi != 2,
                 'ABI2 fork bindings cannot use legacy allocation proof')
        if not native:
            # Every native role must retain its physical input proof. Checking
            # only the two fork roles admits partially stripped v2 records.
            for expected_binding in native_bindings:
                matches = [item for item in bindings if item.get('key') == expected_binding['key']]
                _require(len(matches) == 1, 'fork binding role is unavailable')
                binding = matches[0]
                expected = {**expected_binding,
                    'physical_ref': ('internal-revision:' + source_internal['revision_id']
                                     if binding is source_binding else 'parent'),
                    'revision_id': (source_internal if binding is source_binding else parent)['revision_id'],
                    'content_digest': (source_internal if binding is source_binding else parent)['content_digest'],
                    'path': [] if binding is source_binding else ['occurrences', 'occurrence_id:' + identity],
                    'boundary': binding.get('boundary')}
                if binding is source_binding:
                    expected['owners'] = source_owners
                _require(binding == expected, 'fork native binding schema changed')
            for binding in bindings:
                binding_boundary = binding.get('boundary')
                _require(isinstance(binding_boundary, Mapping)
                         and isinstance(binding.get('physical_ref'), str)
                         and bool(binding['physical_ref'])
                         and binding.get('policy') == 'fixed'
                         and binding.get('dependencies') == []
                         and binding_boundary.get('base_revision_id') == requested_base['revision_id']
                         and binding_boundary.get('base_publication_id') ==
                         requested_base['publication_id']
                         and binding_boundary.get('evaluated_head') == {
                             'revision_id': parent['revision_id'],
                             'publication_id': accepted_base['publication_id']}
                         and binding_boundary.get('command_ordinal') == record.get('ordinal')
                         and isinstance(binding_boundary.get('step_id'), str)
                         and bool(binding_boundary['step_id']),
                         'fork binding boundary changed')
                if binding.get('key') in {item['key'] for item in native_bindings}:
                    _require(set(binding_boundary) == {'base_revision_id', 'base_publication_id',
                             'evaluated_head', 'command_ordinal', 'step_id', 'phase'}
                             and binding_boundary['phase'] == 'input',
                             'fork input phase changed')
        state.bundle['shots'][destination] = _CopiedShot(
            state.bundle['shots'][destination], {
                'source_occurrence_id': identity, 'source_shot_id': source_shot_id,
                'source_revision_id': source_revision_id,
                'occurrence_id': identity, 'shot_id': destination})

    def scan(records, bundle, *, ordinary=False, output=None, accepted_base, requested_base,
             recorder_abi=None):
        state = State(bundle)
        reserved.update(_reservation_refs(state))
        for record in records:
            items = record['created']
            if record['command']['op'] == 'occurrence.place-shared':
                from .native_occurrence_shared import restore_shared_record

                restore_shared_record(state.bundle, record, output)
            if (not isinstance(items, list) or any(not valid(item) for item in items)
                    or not isinstance(record['command'], Mapping)
                    or record['command'].get('created') != [item['ref'] for item in items]):
                raise CommandError('created identity history is unavailable', kind='history_unavailable')
            check = ordinary and record['command']['op'] != 'timeline.inverse'
            claimed = set()
            for item in items:
                ref = item['ref']
                if check and (ref in reserved or ref in claimed):
                    raise CommandError('created identity was already reserved', kind='identity_collision', ref=ref)
                claimed.add(ref)
            for write in record['writes']:
                if write['ref'] == 'timeline':
                    for pin in (write['before']['value'], write['after']['value']):
                        source = boundary(pin['revision_id'])
                        image = source['source_mapping']['_runtime_document']['parent_revision']
                        _require(pin == {key: image[key] for key in ('revision_id', 'content_digest')},
                                 'historical closure pin mismatch')
                        state.closures[pin['revision_id']] = source
                if not state.matches(write['ref'], write['path'], write['before']):
                    raise CommandError('semantic identity boundary differs from writes', kind='history_unavailable')
                if (record['command']['op'] == 'timeline.inverse' and root_identity(write['ref']) is not None
                        and write['ref'].startswith('occurrence:') and not write['path']
                        and write['after']['state'] == 'present'):
                    from ._root_inverse import recover_source
                    recover_source(state, write['after']['value'], output)
                before = _reservation_refs(state)
                root = root_identity(write['ref'])
                created = ({'ref': write['ref'], 'kind': root[0],
                            'owner': None if root[0] == 'shot' else 'parent'} if root else None)
                if (root is not None and not write['path'] and write['before']['state'] == 'absent'
                        and write['after']['state'] == 'present' and created in items):
                    from ._recording import Recording
                    Recording(state.bundle, record['command'], record['ordinal']).create_entity(
                        write['ref'], kind=root[0], owner=created['owner'],
                        value=write['after']['value'], restoring=True)
                    if root[0] == 'shot':
                        restore_native_fork(state, record, write, accepted_base, requested_base,
                                            recorder_abi)
                else:
                    state.set(write['ref'], write['path'], write['after'])
                if write['ref'] == 'timeline':
                    state = State(state.bundle, closures=state.closures)
                after = _reservation_refs(state)
                collisions = (after - before) & reserved if check else set()
                if collisions:
                    raise CommandError('created identity was already reserved', kind='identity_collision',
                                       ref=sorted(collisions)[0])
                reserved.update(after)
            reserved.update(claimed)
            for write in record['derived_writes']:
                state.set(write['ref'], write['path'], write['after'])

    try:
        for entry in _history(port, current, None, rejected, base):
            if (not recorded and entry.get('intent') in {'bootstrap', 'legacy_unknown'}
                    and entry.get('records') is None):
                # Unknown legacy intent is not allocation proof. The first
                # recorded edge supplies its authenticated original boundary.
                continue
            recorded = True
            # Only Runtime's authenticated history entry supplies capture custody.
            # Pre-change entries have no field; explicit malformed values fail closed.
            recorder_abi = entry.get('recorder_abi', 1)
            _require(type(recorder_abi) is int and recorder_abi in (1, 2),
                     'invalid authenticated recorder ABI')
            records = _records([entry], rejected, base, current)
            _require(entry.get('previous') is not None, 'missing original identity boundary')
            scan(records, boundary(entry['previous']['revision_id']),
                 output=boundary(entry['revision_id']), accepted_base=entry['previous'],
                 recorder_abi=recorder_abi,
                 requested_base={key: entry['receipt']['result']['base_' + key]
                                 for key in ('revision_id', 'publication_id')})
            reserved.update(_reservation_refs(State(boundary(entry['revision_id']))))
    except (AuthoringBundleError, CommandError, KeyError, TypeError, ValueError) as exc:
        raise rejected(_failure('history_unavailable', 'semantic identity history is unavailable',
                                base, current)) from exc
    scan(plan.records, boundary(current['revision_id']), ordinary=True, output=plan.bundle,
         accepted_base=current, requested_base=base)


class RuntimeCommitService:
    provider_id = "astrid-v1"
    interface_version = 1

    def __init__(self, contract_digest):
        self.contract_digest = contract_digest

    def capture_candidate_inputs(self, document, *, candidate):
        """Pure optional inputs for Runtime's working-copy verification."""
        document, candidate = copy.deepcopy((document, candidate))
        return copy.deepcopy({
            "base": bundle_from_document(document),
            "preview": preview_authoring_candidate(candidate),
        })

    def project_capture(self, document, *, authoring_preview=None):
        """Pure optional capture admission; Runtime supplies a verified closure."""
        from astrid.core.timeline.shot_composition_projection import (
            project_runtime_parent_composition,
        )

        document = copy.deepcopy(document)
        baseline = bundle_from_document(document)
        project, timeline = document["project_id"], document["timeline_id"]
        parent = {**document["parent_revision"], "project_id": project, "timeline_id": timeline}
        shots = [{**row, "project_id": project} for row in document["shot_revisions"]]
        internals = [{**row, "project_id": project} for row in document["internal_timeline_revisions"]]
        marker = None
        if authoring_preview is not None:
            preview = copy.deepcopy(authoring_preview)
            _require(isinstance(preview, Mapping) and isinstance(preview.get("candidate"), Mapping),
                     "authoring_preview.candidate is required")
            _json(preview)
            frozen = preview["candidate"]
            for key in ("project_id", "timeline_id", "base_parent", "base_parent_payload"):
                _require(_json(frozen.get(key)) == _json(baseline[key]),
                         f"authoring_preview {key} does not match the exact base")
            source = frozen.get("source_mapping")
            _require(isinstance(source, Mapping) and isinstance(source.get("shots"), Mapping),
                     "authoring_preview source mapping is required")
            base_placements = frozen.get("base_placements")
            _require(isinstance(base_placements, Mapping) and isinstance(source.get("placements"), Mapping),
                     "authoring_preview placement base is inconsistent")
            _require(all(isinstance(row, Mapping) and row.get("occurrence_id") == identity
                         for identity, row in base_placements.items()),
                     "invalid authoring_preview placement base")
            for identity, placement in baseline["base_placements"].items():
                _require(_json(base_placements.get(identity)) == _json(placement),
                         "authoring_preview placement base is inconsistent")
            placements = frozen.get("placements")
            _require(isinstance(placements, list), "authoring_preview placements are required")
            for placement in placements:
                _require(isinstance(placement, Mapping), "invalid authoring_preview placement")
                identity = placement.get("occurrence_id")
                if identity in baseline["base_placements"]:
                    _require(_json(source["placements"].get(identity)) ==
                             _json(baseline["base_placements"][identity]),
                             "authoring_preview placement source is inconsistent")
            if "_runtime_document" in source:
                _require(_json(source["_runtime_document"]) == _json(document),
                         "authoring_preview document base is inconsistent")
            _require(isinstance(frozen.get("shots"), Mapping), "authoring_preview shots are required")
            for identity, shot in frozen["shots"].items():
                if identity not in baseline["shots"]:
                    continue  # New draft children are validated by the existing compiler below.
                _require(isinstance(shot, Mapping), "invalid authoring_preview shot")
                for key in ("base_payload", "base_internal_timeline"):
                    _require(_json(shot.get(key)) == _json(baseline["shots"][identity][key]),
                             "authoring_preview child base is inconsistent")
                _require(_json(source["shots"].get(identity)) ==
                         _json(baseline["source_mapping"]["shots"][identity]),
                         "authoring_preview child source is inconsistent")
            expected = preview_authoring_candidate(frozen)
            for key in ("kind", "head", "candidate_digest", "publication_digest", "publication"):
                _require(_json(preview.get(key)) == _json(expected[key]),
                         f"authoring_preview {key} is inconsistent")
            publication = expected["publication"]
            for family, pins in (("shot_revisions", "shots"),
                                 ("internal_timeline_revisions", "internal_timelines")):
                index = {row["revision_id"]: row for row in document[family]}
                index.update({row["revision_id"]: row for row in publication[family]})
                _require(all(pin["revision_id"] in index
                             for pin in publication["dependency_manifest"][pins]),
                         "authoring_preview retained closure is unavailable")
                document[family] = [copy.deepcopy(index[pin["revision_id"]])
                                    for pin in publication["dependency_manifest"][pins]]
            document["parent_revision"] = {
                "revision_id": publication["parent_revision_id"],
                "content_digest": publication["content_digest"], "payload": publication["parent_composition"],
            }
            manifest = publication["dependency_manifest"]
            document["dependency_manifest"] = {**manifest,
                "documents": document["dependency_manifest"]["documents"]}
            _verify_document(document)
            parent = {**document["parent_revision"], "project_id": project, "timeline_id": timeline}
            shots, internals = document["shot_revisions"], document["internal_timeline_revisions"]
            marker = {"label": "Unpublished working copy", "base_parent": frozen["base_parent"],
                      "candidate_digest": expected["candidate_digest"],
                      "publication_digest": expected["publication_digest"],
                      "candidate_parent_revision_id": publication["parent_revision_id"]}
        else:
            # The managed saved opener visits pins in first occurrence order.
            index = {(row["shot_id"], row["revision_id"]): row for row in shots}
            shots = list({(row["shot_id"], row["shot_revision_id"]):
                          index[(row["shot_id"], row["shot_revision_id"])]
                          for row in parent["payload"]["occurrences"]}.values())
            index = {row["revision_id"]: row for row in internals}
            internals = list({row["internal_timeline_revision_id"]:
                              index[row["internal_timeline_revision_id"]] for row in shots}.values())
        projected = project_runtime_parent_composition(
            parent, shot_revisions=shots, internal_timeline_revisions=internals)
        shot_rows = []
        for row in shots:
            payload = row["payload"]
            metadata = payload.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            shot_rows.append({"shot_id": row["shot_id"], "revision_id": row["revision_id"],
                "content_digest": row["content_digest"],
                "name": str(metadata.get("name") or metadata.get("title") or payload.get("name") or row["shot_id"]),
                "text_bindings": payload["text_bindings"] if isinstance(payload.get("text_bindings"), list) else []})
        expansion = {
            "canonical": True, "parent_revision_id": parent["revision_id"],
            "parent_content_digest": parent["content_digest"],
            "children": [{"timeline_id": row["timeline_id"], "revision_id": row["revision_id"],
                          "config_version": 1, "config_hash": row["content_digest"]} for row in internals],
            "shots": shot_rows, "occurrences": list(projected.occurrences),
        }
        if marker is None:
            expansion.update(outputs=list(projected.outputs), graph=projected.graph)
        else:
            expansion["working_copy"] = marker
        result = {"config": projected.config, "registry": projected.registry, "expansion": expansion}
        _json(result)
        return copy.deepcopy(result)

    def commit(self, context, port):
        from runtime_protocol.timeline_commit import CommitRejected, StaleHead

        from astrid.sdk.timeline._collections import AcceptedRecords
        from astrid.sdk.timeline._command_state import State
        from astrid.sdk.timeline._commit_patch import inverse_address_resolver, rebase_patch
        from astrid.sdk.timeline._step_undo import check_later_steps, select_inverse_records
        from astrid.sdk.timeline.commands import CommandError, execute
        from astrid.sdk.timeline.native_media_admission import capture_media_admissions
        from astrid.sdk.timeline.producer import capture_producer
        from astrid.sdk.timeline.rebase import replay

        context = copy.deepcopy(context)
        request = context["request"]
        base = {"revision_id": request["base_revision_id"],
                "publication_id": request["base_publication_id"]}
        current = None
        try:
            calls = request["commands"]
            from astrid.sdk.timeline.commands import validate_command
            verified_media = []
            # Step selection is provider control over authenticated history. The
            # native ABI still executes the existing whole-record inverse form.
            execution_calls = copy.deepcopy(calls)
            for call in execution_calls:
                if call.get('op') == 'timeline.inverse' and isinstance(call.get('args'), dict):
                    call['args'].pop('steps', None)
                validate_command(call)
            verified_media.extend(capture_media_admissions(port, calls))
            verified_producer = capture_producer(port, request)
            for command_index, call in enumerate(calls):
                if call['op'] == 'text-binding.set' and 'text' in call['args']:
                    verified_media.append(port.text_selection(copy.deepcopy(call['args']['text'])))
                if call['op'] in ('clip.add-media', 'clip.duplicate-media', 'clip.swap-media', 'occurrence.add-media', 'occurrence.swap-media', 'placement.create',
                                'shot.pool'):
                    for key in ('old_media', 'media'):
                        if key in call['args']:
                            if call['op'] == 'clip.duplicate-media':
                                from runtime_protocol.errors import (
                                    ConflictError,
                                    NotFoundError,
                                    ValidationError,
                                )

                                try:
                                    selected = port.selected_media(copy.deepcopy(call['args'][key]))
                                except (ConflictError, NotFoundError, ValidationError) as exc:
                                    invalid = not isinstance(exc, ConflictError)
                                    raise CommitRejected(_failure(
                                        'invalid_media_selection' if invalid else 'dependency_changed',
                                        str(exc), base, port.head(),
                                        path=['commands', str(command_index), 'args', key], invalid=invalid,
                                        status='invalid' if invalid else 'conflict',
                                        hint='Retrieve the exact current Runtime media selection',
                                        suggested_command='media show')) from exc
                                verified_media.append(selected)
                            else:
                                verified_media.append(port.selected_media(copy.deepcopy(call['args'][key])))
            restore = any(call["op"] == "timeline.restore" for call in calls)
            if restore:
                current = port.head()
                if len(calls) != 1:
                    raise CommitRejected(_failure("invalid_commands", "restore must be the sole command",
                                                  base, current))
                if current != base:
                    raise CommitRejected(_failure("stale_head", "restore requires the exact head pair",
                                                  base, current))
            bundle = _load(port, context, base["revision_id"])
            current = port.head()
            base_entry = port.history_entry(base["publication_id"])
            if base_entry["revision_id"] != base["revision_id"]:
                raise CommitRejected(_failure("history_unavailable", "base publication/revision mismatch",
                                              base, current))
            initialize = any(call['op'] == 'timeline.init' for call in calls)
            if initialize and (base_entry['intent'] != 'bootstrap'
                               or base_entry['previous'] is not None or current != base):
                raise CommandError('initialization requires the original bootstrap publication',
                                   kind='not_pristine')
            undo_of, undo_steps = [], None
            if sum(call["op"] == "timeline.inverse" for call in calls) > 1:
                raise CommitRejected(_failure("unsupported_operation",
                                              "use one inverse command listing all target publications",
                                              base, current))
            for call in calls:
                if call["op"] == "timeline.inverse":
                    if 'steps' in call['args'] and not isinstance(call['args']['steps'], dict):
                        raise CommandError('inverse steps must be a publication map', kind='invalid_steps')
                    undo_steps = copy.deepcopy(call["args"].get("steps"))
                    for publication in call["args"]["publications"]:
                        if publication not in undo_of:
                            undo_of.append(publication)
            inverse_records, inverse_intervening, inverse_sources = [], [], []
            if undo_of:
                targets = [port.history_entry(publication) for publication in undo_of]
                segments = []
                for target in targets:
                    pin = {"revision_id": target["revision_id"],
                           "publication_id": target["publication_id"]}
                    # Loading the exact target also verifies source scope; no
                    # client-provided inverse writes enter the core engine.
                    _load(port, context, target["revision_id"])
                    segment = [target, *_history(port, base, pin, CommitRejected, base)]
                    segments.append(segment)
                # The longest segment begins at the earliest targeted edge.
                ordered = max(segments, key=len)
                if undo_of != [entry["publication_id"] for entry in ordered
                               if entry["publication_id"] in undo_of]:
                    raise CommitRejected(_failure("invalid_inverse_order",
                                                  "inverse publications must be oldest to newest",
                                                  base, current))
                accepted = [entry for entry in ordered if entry['publication_id'] in undo_of]

                def load_boundary(entry):
                    previous = entry.get('previous')
                    if not previous:
                        raise CommandError('inverse input boundary is unavailable', kind='history_unavailable')
                    prior = port.history_entry(previous['publication_id'])
                    if prior['revision_id'] != previous['revision_id']:
                        raise CommandError('inverse input boundary mismatch', kind='history_unavailable')
                    pin = {key: entry[key] for key in ('revision_id', 'publication_id')}
                    _history(port, pin, previous, CommitRejected, base)
                    return (_load(port, context, previous['revision_id']),
                            _load(port, context, entry['revision_id']))

                inverse_sources = [source for entry in accepted for source in load_boundary(entry)]
                _records(ordered, CommitRejected, base, current)
                accepted_records = AcceptedRecords(accepted, [record for entry in accepted
                    for record in entry['records']['commands']], load_boundary)
                inverse_history = ordered

                def select_at(head_bundle, history):
                    resolver = (inverse_address_resolver(history, head_bundle, load_boundary=load_boundary,
                                                        load_history=port.history_entry)
                        if any('physical_ref' in binding for record in accepted_records
                               for binding in record['bindings']) else None)
                    selected = select_inverse_records(accepted_records, undo_steps, address_resolver=resolver)
                    check_later_steps(selected, history, state=State(head_bundle))
                    return selected

                inverse_records = select_at(bundle, inverse_history)
            closures, restored_from = {}, None
            if restore:
                restored_from = copy.deepcopy(calls[0]['args'])
                source = _load(port, context, restored_from['revision_id'])
                image = source['source_mapping']['_runtime_document']['parent_revision']
                _require(image['content_digest'] == restored_from['content_digest'],
                         "restore source digest mismatch")
                closures[restored_from['revision_id']] = source
            else:
                # Only authenticated accepted writes can authorize an inverse
                # closure replacement. Never accept a caller-supplied image.
                for write in (write for record in inverse_records for write in record['writes']):
                    if write['ref'] == 'timeline' and not write['path']:
                        pin = write['before']['value']
                        source = _load(port, context, pin['revision_id'])
                        image = source['source_mapping']['_runtime_document']['parent_revision']
                        _require(pin == {key: image[key] for key in ('revision_id', 'content_digest')},
                                 "inverse historical closure pin mismatch")
                        closures[pin['revision_id']] = source
            # The port captures exactly the resulting document inputs. Discarded
            # current/source closures must not retain unrelated document pins.
            schema_inputs = []
            for source in closures.values():
                schema_inputs.extend(_hydrate(port, source))
            if not closures:
                schema_inputs = _hydrate(port, bundle, calls, inverse_records=inverse_records,
                                         inverse_sources=inverse_sources)
            resolve_inputs = _schema_resolver(port, schema_inputs, inverse_records,
                                              context=context, through=base)
            from .native_occurrence_shared import resolve_source
            resolve_inputs = resolve_source(port, resolve_inputs, inverse_records)
            original = execute(bundle, execution_calls, inverse_records=inverse_records,
                               intervening_records=inverse_intervening, closures=closures,
                               verified_media=verified_media, realm_id=context['realm_id'],
                               resolve_inputs=resolve_inputs, verified_producer=verified_producer,
                               commit_request=request, evaluated_head=base)
            for attempt in range(3):
                bindings = {}
                if attempt:
                    current = port.head()
                # An equal document after undo is still a later publication.
                # CAS retries must recheck this fact before ordinary replay.
                if initialize and current != base:
                    raise CommandError('initialization cannot rebase over an accepted edit',
                                       kind='not_pristine')
                if current == base:
                    plan = original
                else:
                    if restore:
                        raise CommitRejected(_failure("stale_head", "restore requires the exact head pair",
                                                      base, current))
                    entries = _history(port, current, base, CommitRejected, base)
                    head_bundle = _load(port, context, current["revision_id"])
                    if undo_of:
                        inverse_records = select_at(head_bundle, [*inverse_history, *entries])
                    if not closures:
                        schema_inputs = _hydrate(port, head_bundle, calls, inverse_records=inverse_records,
                                             inverse_sources=inverse_sources)
                    resolve_inputs = _schema_resolver(port, schema_inputs, inverse_records,
                                                      context=context, through=current)
                    resolve_inputs = resolve_source(port, resolve_inputs, inverse_records)
                    if undo_of or any(call['op'] == 'doc.patch' for call in calls) or any(
                            key in request for key in ('merge', 'reads', 'steps')):
                        _records(entries, CommitRejected, base, current)
                        def patch_boundary(entry):
                            previous = entry.get('previous')
                            if previous is None:
                                raise CommandError('missing history boundary', kind='history_unavailable')
                            return (_load(port, context, previous['revision_id']),
                                    _load(port, context, entry['revision_id']))
                        bindings = ({} if undo_of else rebase_patch(
                            original, entries, head_bundle, load_boundary=patch_boundary))
                        try:
                            replay_calls = (execution_calls if undo_of else [
                                call if call['op'] == 'doc.patch' else original.commands[index]
                                for index, call in enumerate(calls)])
                            plan = execute(head_bundle, replay_calls, inverse_records=inverse_records,
                                           intervening_records=[] if undo_of else _records(
                                               entries, CommitRejected, base, current), closures=closures,
                                           verified_media=verified_media, realm_id=context['realm_id'],
                                           resolve_inputs=resolve_inputs, verified_producer=verified_producer,
                                           commit_request=request, evaluated_head=current,
                                           replay_bindings=bindings)
                        except CommandError as exc:
                            if exc.kind == 'previous_value_mismatch':
                                exc.kind = ('selection_changed' if getattr(exc, 'dependency_kind', None) == 'collection'
                                            else 'dependency_changed')
                            raise
                    else:
                        plan = replay(original, head_bundle, inverse_records=inverse_records,
                                  intervening_records=[] if undo_of else _records(
                                      entries, CommitRejected, base, current), closures=closures,
                                  verified_media=verified_media, realm_id=context['realm_id'],
                                  resolve_inputs=resolve_inputs, verified_producer=verified_producer,
                                  commit_request=request, evaluated_head=current)
                for record in plan.records:
                    if record['command']['op'] == 'timeline.inverse' and undo_steps is not None:
                        record['command']['args']['steps'] = copy.deepcopy(undo_steps)
                _check_reservations(port, plan, current, CommitRejected, base)
                from astrid.sdk.timeline.validation import validate_final_candidate

                validation_bundle = plan.bundle
                if ('_copy_manifest' in plan.bundle['source_mapping']
                        or len({row['shot_id'] for row in plan.bundle['placements']})
                        != len(plan.bundle['placements'])):
                    # Validate shared physical roots through main's canonical
                    # per-occurrence adapter; the accepted closure stays shared.
                    validation_bundle = bundle_from_document(document_from_bundle(plan.bundle))
                    validation_bundle['source_mapping']['pinned_documents'] = copy.deepcopy(
                        plan.bundle['source_mapping'].get('pinned_documents', []))
                validate_final_candidate(
                    validation_bundle,
                    baseline=(closures[restored_from['revision_id']] if restored_from is not None
                              else bundle if current == base else head_bundle),
                    schema_inputs=schema_inputs,
                    realm_id=context.get('realm_id'),
                    source_bundles=[*inverse_sources, *closures.values()],
                    restoration_records=plan.records if inverse_records or restore else (),
                    inverse_records=inverse_records,
                )
                document = document_from_bundle(plan.bundle, source_bundles=inverse_sources,
                                                inverse_records=inverse_records)
                from ._commit_preview import capture_sequential_execution, prepared_records

                records = prepared_records(plan, undo_of=undo_of,
                                           restored_from=restored_from)
                # R2's guarded per-step inverse selection is part of the
                # authenticated recorder, including when the official v2
                # preview helper builds the common record fields.
                records["undo_steps"] = copy.deepcopy(undo_steps)
                prepared = {
                    "expected_head": copy.deepcopy(current), "canonical_document": document,
                    "document_digest": _digest(document),
                    "records": records,
                    "id_map": {}, "semantic_events": copy.deepcopy(plan.semantic_events),
                    "provider_version": PROVIDER_VERSION, "compiler_version": COMPILER_VERSION,
                }
                try:
                    capture_sequential_execution(port, context, original, plan, bindings, prepared)
                    if request.get("dry_run", False):
                        # Runtime authenticates and retains this exact outcome on
                        # the port.  Any response projection after this call
                        # would make the provider's result differ from the
                        # authenticated candidate.
                        return port.preview(prepared)
                    return port.accept(prepared)
                except StaleHead as exc:
                    current = copy.deepcopy(exc.current_head)
                    if restore:
                        raise CommitRejected(_failure("stale_head", "restore head changed at acceptance",
                                                      base, current)) from exc
                    continue
            raise CommitRejected(_failure("head_busy", "timeline head changed on three attempts",
                                          base, current))
        except AuthoringBundleError as exc:
            raise CommitRejected(_failure("invalid_document", str(exc), base, current,
                                          invalid=current == base or current is None)) from exc
        except CommandError as exc:
            path = getattr(exc, "path", ())
            if not path and exc.kind in {"invalid_commands", "invalid_edit_command",
                                         "invalid_steps", "unsupported_operation",
                                         "unsupported_version"}:
                path = ["commands"]
            conflicts = ([exc.to_json()] if exc.kind in {
                "value_conflict", "identity_conflict", "identity_collision", "inverse_conflict",
                "dependency_changed", "inverse_dependency_changed",
                "intervening_write", "selection_changed", "surviving_dependents",
                "atomic_scope_changed", "binding_changed", "ambiguous_ref"} else None)
            raise CommitRejected(_failure(exc.kind, str(exc), base, current,
                                          conflicts=conflicts, path=path,
                                          invalid=None)) from exc


def create_runtime_commit_service():
    """Load Runtime compatibility only when the fixed provider is selected."""
    from runtime_protocol.contract_metadata import SCHEMA_DIGEST

    return RuntimeCommitService(SCHEMA_DIGEST)
