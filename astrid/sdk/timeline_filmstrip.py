"""Freeze a managed render and its exact script identities for visual review."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from fractions import Fraction
from typing import Any

from .exceptions import CapabilityValidationError
from .pagination import page_pair, paged_rows
from .project_render import _SUCCESS_STATES, _identifier, _render_capability, _state

_AUTHORITY_IDENTITY_FIELDS = (
    'project_id', 'project_slug', 'timeline_id', 'timeline_slug', 'timeline_ulid',
    'config_version', 'head_event_id', 'head_hash', 'config_hash', 'registry_hash',
    'materialized_registry_hash',
)


def matching_composed_render(
    client: Any, *, project_id: str, timeline: Mapping[str, Any], limit: int = 50,
) -> str | None:
    """Return one recent successful render whose frozen authority is current.

    This is intentionally a bounded read. A candidate must identify the exact
    timeline and match every current immutable identity field available on the
    timeline row, including at least one revision/content pin. Ambiguous or
    incomplete authority fails closed.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        return None
    timeline_id = _identifier(timeline, "timeline_id", "id")
    if not project_id or not timeline_id:
        return None
    current: dict[str, Any] = {"timeline_id": timeline_id}
    for key in ("project_id", "project_slug", "timeline_slug", "timeline_ulid", "config_version",
                "head_event_id", "head_hash", "config_hash", "registry_hash", "materialized_registry_hash"):
        if timeline.get(key) is not None:
            current[key] = timeline[key]
    revision = _identifier(timeline, "parent_revision_id", "head_revision_id", "revision_id")
    if revision:
        current["parent_revision_id"] = revision

    def exact_authority(authority: Mapping[str, Any]) -> bool:
        if _identifier(authority, "timeline_id") != timeline_id:
            return False
        if _is_candidate_render({}, authority):
            candidate = authority.get("authoring_preview")
            expected_parent = current.get("parent_revision_id")
            if (
                not isinstance(candidate, Mapping)
                or not expected_parent
                or candidate.get("candidate_parent_revision_id") != expected_parent
                or not candidate.get("candidate_digest")
                or not candidate.get("publication_digest")
            ):
                return False
        immutable = ("parent_revision_id", "config_version", "head_hash", "config_hash", "registry_hash",
                     "materialized_registry_hash", "head_event_id")
        pins = [key for key in immutable if current.get(key) is not None]
        if not pins:
            return False
        aliases = {
            "parent_revision_id": ("parent_revision_id", "head_revision_id", "revision_id"),
        }
        matched_pin = False
        for key in pins:
            names = aliases.get(key, (key,))
            actual = next((authority.get(name) for name in names if authority.get(name) is not None), None)
            if actual is None or actual != current[key]:
                return False
            matched_pin = True
        return matched_pin

    # A transport that only exposes the native input-view surface has no
    # render-history reader. That is enough for the explicit input-only
    # capability, but it cannot satisfy the default composed-output route.
    # The caller must keep the composed request fail-closed rather than
    # silently replacing it with declared inputs.
    list_runs = getattr(client, "list_project_runs", None)
    if not callable(list_runs):
        return None
    try:
        page_value = list_runs(project_id, cursor=None, limit=limit)
    except Exception:
        return None
    if hasattr(page_value, "ok") and hasattr(page_value, "data"):
        if not bool(page_value.ok):
            return None
        page_value = page_value.data
    page = page_pair(page_value)
    if page is None:
        return None
    rows, _next_cursor = page
    if len(rows) > limit:
        return None
    ordered = sorted(
        (row for row in rows if isinstance(row, Mapping)
         and _render_capability(row) == "rendering.render" and _state(row) in _SUCCESS_STATES),
        key=lambda row: (str(row.get("created_at") or row.get("updated_at") or ""),
                         _identifier(row, "run_id", "id")),
        reverse=True,
    )
    for listed in ordered:
        run_id = _identifier(listed, "run_id", "id")
        if not run_id:
            continue
        run = client.get_run(run_id)
        if (not isinstance(run, Mapping) or _state(run) not in _SUCCESS_STATES
                or _identifier(run, "project_id", "project") != project_id):
            continue
        task_ids = run.get("task_ids")
        if not isinstance(task_ids, list) or len(task_ids) > 20:
            continue
        render_tasks = []
        for task_id in task_ids:
            task = client.get_task(str(task_id))
            if isinstance(task, Mapping) and _render_capability(task) == "rendering.render" and _state(task) in _SUCCESS_STATES:
                render_tasks.append(task)
        if len(render_tasks) != 1:
            continue
        task = render_tasks[0]
        try:
            envelope = _envelope(task)
            authority = _authority(envelope)
        except CapabilityValidationError:
            continue
        frozen_inputs = envelope.get("inputs")
        if isinstance(frozen_inputs, Mapping) and frozen_inputs.get("profile") is not None:
            # A caller-selected render profile is not interchangeable with the
            # normal timeline profile for implicit auto/final-output reuse.
            # Such previews remain inspectable by explicit run ID.
            continue
        if not exact_authority(authority):
            continue
        result = task.get("result")
        raw_outputs = result.get("outputs") if isinstance(result, Mapping) else None
        if not isinstance(raw_outputs, list) and isinstance(result, Mapping):
            raw_outputs = result.get("output_objects")
        outputs = [item for item in (raw_outputs or []) if isinstance(item, Mapping)
                   and _identifier(item, "output_port", "port", "name") == "video"]
        if len(outputs) > 1:
            continue
        from .project_render import (
            _association_needs_managed_lookup,
            _find_publication_association,
            _lookup_managed_output,
        )
        output = outputs[0] if outputs else {}
        association = _find_publication_association(task=task, run=run, output=output)
        if association is None or _association_needs_managed_lookup(association):
            association = _lookup_managed_output(
                client=client,
                project_id=project_id,
                run_id=run_id,
                task_id=_identifier(task, "task_id", "id"),
                output=output,
                association=association,
            )
        if association is not None:
            # Ordered by creation time: the first verified exact output is
            # the fresh authority for this state. Older renders of the same
            # immutable state are harmless and must not make auto ambiguous.
            return run_id
    return None


def _fail(message: str, *, details: Mapping[str, Any] | None = None) -> None:
    """Raise a typed preflight error without collapsing recovery metadata."""
    raise CapabilityValidationError(message, details=details)


def _status_failure(status: Mapping[str, Any]) -> None:
    """Expose the shared lifecycle classification in SDK validation details."""
    _fail(str(status.get("label") or "timeline render is not available"), details={
        "inspection_status": dict(status),
        "next_actions": list(status.get("next_actions") or status.get("actions") or []),
    })


def _envelope(task: Mapping) -> Mapping:
    value = task.get('spec', {})
    for _ in range(4):
        if isinstance(value, Mapping) and 'inputs' in value:
            return value
        value = value.get('spec', {}) if isinstance(value, Mapping) else {}
    _fail('Render has no immutable input snapshot; rerender the selected timeline.')


def _authority(envelope: Mapping) -> Mapping:
    """Resolve the renderer's frozen authority from both supported envelopes."""
    inputs = envelope.get('inputs', {})
    if not isinstance(inputs, Mapping):
        _fail('Render has malformed frozen inputs; rerender the selected timeline.')
    legacy = envelope.get('authority_context')
    canonical = inputs.get('timeline_authority')
    if legacy is not None and not isinstance(legacy, Mapping):
        _fail('Render has malformed legacy authority; rerender the selected timeline.')
    if canonical is not None and not isinstance(canonical, Mapping):
        _fail('Render has malformed timeline authority; rerender the selected timeline.')
    if isinstance(legacy, Mapping) and isinstance(canonical, Mapping):
        for field in _AUTHORITY_IDENTITY_FIELDS:
            if (legacy.get(field) is not None and canonical.get(field) is not None
                    and legacy.get(field) != canonical.get(field)):
                _fail('Render authority sources disagree; rerender the selected timeline.')
    return legacy or canonical or {}


def _is_candidate_render(envelope: Mapping, authority: Mapping | None = None) -> bool:
    """Return whether a managed render is an unpublished candidate preview.

    Latest filmstrip selection is canonical-only.  An explicit ``render_run``
    remains allowed to inspect a candidate, but a candidate must never mask
    the latest published render merely because it was created later.
    """
    values = [authority or {}, envelope]
    for value in values:
        if not isinstance(value, Mapping):
            continue
        mode = str(value.get("render_mode") or value.get("mode") or "").strip().lower()
        if mode in {"authoring_candidate_preview", "candidate", "preview", "unpublished"}:
            return True
        if isinstance(value.get("authoring_preview"), Mapping):
            return True
    return False


def _explicit_render_selection(
    envelope: Mapping, authority: Mapping[str, Any], timeline: Mapping[str, Any] | None,
) -> str:
    """Truthfully label an explicitly selected immutable render authority."""
    if _is_candidate_render(envelope, authority):
        return "explicit_candidate_preview"
    if not isinstance(timeline, Mapping):
        return "explicit_unverified_legacy_render"
    aliases = {
        "parent_revision_id": ("parent_revision_id", "head_revision_id", "revision_id"),
    }
    compared = 0
    for key in (
        "parent_revision_id", "config_version", "head_event_id", "head_hash",
        "config_hash", "registry_hash", "materialized_registry_hash",
    ):
        expected = next(
            (timeline.get(name) for name in aliases.get(key, (key,)) if timeline.get(name) is not None),
            None,
        )
        actual = next(
            (authority.get(name) for name in aliases.get(key, (key,)) if authority.get(name) is not None),
            None,
        )
        if expected is None:
            continue
        if actual is None:
            return "explicit_unverified_legacy_render"
        compared += 1
        if expected != actual:
            return "explicit_historical_render"
    return "explicit_current_render" if compared else "explicit_unverified_legacy_render"


def _timeline_snapshot(envelope: Mapping) -> Mapping:
    """Resolve the frozen snapshot in legacy and direct-render envelopes."""
    inputs = envelope.get('inputs', {})
    if not isinstance(inputs, Mapping):
        _fail('Render has malformed frozen inputs; rerender the selected timeline.')
    nested_present = 'timeline_snapshot' in inputs
    sibling_present = 'timeline_snapshot' in envelope
    nested = inputs.get('timeline_snapshot')
    sibling = envelope.get('timeline_snapshot')
    if nested_present and not isinstance(nested, Mapping):
        _fail('Render has malformed frozen timeline snapshot; rerender the selected timeline.')
    if sibling_present and not isinstance(sibling, Mapping):
        _fail('Render has malformed frozen timeline snapshot; rerender the selected timeline.')
    if nested_present and sibling_present and nested != sibling:
        _fail('Render frozen timeline snapshots disagree; rerender the selected timeline.')
    snapshot = nested if nested_present else sibling if sibling_present else {}
    return snapshot


def _canonical_shot_occurrences(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    app = config.get("app") if isinstance(config, Mapping) else None
    composition = app.get("astrid_shot_composition") if isinstance(app, Mapping) else None
    occurrences = composition.get("occurrences") if isinstance(composition, Mapping) else None
    return [dict(item) for item in occurrences if isinstance(item, Mapping)] if isinstance(occurrences, list) else []


def _expand_input_snapshot(client: Any, config: Mapping[str, Any], registry: Mapping[str, Any], authority: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accept only an admission-owned canonical input snapshot.

    Child expansion belongs to the exact parent-composition projection before
    this function is called. This lane never reopens mutable timeline
    documents or interprets legacy ``clipType=shot`` shells.
    """
    raw_clips = config.get("clips") if isinstance(config, Mapping) else None
    if isinstance(raw_clips, list) and any(
        isinstance(c, Mapping) and c.get("clipType") == "shot" for c in raw_clips
    ):
        _fail("Input inspection found legacy clipType=shot entries; migrate the timeline offline before review.")
    return deepcopy(dict(config)), deepcopy(dict(registry))


def _open_current_input_closure(
    client: Any, *, project_id: str, timeline_row: Mapping[str, Any],
    config: Mapping[str, Any], registry: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Open the current timeline through its immutable parent head.

    A child shot cannot be expanded from a mutable timeline document: that
    can join a newer child revision to an older parent. The managed
    render path already owns the exact parent/shot/internal-revision opener;
    reuse that seam for the render-free inspection lane. Flat legacy timeline
    documents are not a supported read representation.
    """
    clips = config.get("clips") if isinstance(config, Mapping) else []
    has_shot_placements = isinstance(clips, list) and any(
        isinstance(clip, Mapping) and clip.get("clipType") == "shot" for clip in clips
    )
    parent_head = timeline_row.get("head_revision_id") or timeline_row.get("parent_revision_id")
    if not has_shot_placements and not parent_head:
        return deepcopy(dict(config)), deepcopy(dict(registry)), {}
    if not isinstance(parent_head, str) or not parent_head:
        _fail("Input inspection requires an immutable parent composition head for shot placements.")
    try:
        from astrid.packs.rendering.actions.render.managed_timeline import _project_exact_parent_head

        parent, projected, expansion = _project_exact_parent_head(
            client=client, project_id=project_id,
            timeline_id=str(_identifier(timeline_row, "timeline_id", "id")),
            parent_revision_id=parent_head,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        _fail(f"Input inspection cannot open the exact parent composition closure: {exc}")
    projected_config = getattr(projected, "config", None)
    projected_registry = getattr(projected, "registry", None)
    if not isinstance(projected_config, Mapping) or not isinstance(projected_registry, Mapping):
        _fail("Runtime returned an invalid exact parent composition closure.")
    authority = {
        "parent_revision_id": parent_head,
        "parent_content_digest": parent.get("content_digest"),
        "children": deepcopy(list((expansion or {}).get("children") or [])),
        "shots": deepcopy(list((expansion or {}).get("shots") or [])),
        "occurrences": deepcopy(list((expansion or {}).get("occurrences") or [])),
        "canonical": True,
    }
    return deepcopy(dict(projected_config)), deepcopy(dict(projected_registry)), authority


def _explicit_capture_revision(
    client: Any, run_id: str, *, project_id: str, timeline_id: str,
) -> str:
    """Resolve an explicit render selector to its admitted saved parent revision.

    The render itself is never a frame source. It may only identify a saved
    canonical revision that the exact-revision reader will independently
    admit and materialize for composed frame capture.
    """
    try:
        run = client.get_run(run_id)
    except Exception as exc:
        _fail(f"Explicit render_run {run_id!r} cannot be resolved to a saved revision: {exc}")
    if not isinstance(run, Mapping):
        _fail(f"Explicit render_run {run_id!r} cannot be resolved to a saved revision.")
    if _identifier(run, "project_id", "project") != project_id:
        _fail(f"Explicit render_run {run_id!r} belongs to a different project.")
    if _render_capability(run) != "rendering.render" or _state(run) not in _SUCCESS_STATES:
        _fail(f"Explicit render_run {run_id!r} is not a successful rendering.render run.")
    task_ids = run.get("task_ids")
    if not isinstance(task_ids, list) or len(task_ids) > 20:
        _fail(f"Explicit render_run {run_id!r} has no admitted immutable render task.")
    tasks = []
    for task_id in task_ids:
        try:
            task = client.get_task(str(task_id))
        except Exception as exc:
            _fail(f"Explicit render_run {run_id!r} task cannot be resolved: {exc}")
        if isinstance(task, Mapping) and _render_capability(task) == "rendering.render":
            tasks.append(task)
    if len(tasks) != 1 or _state(tasks[0]) not in _SUCCESS_STATES:
        _fail(f"Explicit render_run {run_id!r} has no single successful immutable render task.")
    try:
        envelope = _envelope(tasks[0])
        authority = _authority(envelope)
    except CapabilityValidationError as exc:
        _fail(f"Explicit render_run {run_id!r} has no admitted immutable saved revision: {exc}")
    if _is_candidate_render(envelope, authority):
        _fail(f"Explicit render_run {run_id!r} is an unpublished candidate, not a saved canonical revision.")
    if _identifier(authority, "project_id", "project") not in (None, project_id):
        _fail(f"Explicit render_run {run_id!r} pins a different project.")
    if _identifier(authority, "timeline_id") != timeline_id:
        _fail(f"Explicit render_run {run_id!r} pins a different timeline.")
    revision = _identifier(authority, "parent_revision_id", "head_revision_id", "revision_id")
    if not revision:
        _fail(f"Explicit render_run {run_id!r} does not pin an immutable saved parent revision.")
    return revision


def _candidate_shot_row(shot: Mapping[str, Any]) -> dict[str, Any]:
    """The shot row a snapshot expansion carries (same shape as a managed render's)."""
    payload = shot.get("payload") if isinstance(shot.get("payload"), Mapping) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    return {
        "shot_id": shot["shot_id"],
        "revision_id": shot["revision_id"],
        "name": str(metadata.get("name") or metadata.get("title") or payload.get("name") or shot["shot_id"]),
        "text_bindings": list(payload.get("text_bindings") or []) if isinstance(payload.get("text_bindings"), list) else [],
    }


def _open_candidate_input_closure(
    client: Any, *, project_id: str, preview: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Like ``_open_current_input_closure``, but for an unpublished working copy (a frozen candidate)."""
    from astrid.sdk.authoring_render_preview import candidate_projection

    try:
        built = candidate_projection(preview, client=client, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 - one actionable line
        _fail(f"The working copy cannot be projected for inspection: {exc}")
    projected = built["projected"]
    publication = built["publication"]
    authority = {
        "parent_revision_id": publication["parent_revision_id"],
        "children": [
            {"timeline_id": row["timeline_id"], "revision_id": row["revision_id"],
             "config_version": 1, "config_hash": row["content_digest"]}
            for row in built["internals"]
        ],
        "shots": [_candidate_shot_row(row) for row in built["shots"]],
        "occurrences": [dict(row) for row in projected.occurrences],
        "canonical": True,
        "working_copy": built["marker"],
    }
    return deepcopy(dict(projected.config)), deepcopy(dict(projected.registry)), authority


def _occurrence_timing(occurrence: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the placement window of one occurrence as seconds (at/hold).

    Runtime occurrence rows carry a rational placement ``speed`` object and
    the projection already resolved ``at``/``hold`` (or ``at_ms``/``duration_ms``)
    in seconds.  Timing validation only understands the clip shape, so reduce
    the occurrence to that window before frame arithmetic; legacy fixtures that
    already use ``at``/``hold`` pass through unchanged.
    """
    if 'at' in occurrence and 'hold' in occurrence:
        return {'at': occurrence['at'], 'hold': occurrence['hold']}
    if 'at_ms' in occurrence and 'duration_ms' in occurrence:
        return {'at': float(occurrence['at_ms']) / 1000.0, 'hold': float(occurrence['duration_ms']) / 1000.0}
    return occurrence


def _digest(value: Any) -> str:
    raw = str(value or '').removeprefix('sha256:')
    if len(raw) != 64 or any(c not in '0123456789abcdef' for c in raw):
        _fail('Render contains an invalid managed object digest.')
    return 'sha256:' + raw


def _read_text(client: Any, binding: Mapping) -> str:
    object_id = _digest(binding.get('media_id'))
    if object_id != binding.get('content_hash'):
        _fail('Frozen script binding identity does not match its content hash.')
    response = client.get_object(object_id)
    raw = response.get('data') if isinstance(response, Mapping) else response.data
    if not isinstance(raw, bytes) or 'sha256:' + hashlib.sha256(raw).hexdigest() != object_id or len(raw) != binding.get('byte_size'):
        _fail('Frozen script bytes do not match the render binding digest and size.')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        _fail('Frozen script is not UTF-8 text.')


def _duration(clip: Mapping) -> float:
    from astrid.core.timeline.duration import clip_timeline_duration
    return clip_timeline_duration(clip)


def _is_spoken_binding(binding: Mapping) -> bool:
    """Whether a shot text binding belongs on the spoken-word review lane."""
    kind = binding.get('kind')
    if kind == 'voiceover_script':
        return True
    # A generic transcript is not safe to display as narration unless the
    # admission explicitly marks it spoken.  This prevents prompt and
    # generation metadata from becoming accidental filmstrip captions.
    return kind in {'transcript', 'transcript_text'} and binding.get('spoken') is True


def _is_verified_speech_annotation(annotation: Mapping) -> bool:
    """Allow only frozen speech annotations, never prompt/transcript traps."""
    source_type = str(annotation.get('source_type') or annotation.get('kind')
                      or annotation.get('type') or '').strip().lower()
    if source_type in {'generation_prompt', 'prompt', 'positive_prompt', 'negative_prompt',
                       'unmarked_transcript', 'unmarked-transcript'}:
        return False
    if source_type in {'transcript', 'transcript_text'}:
        return annotation.get('spoken') is True or annotation.get('verified') is True
    if source_type in {'verified_speech', 'verified-speech', 'spoken_transcript', 'spoken-transcript'}:
        return True
    # Preserve the existing frozen annotation shape when no source classifier
    # is present; explicit negative markers still cannot enter captions.
    return annotation.get('spoken') is not False and annotation.get('verified') is not False


def build_filmstrip_snapshot(envelope: Mapping, *, client: Any, project: str, run_id: str, video_digest: str) -> dict:
    """Pure snapshot mapping except verified reads of pinned immutable text objects."""
    inputs = envelope.get('inputs', {})
    authority = _authority(envelope)
    timeline = _timeline_snapshot(envelope)
    config = timeline.get('config', {})
    if not isinstance(config.get('clips'), list) or not authority.get('timeline_id'):
        _fail('Render lacks a frozen canonical timeline; rerender it before visual review.')
    canvas = config.get('theme_overrides', {}).get('visual', {}).get('canvas', {})
    profile = inputs.get('profile') or {}
    if not isinstance(profile, Mapping):
        _fail('Render profile is not a frozen profile mapping; rerender with explicit frame rate.')
    fps_value = profile.get('fps_rational') or canvas.get('fps')
    if isinstance(fps_value, (list, tuple)) and len(fps_value) == 2:
        fps = Fraction(*fps_value)
    elif fps_value:
        fps = Fraction(str(fps_value))
    else:
        _fail('Render snapshot does not record its frame rate; rerender with an explicit canvas.')
    if fps <= 0:
        _fail('Render frame rate is invalid.')
    tracks = {t['id']: t.get('kind', '') for t in config.get('tracks', [])}
    from astrid.core.timeline.duration import (
        clip_end_frame,
        clip_start_frame,
        timeline_duration_frames,
        timeline_render_duration_frames,
    )
    authored_duration_frames = timeline_duration_frames(config, float(fps))
    rendered_duration_frames = timeline_render_duration_frames(config, float(fps))
    clips = []
    for raw in config['clips']:
        clip = deepcopy(dict(raw))
        # These fields are render-admission provenance, not authored timeline
        # input.  Do not expose caller-authored/fabricated labels until an
        # admission-owned occurrence below proves the identity.
        for key in ('shot_id', 'shot_name', 'occurrence_id', 'occurrence_ids'):
            clip.pop(key, None)
        clip.update(kind=tracks.get(raw.get('track'), ''), duration=_duration(raw),
            start_frame=clip_start_frame(raw, float(fps)), end_frame=clip_end_frame(raw, float(fps)))
        clips.append(clip)
    # These occurrences were admitted by the renderer from canonical registered
    # shots. Never infer a shot from a filename, ordinal, or current document.
    # Only admission-owned flattened occurrences prove a rendered shot
    # identity.  Legacy review_context entries carry authored timing only and
    # must never be used to infer a shot label or occurrence by overlap.
    occurrences = authority.get('expansion', {}).get('occurrences', [])
    if not isinstance(occurrences, list):
        occurrences = []
    frozen_shots = {
        s['shot_id']: s for s in authority.get('expansion', {}).get('shots', [])
        if isinstance(s, Mapping) and s.get('shot_id')
    }
    verified_text = {}

    def verified_binding_text(binding: Mapping) -> str:
        """Verify each frozen spoken binding once without projecting identity."""
        key = binding.get('binding_id') or binding.get('media_id')
        if key not in verified_text:
            verified_text[key] = _read_text(client, binding)
        return verified_text[key]

    # Integrity of a frozen spoken script remains authoritative even when the
    # render carries no admitted occurrence.  This validates bytes only; it
    # does not create a shot label, timing, or script projection.
    for shot in frozen_shots.values():
        for binding in shot.get('text_bindings', []):
            if _is_spoken_binding(binding):
                verified_binding_text(binding)
    scripts = []
    shot_occurrences = []
    for occurrence in occurrences:
        if not isinstance(occurrence, Mapping):
            continue
        shot_id = occurrence.get('shot_id')
        # Runtime parent-revision projections name the placement ``occurrence_id``;
        # the flattened-clip spelling ``shot_occurrence_id`` is the legacy key.
        # Reading only the legacy key dropped every occurrence, which left clips
        # unlabelled and suppressed the shot scripts (spoken text) entirely.
        occurrence_id = occurrence.get('occurrence_id') or occurrence.get('shot_occurrence_id')
        shot = frozen_shots.get(shot_id)
        if not shot or not occurrence_id:
            continue
        timing = _occurrence_timing(occurrence)
        start_frame = clip_start_frame(timing, float(fps))
        end_frame = clip_end_frame(timing, float(fps))
        start_frame = max(0, min(start_frame, authored_duration_frames))
        end_frame = max(start_frame, min(end_frame, authored_duration_frames))
        if end_frame <= start_frame:
            continue
        start = start_frame / float(fps); end = end_frame / float(fps)
        shot_occurrences.append({'occurrence_id': occurrence_id, 'shot_id': shot_id,
            'shot_name': shot['name'], 'start': start, 'end': end,
            'start_frame': start_frame, 'end_frame': end_frame})
        # Admission stamped the exact occurrence onto every flattened payload.
        # A missing stamp is not silently repaired by timing or authored order.
        for clip in clips:
            if clip.get('shot_occurrence_id') == occurrence_id:
                clip.setdefault('occurrence_ids', []).append(occurrence_id)
                clip.update(shot_id=shot_id, shot_name=shot['name'], occurrence_id=occurrence_id)
        for binding in shot.get('text_bindings', []):
            # Prompt bindings describe how a frame was generated; they are not
            # narration.  The filmstrip is a spoken-word review surface, so
            # never leak positive/negative generation prompts into its cards.
            if not _is_spoken_binding(binding):
                continue
            text = verified_binding_text(binding)
            # A canonical shot script is not an aligned audio transcript. Even
            # a single overlapping audio clip could be music: don't invent timing.
            scripts.append({'start': start, 'end': end, 'text': text,
                'shot_id': shot_id, 'shot_name': shot['name'], 'kind': binding['kind'],
                'occurrence_id': occurrence_id, 'start_frame': start_frame, 'end_frame': end_frame,
                'binding_id': binding['binding_id'], 'head': binding['head'],
                'media_id': binding['media_id'], 'timing_basis': 'shot_script',
                'label': 'Shot script (not word-aligned)'})
    # A render may carry an immutable provider-independent annotation set in
    # its frozen input envelope.  Project it here only; opening a filmstrip
    # never discovers a transcript or calls an ASR provider.
    from astrid.packs.rendering.actions.timeline_visualize.speech_projection import (
        project_speech_annotations,
    )
    raw_annotations = inputs.get('speech_annotations', inputs.get('transcript_annotations'))
    # Shot occurrences carry render placement, not source-audio bounds.  Only
    # an explicitly admitted speech occurrence may be used for projection;
    # falling back would make an unrelated phrase look word-aligned.
    raw_occurrences = inputs.get('speech_occurrences')
    audio_analysis = inputs.get('audio_analysis')
    audio = deepcopy(audio_analysis) if isinstance(audio_analysis, Mapping) else None
    if raw_annotations is not None or audio is not None:
        audio = audio or {}
        if isinstance(raw_annotations, list):
            raw_annotations = [annotation for annotation in raw_annotations
                               if isinstance(annotation, Mapping) and _is_verified_speech_annotation(annotation)]
        audio['speech'] = project_speech_annotations(
            raw_annotations if isinstance(raw_annotations, list) else [],
            raw_occurrences if isinstance(raw_occurrences, list) else [],
            source_audio_digest=inputs.get('source_audio_digest') or audio.get('source_audio_digest'),
            transcript_digest=inputs.get('transcript_digest'),
            annotation_digest=inputs.get('annotation_digest'),
            correction_version=inputs.get('correction_version', 0),
            timing_method=inputs.get('timing_method'),
            coverage=inputs.get('speech_coverage'),
        )
    from astrid.sdk.timeline.music_lane import project_snapshot as project_music_lane_snapshot
    audio_lane = project_music_lane_snapshot({
        'config': config,
        'registry': timeline.get('registry') or inputs.get('registry') or {},
        'fps': float(fps),
        'project_id': project,
        'timeline_id': authority['timeline_id'],
    })
    result = {'project_slug': project, 'timeline_id': authority['timeline_id'],
        'timeline_name': authority.get('timeline_slug', authority['timeline_id']),
        'render_run_id': run_id, 'video_digest': video_digest,
        'fps_rational': [fps.numerator, fps.denominator],
        'duration_frames': rendered_duration_frames,
        'clips': clips, 'scripts': scripts, 'occurrences': shot_occurrences,
        'tracks': deepcopy(config.get('tracks', [])),
        'registry': deepcopy(timeline.get('registry') or inputs.get('registry') or {}),
        'metadata': {'canonical_timeline': deepcopy(authority),
            'authored_duration_frames': authored_duration_frames,
            'rendered_duration_frames': rendered_duration_frames,
            'script_timing': 'shot_script',
            'script_mapping_available': bool(occurrences and frozen_shots),
            'script_mapping_note': 'Frozen shot script; no word alignment.' if occurrences and frozen_shots else 'Render did not pin shot placements and scripts; rerender for script labels.'}}
    if audio_lane is not None:
        result['audio_lane'] = audio_lane
    if audio is not None:
        result['audio'] = audio
    return result


def prepare_filmstrip(
    inputs: Mapping, *, project: str, client: Any = None,
    _composed_media_attachment: bool = False,
) -> dict:
    """Select a successful managed render; never admit a caller-provided file."""
    if client is None:
        from .client import AstridClient
        with AstridClient.open_from_launcher() as connected:
            return prepare_filmstrip(inputs, project=project, client=connected)
    # Public SDK calls arrive through AstridClient; the timelines facade
    # dispatches through RemoteAstridClient directly. Both wrap the same
    # workspace transport whose project/run readers admission needs.
    remote = getattr(client, '_remote', client)
    client = getattr(remote, '_transport', remote)
    from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import render_status
    if inputs.get('rendered_video'):
        _fail('Filmstrip review accepts a managed --render-run, not --rendered-video.')
    project_row = client.get_project(project)
    project_id = _identifier(project_row, 'project_id', 'id')
    selector = inputs.get('timeline_slug') or inputs.get('timeline_ref')
    exact = inputs.get('render_run')
    if exact == 'latest':
        exact = None
    canonical_project = str(project_row.get('slug') or project)
    from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import normalize_components
    components = normalize_components(inputs.get('show'), inputs.get('hide'))
    timeline_row = None
    if selector or not exact:
        selector = selector or project_row.get('metadata', {}).get('default_timeline_id')
        rows = paged_rows(client.list_timelines, project_id, limit=50) or []
        timeline_row = next((r for r in rows if selector in {r.get('timeline_id'), r.get('id'), r.get('slug')}), None)
        if timeline_row is None:
            _fail('Select a canonical timeline before filmstrip review.')
    # Input-only inspection is a read-only timeline projection. It intentionally
    # does not list runs, inspect tasks, or request a render. The executor gets
    # this immutable row through the same authority envelope as rendered
    # review, so input/output admission remains one coherent path.
    # An exact render-run selector must take the immutable snapshot pinned by
    # that run, even when a timeline slug is also supplied.  Only the ordinary
    # input-only path may read the current canonical timeline row.
    if 'output' not in components['resolved'] and timeline_row is not None and not exact:
        if not (timeline_row.get('head_revision_id') or timeline_row.get('parent_revision_id')):
            _fail('Timeline has no canonical current parent head.')
        if isinstance(inputs.get('authoring_preview'), Mapping):
            config, registry, closure = _open_candidate_input_closure(
                client, project_id=project_id, preview=inputs['authoring_preview'])
        else:
            config, registry, closure = _open_current_input_closure(
                client, project_id=project_id, timeline_row=timeline_row,
                config={}, registry={'assets': {}},
            )
        from astrid.core.timeline.duration import timeline_duration_frames
        canvas = config.get('theme_overrides', {}).get('visual', {}).get('canvas', {})
        fps = canvas.get('fps', 30) if isinstance(canvas, Mapping) else 30
        try:
            fps_fraction = Fraction(str(fps))
            duration_frames = timeline_duration_frames(config, float(fps_fraction))
        except (TypeError, ValueError):
            fps_fraction = Fraction(30, 1)
            duration_frames = 0
        from astrid.sdk.timeline.music_lane import project_snapshot as project_music_lane_snapshot
        audio_lane = project_music_lane_snapshot({
            'config': config, 'registry': registry, 'fps': float(fps_fraction),
            'project_id': project_id, 'timeline_id': authority.get('timeline_id'),
        })
        input_snapshot = {
            'project_slug': canonical_project, 'timeline_id': authority.get('timeline_id'),
            'timeline_name': selector or authority.get('timeline_slug') or authority.get('timeline_id'),
            'fps_rational': [fps_fraction.numerator, fps_fraction.denominator], 'duration_frames': int(duration_frames),
            'clips': deepcopy(config.get('clips') or []),
            'pinned_shots': deepcopy(config.get('pinnedShotGroups') or []),
            'shot_occurrences': _canonical_shot_occurrences(config),
            'tracks': deepcopy(config.get('tracks') or []),
            'registry': deepcopy(dict(registry)),
            'metadata': {
                'selection': 'render_pinned_input_only',
                'render_run_id': _identifier(run, 'id', 'run_id'),
                'authority': deepcopy(dict(authority)),
                'input_expansion': {'children': deepcopy((authority.get('expansion') or {}).get('children') or []), 'flattened': True},
                'timeline_identity': {
                    key: authority.get(key)
                    for key in ('timeline_id', 'timeline_slug', 'config_version', 'head_event_id', 'head_hash', 'registry_hash')
                    if authority.get(key) is not None
                },
            },
        }
        if audio_lane is not None:
            input_snapshot['audio_lane'] = audio_lane
        return {
            'mode': 'input_only', 'project_id': project_id,
            'timeline_id': _identifier(timeline_row, 'timeline_id', 'id'),
            'timeline_slug': _identifier(timeline_row, 'slug', 'timeline_id', 'id'),
            'component_request': components,
            'input_snapshot': {
                'project_slug': canonical_project,
                'timeline_id': _identifier(timeline_row, 'timeline_id', 'id'),
                'timeline_name': _identifier(timeline_row, 'slug', 'timeline_id', 'id'),
                'fps_rational': [fps_fraction.numerator, fps_fraction.denominator], 'duration_frames': int(duration_frames),
                'clips': deepcopy(config.get('clips') or []),
                # Preserve authored shot order/membership for render-free
                # filmstrip selectors (the flattened clip list alone does not
                # carry pinnedShotGroups).
                'pinned_shots': deepcopy(config.get('pinnedShotGroups') or []),
                'shot_occurrences': _canonical_shot_occurrences(config),
                'tracks': deepcopy(config.get('tracks') or []),
                'registry': deepcopy(dict(registry)),
                'metadata': {
                    'selection': 'input_only',
                    'authority': 'canonical_parent_revision_closure' if closure else 'canonical_timeline_snapshot',
                    'input_expansion': {
                        'children': deepcopy(closure.get('children') or []),
                        'flattened': bool(closure) or any(isinstance(c, Mapping) and c.get('shot_occurrence_id') for c in config.get('clips', [])),
                    },
                    'timeline_identity': {
                        key: timeline_row.get(key)
                        for key in ('timeline_id', 'slug', 'config_version', 'head_event_id', 'head_hash', 'registry_hash')
                        if timeline_row.get(key) is not None
                    },
                    **({'parent_revision_id': closure['parent_revision_id']} if closure else {}),
                    **({'working_copy': deepcopy(dict(closure['working_copy']))}
                       if isinstance(closure.get('working_copy'), Mapping) else {}),
                },
            },
        }
    if inputs.get('composed_capture') and timeline_row is None:
        _fail('Composed frame capture requires a selected canonical timeline.')
    if inputs.get('composed_capture') and timeline_row is not None:
        requested_revision = inputs.get('revision_id')
        if exact:
            run_revision = _explicit_capture_revision(
                client,
                str(exact),
                project_id=str(project_id),
                timeline_id=str(_identifier(timeline_row, 'timeline_id', 'id')),
            )
            if requested_revision in (None, ''):
                requested_revision = run_revision
            elif str(requested_revision) != str(run_revision):
                _fail('Explicit revision_id conflicts with the saved revision selected by render_run.')
        capture_row = dict(timeline_row)
        if requested_revision not in (None, ''):
            capture_row['head_revision_id'] = str(requested_revision)
            capture_row['parent_revision_id'] = str(requested_revision)
        if not (capture_row.get('head_revision_id') or capture_row.get('parent_revision_id')):
            _fail('Composed frame capture requires an immutable parent revision.')
        if isinstance(inputs.get('authoring_preview'), Mapping):
            config, registry, closure = _open_candidate_input_closure(
                client, project_id=project_id, preview=inputs['authoring_preview'])
            requested_revision = None
        else:
            try:
                config, registry, closure = _open_current_input_closure(
                    client, project_id=project_id, timeline_row=capture_row,
                    config={}, registry={'assets': {}},
                )
            except CapabilityValidationError as exc:
                _fail(f'Composed frame capture cannot resolve admitted saved revision {requested_revision!r}: {exc}')
            except Exception as exc:
                _fail(f'Composed frame capture cannot resolve admitted saved revision {requested_revision!r}: {exc}')
        # The parent projection carries canonical media identities, but the
        # generic host only accepts a runtime-admitted registry snapshot. Keep
        # this composed-capture path aligned with managed render admission so
        # every asset is authorized and materializable before Remotion runs.
        from astrid.packs.rendering.actions.render.managed_timeline import (
            _runtime_snapshot_registry,
        )
        registry_client = remote if callable(
            getattr(getattr(remote, 'media', None), 'list', None)
        ) else client
        registry = _runtime_snapshot_registry(
            registry, project_ref=canonical_project, client=registry_client
        )
        parent_revision = (
            closure.get('parent_revision_id')
            or capture_row.get('head_revision_id')
            or capture_row.get('parent_revision_id')
        )
        if requested_revision not in (None, '') and str(parent_revision) != str(requested_revision):
            _fail('Composed frame capture resolved a different saved revision than the requested revision.')
        # Current-head hashes and versions are not historical revision
        # identity. Bind capture/cache identity to the immutable revision and
        # the exact materialized closure below.
        authority = {
            key: capture_row.get(key)
            for key in ('project_id', 'project_slug', 'timeline_id', 'timeline_slug', 'timeline_ulid')
            if capture_row.get(key) is not None
        }
        authority['timeline_id'] = _identifier(capture_row, 'timeline_id', 'id')
        authority['parent_revision_id'] = parent_revision
        if closure.get('parent_content_digest') is not None:
            authority['parent_content_digest'] = closure['parent_content_digest']
        authority['expansion'] = deepcopy(closure)
        canvas = config.get('theme_overrides', {}).get('visual', {}).get('canvas', {})
        fps_value = canvas.get('fps', 30) if isinstance(canvas, Mapping) else 30
        try:
            fps = Fraction(str(fps_value))
        except (TypeError, ValueError, ZeroDivisionError):
            fps = Fraction(30, 1)
        capture_identity = 'sha256:' + hashlib.sha256(
            json.dumps(
                {'authority': authority, 'config': config, 'registry': registry},
                sort_keys=True, separators=(',', ':'), default=str,
            ).encode('utf-8')
        ).hexdigest()
        capture_id = f"composed-capture:{authority['timeline_id']}:{parent_revision}"
        envelope = {
            'inputs': {
                'timeline_snapshot': {'config': config, 'registry': registry},
                'profile': {'fps_rational': [fps.numerator, fps.denominator]},
                'timeline_authority': authority,
            },
        }
        snapshot = build_filmstrip_snapshot(
            envelope, client=client, project=canonical_project,
            run_id=capture_id, video_digest=capture_identity,
        )
        snapshot['config'] = deepcopy(config)
        snapshot['registry'] = deepcopy(registry)
        snapshot['metadata']['selection'] = 'composed_frame_capture'
        snapshot['metadata']['capture_authority'] = deepcopy(authority)
        snapshot['metadata']['capture_identity'] = capture_identity
        snapshot['metadata']['requested_revision_id'] = requested_revision or parent_revision
        if isinstance(closure.get('working_copy'), Mapping):
            # Provenance: these frames are of an unpublished working copy, built on this base.
            snapshot['metadata']['working_copy'] = deepcopy(dict(closure['working_copy']))
        media_attachment = None
        if inputs.get('include_media'):
            media_run = str(exact) if exact else matching_composed_render(
                client,
                project_id=str(project_id),
                timeline={
                    'project_id': project_id,
                    'project_slug': canonical_project,
                    'timeline_id': authority['timeline_id'],
                    'parent_revision_id': parent_revision,
                },
                limit=50,
            )
            if not media_run:
                _fail(
                    'include_media requires an exact managed rendered video for the selected saved revision; '
                    'none is available to attach.'
                )
            media_inputs = dict(inputs)
            media_inputs.pop('composed_capture', None)
            media_inputs['render_run'] = str(media_run)
            media_inputs['include_media'] = False
            media_authority = prepare_filmstrip(
                media_inputs,
                project=project,
                client=client,
                _composed_media_attachment=True,
            )
            media_snapshot = media_authority.get('filmstrip_snapshot')
            media_canonical = (
                media_snapshot.get('metadata', {}).get('canonical_timeline')
                if isinstance(media_snapshot, Mapping)
                else None
            )
            media_revision = _identifier(
                media_canonical or {}, 'parent_revision_id', 'head_revision_id', 'revision_id'
            )
            if media_revision != str(parent_revision):
                _fail('include_media render does not match the saved revision selected for composed frames.')
            media_attachment = {
                'render_run_id': media_authority.get('render_run_id'),
                'video_digest': media_authority.get('video_digest'),
                'video_object_id': media_authority.get('video_object_id'),
                'managed_object_reference': media_authority.get('managed_object_reference'),
            }
            if not all(isinstance(media_attachment.get(key), str) for key in (
                'video_digest', 'video_object_id', 'managed_object_reference',
            )):
                _fail('include_media render has no admitted managed video object to attach.')
        result = {
            'mode': 'filmstrip' if media_attachment else 'composed_capture',
            'project_id': project_id,
            'timeline_id': authority['timeline_id'],
            'timeline_slug': _identifier(capture_row, 'slug', 'timeline_id', 'id'),
            'revision_id': requested_revision or parent_revision,
            'capture_snapshot': snapshot,
            'capture_identity': capture_identity,
            'frame_source': 'composed_capture' if media_attachment else None,
            'component_request': components,
            'include_media': bool(inputs.get('include_media', False)),
        }
        if media_attachment:
            result.update(media_attachment)
        return result
    if exact:
        candidates = [client.get_run(exact)]
    else:
        candidates = paged_rows(client.list_project_runs, project_id, limit=50) or []
        candidates = sorted(candidates, key=lambda r: (str(r.get('created_at', '')), _identifier(r, 'id', 'run_id')), reverse=True)
    selected = None
    observed: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for candidate in candidates:
        run = client.get_run(_identifier(candidate, 'id', 'run_id'))
        if _identifier(run, 'project_id', 'project') != project_id:
            if exact:
                _status_failure(render_status(
                    lifecycle=_state(run), output={"run_id": _identifier(run, "id", "run_id"), "timeline": selector},
                    owner_ok=False, project=project,
                ))
            observed.append((run, {}))
            continue
        if _render_capability(run) != 'rendering.render':
            continue
        lifecycle = _state(run)
        tasks = [client.get_task(t) for t in run.get('task_ids', [])]
        render_tasks = [t for t in tasks if _render_capability(t) == 'rendering.render']
        task_state = _state(render_tasks[0]) if len(render_tasks) == 1 else lifecycle
        observed.append((run, render_tasks[0] if len(render_tasks) == 1 else {}))
        # Scope a latest-run status to the selected canonical timeline before
        # classifying lifecycle.  A pending render for another timeline in the
        # same project must not mask this timeline's successful render.
        candidate_authority = None
        candidate_envelope = None
        if len(render_tasks) == 1:
            try:
                candidate_envelope = _envelope(render_tasks[0])
                candidate_authority = _authority(candidate_envelope)
            except CapabilityValidationError:
                candidate_envelope = None
        if not exact and _is_candidate_render(candidate_envelope or {}, candidate_authority):
            # Candidate previews are valid explicit inspection targets, but
            # never participate in implicit ``latest`` canonical selection.
            continue
        if timeline_row and candidate_authority is not None and candidate_authority.get('timeline_id') != _identifier(timeline_row, 'timeline_id', 'id'):
            continue
        if lifecycle not in _SUCCESS_STATES or task_state not in _SUCCESS_STATES:
            # ``--render-run`` pins the input authority even when production
            # failed. This is the render-free recovery lane; it must not be
            # mistaken for an implicit retry or a successful output.
            if exact and 'output' not in components['resolved'] and len(render_tasks) == 1:
                try:
                    envelope = candidate_envelope or _envelope(render_tasks[0])
                    authority = candidate_authority or _authority(envelope)
                    if timeline_row is None or authority.get('timeline_id') == _identifier(timeline_row, 'timeline_id', 'id'):
                        selected = (run, render_tasks[0], envelope, authority)
                        break
                except CapabilityValidationError:
                    pass
            # An exact run is a direct status query; latest ignores unrelated
            # non-terminal candidates but still reports a useful state when no
            # successful candidate exists.
            if exact or (timeline_row is not None and lifecycle in {'pending', 'queued', 'running', 'admitted', 'starting', 'in_progress', 'failed', 'error', 'cancelled', 'canceled'}):
                record = {'run_id': _identifier(run, 'id', 'run_id'), 'task_id': _identifier(render_tasks[0], 'id', 'task_id') if render_tasks else None, 'timeline': selector}
                _status_failure(render_status(lifecycle=task_state, output=record, project=project))
            continue
        tasks = [t for t in render_tasks if _state(t) in _SUCCESS_STATES]
        if len(tasks) != 1:
            continue
        task = tasks[0]; envelope = candidate_envelope or _envelope(task)
        authority = candidate_authority or _authority(envelope)
        if timeline_row and authority.get('timeline_id') != _identifier(timeline_row, 'timeline_id', 'id'):
            continue
        selected = (run, task, envelope, authority)
        break
    if selected is None:
        _status_failure(render_status(lifecycle="absent", output={"timeline": selector}, project=project))
    run, task, envelope, authority = selected
    if 'output' not in components['resolved']:
        timeline_snapshot = _timeline_snapshot(envelope)
        config = timeline_snapshot.get('config', {})
        registry = timeline_snapshot.get('registry', {})
        if not isinstance(config, Mapping) or not isinstance(registry, Mapping):
            _fail('Render has no immutable canonical input snapshot.')
        config, registry = _expand_input_snapshot(client, config, registry, authority)
        from astrid.core.timeline.duration import timeline_duration_frames
        canvas = config.get('theme_overrides', {}).get('visual', {}).get('canvas', {})
        fps = canvas.get('fps', 30) if isinstance(canvas, Mapping) else 30
        try:
            fps_fraction = Fraction(str(fps))
            duration_frames = timeline_duration_frames(config, float(fps_fraction))
        except (TypeError, ValueError):
            fps_fraction = Fraction(30, 1)
            duration_frames = 0
        return {
            'mode': 'input_only', 'project_id': project_id,
            'timeline_id': authority.get('timeline_id'),
            'timeline_slug': selector or authority.get('timeline_slug') or authority.get('timeline_id'),
            'render_run_id': _identifier(run, 'id', 'run_id'),
            'component_request': components,
            'input_snapshot': input_snapshot,
        }
    if not exact:
        pins = [{'timeline_id': authority['timeline_id'], 'config_version': authority.get('config_version')}]
        pins += authority.get('expansion', {}).get('children', [])
        current_rows = paged_rows(client.list_timelines, project_id, limit=50) or []
        for pin in pins:
            current = next((row for row in current_rows if isinstance(row, Mapping) and str(row.get("timeline_id")) == str(pin.get("timeline_id"))), None)
            # Some transports expose the authoritative row only through
            # get_timeline, while others include it in list_timelines. Merge
            # both read surfaces before comparing the frozen version/head.
            live_reader = getattr(client, "get_timeline", None)
            if callable(live_reader):
                try:
                    live = live_reader(str(pin.get("timeline_id")))
                except Exception:
                    live = None
                if isinstance(live, Mapping):
                    current = {**(dict(current) if isinstance(current, Mapping) else {}), **dict(live)}
            current_version = current.get("config_version") if isinstance(current, Mapping) else None
            pinned_version = pin.get("config_version")
            if pinned_version is not None and current_version is not None and str(current_version) != str(pinned_version):
                _status_failure(render_status(
                    lifecycle="succeeded",
                    output={"available": True, "run_id": _identifier(run, "id", "run_id"), "timeline": selector},
                    fresh=False, project=project,
                ))
            current_head = current.get("head_revision_id") if isinstance(current, Mapping) else None
            pinned_head = pin.get("revision_id") or pin.get("head_revision_id")
            if pinned_head and current_head and str(current_head) != str(pinned_head):
                _status_failure(render_status(
                    lifecycle="succeeded",
                    output={"available": True, "run_id": _identifier(run, "id", "run_id"), "timeline": selector},
                    fresh=False, project=project,
                ))
        # Script-only edits do not necessarily advance timeline versions.
        for shot in authority.get('expansion', {}).get('shots', []):
            for binding in shot.get('text_bindings', []):
                current = client.get_project_shot_text_binding(project_id, binding['binding_id'])
                if current.get('head') != binding.get('head') or current.get('content_hash') != binding.get('content_hash'):
                    _status_failure(render_status(
                        lifecycle="succeeded",
                        output={"available": True, "run_id": _identifier(run, "id", "run_id"), "timeline": selector},
                        fresh=False, project=project,
                    ))
    managed_object_reference = None
    if _composed_media_attachment:
        result = task.get('result')
        raw_outputs = result.get('outputs') if isinstance(result, Mapping) else None
        if not isinstance(raw_outputs, list) and isinstance(result, Mapping):
            raw_outputs = result.get('output_objects')
        videos = [
            output for output in (raw_outputs or [])
            if isinstance(output, Mapping)
            and _identifier(output, 'output_port', 'port', 'name') == 'video'
        ]
        if len(videos) != 1:
            _fail('include_media render must publish exactly one canonical video output.')
        output = videos[0]
        from .project_render import (
            _association_needs_managed_lookup,
            _find_publication_association,
            _lookup_managed_output,
            _normalize_digest,
            _publication_metadata,
        )
        try:
            association = _find_publication_association(task=task, run=run, output=output)
            if association is None or _association_needs_managed_lookup(association):
                association = _lookup_managed_output(
                    client=client,
                    project_id=project_id,
                    run_id=_identifier(run, 'id', 'run_id'),
                    task_id=_identifier(task, 'task_id', 'id'),
                    output=output,
                    association=association,
                )
        except Exception as exc:
            _fail(f'include_media render has no admitted managed video association: {exc}')
        if association is None:
            _fail('include_media render has no admitted managed video association.')
        metadata, metadata_error = _publication_metadata(
            association=association, output=output, task=task, run=run,
        )
        if metadata_error:
            _fail(f'include_media render has no admitted managed video association: {metadata_error}')
        selected_run_id = _identifier(run, 'id', 'run_id')
        selected_task_id = _identifier(task, 'task_id', 'id')
        association_project_id = _identifier(association, 'project_id')
        if association_project_id and association_project_id != str(project_id):
            _fail('include_media managed video association belongs to a different project.')
        if (
            not selected_run_id
            or not selected_task_id
            or metadata.get('run_id') != selected_run_id
            or metadata.get('task_id') != selected_task_id
        ):
            _fail('include_media managed video association does not belong to the selected render run and task.')
        digest = _digest(metadata.get('digest'))
        managed_object_reference = metadata.get('managed_object_reference')
        objects = paged_rows(client.list_project_objects, project_id, limit=50) or []
        digest_hex = _normalize_digest(digest)
        owned = False
        for item in objects:
            if not isinstance(item, Mapping):
                continue
            item_reference = _identifier(item, 'object_id', 'id')
            raw_item_digest = item.get('digest')
            item_digest = _normalize_digest(raw_item_digest)
            if item_digest == digest_hex:
                owned = True
                break
            if (
                item_reference in {managed_object_reference, digest}
                and raw_item_digest in (None, '')
            ):
                owned = True
                break
        if not owned:
            _status_failure(render_status(
                lifecycle='succeeded',
                output={"available": True, "digest": digest, "run_id": _identifier(run, "id", "run_id"), "timeline": selector},
                owner_ok=False,
                project=project,
            ))
    else:
        outputs = task.get('result', {}).get('outputs', [])
        videos = [o for o in outputs if o.get('name') == 'video']
        if len(videos) != 1:
            _status_failure(render_status(
                lifecycle="succeeded", output={"available": False, "run_id": _identifier(run, "id", "run_id"), "timeline": selector}, project=project,
            ))
        digest = _digest(videos[0].get('digest') or videos[0].get('object_id'))
        objects = paged_rows(client.list_project_objects, project_id, limit=50) or []
        if not any(digest in {o.get('object_id'), o.get('digest')} for o in objects):
            _status_failure(render_status(
                lifecycle="succeeded", output={"available": True, "digest": digest, "run_id": _identifier(run, "id", "run_id"), "timeline": selector}, owner_ok=False, project=project,
            ))
    run_id = _identifier(run, 'id', 'run_id')
    snapshot = build_filmstrip_snapshot(envelope, client=client, project=canonical_project, run_id=run_id, video_digest=digest)
    selection = 'latest_current_render'
    if exact and not inputs.get('auto_selected_render'):
        selection = _explicit_render_selection(envelope, authority, timeline_row)
    snapshot['metadata']['selection'] = selection
    from .managed_transcript import transcript_input_from_snapshot
    timeline_snapshot = _timeline_snapshot(envelope)
    config = timeline_snapshot.get('config', {})
    registry = timeline_snapshot.get('registry', {})
    try:
        transcript_input = transcript_input_from_snapshot(config, registry)
    except ValueError as exc:
        _fail(str(exc))
    result = {'mode': 'filmstrip', 'filmstrip_snapshot': snapshot,
        'video_object_id': digest, 'video_digest': digest, 'render_run_id': run_id,
        'project_id': project_id, 'timeline_id': authority['timeline_id'],
        'include_media': bool(inputs.get('include_media', False)),
        'transcript_input': transcript_input}
    if _composed_media_attachment:
        result['video_object_id'] = managed_object_reference
        result['managed_object_reference'] = managed_object_reference
    return result


def latest_render(client: Any, *, project_id: str, timeline_id: str, scan: int = 12) -> dict[str, Any] | None:
    """The newest successful ``rendering.render`` run of one timeline: ``{run_id, revision, created_at,
    candidate}`` (bounded: the last ``scan`` render runs), or None. Read-only; used by ``timelines status``."""
    remote = getattr(client, "_remote", client)
    client = getattr(remote, "_transport", remote)
    list_runs = getattr(client, "list_project_runs", None)
    if not callable(list_runs):
        return None
    rows: list[Mapping[str, Any]] = []
    cursor = None
    for _page in range(20):  # the list is oldest first: read every page (bounded), then take the newest
        try:
            page_value = list_runs(project_id, cursor=cursor, limit=50)
            if hasattr(page_value, "ok") and hasattr(page_value, "data"):
                page_value = page_value.data if page_value.ok else None
            page = page_pair(page_value) if page_value is not None else None
        except Exception:  # noqa: BLE001 - status must never fail on this
            return None
        if page is None:
            break
        rows += [r for r in page[0] if isinstance(r, Mapping) and _render_capability(r) == "rendering.render"
                 and _state(r) in _SUCCESS_STATES]
        cursor = page[1]
        if not cursor:
            break
    rows.sort(key=lambda r: str(r.get("created_at") or r.get("updated_at") or ""), reverse=True)
    for listed in rows[:scan]:
        try:
            run = client.get_run(_identifier(listed, "run_id", "id"))
            for task_id in (run.get("task_ids") or [])[:20]:
                task = client.get_task(str(task_id))
                if not (isinstance(task, Mapping) and _render_capability(task) == "rendering.render"):
                    continue
                envelope = _envelope(task)
                authority = _authority(envelope)
                if _identifier(authority, "timeline_id") != timeline_id:
                    break
                return {"run_id": _identifier(run, "run_id", "id"), "created_at": run.get("created_at"),
                        "revision": _identifier(authority, "parent_revision_id", "head_revision_id", "revision_id", "head_event_id"),
                        "candidate": _is_candidate_render(envelope, authority)}
        except Exception:  # noqa: BLE001
            continue
    return None
