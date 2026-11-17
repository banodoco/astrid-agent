"""The per-shot plan: one validated object at ``payload.metadata.plan`` on a timeline's shot.

A film scene is planned shot by shot (method, inputs, status, takes). The plan lives on the
shot's own payload, so it is saved with every timeline revision, carried in ``.astrid``
archives, and read the same way by every skill:

    astrid timelines shots plan set  <shot> --project P --timeline T --set method=LM,R --set status=planned
    astrid timelines shots plan show <shot> --project P --timeline T
    astrid timelines shots plan list        --project P --timeline T
    astrid timelines visualize T --project P --preset overview --shot-plans

Writes go through the ordinary timeline commit (``Timeline.commit``): one guarded ``doc.patch``
of ``metadata.plan`` on the shot revision, so other metadata keys and the rest of the shot are
untouched and the previous plan stays in the earlier revision. There is no other storage.

Shape (``version`` 1; every other key optional; unknown keys are refused)::

    {
      "version": 1,
      "on_screen": ["Morpheus"],                 # who or what is seen
      "line": "You have two options.",           # words heard in this shot, as planned (label only;
                                                 #   the narration itself is the voiceover_script binding)
      "intent": "keep the real performance",     # what must change, in a sentence
      "method": ["LM", "R"],                     # V LC LM R M E K (the recast skill's codes)
      "check": "mouth matches the voice track",  # what to look at when judging a take
      "inputs": {"references": ["sha256:…"],     # fixed reference media ids
                 "voice": {"media": "sha256:…", "range": [5.0, 8.0]},  # seconds into that voice media
                 "masks": ["sha256:…"]},
      "status": "planned",                       # planned | in_progress | take_ready | approved
                                                 #   (take_ready/approved need a take, except a K shot)
      "takes": [{"generation": "run-…", "media": "sha256:…", "note": "…"}]
    }
"""
from __future__ import annotations

import copy
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

PLAN_KEY = "plan"
PLAN_VERSION = 1
METHOD_CODES = ("V", "LC", "LM", "R", "M", "E", "K")
STATUSES = ("planned", "in_progress", "take_ready", "approved")
FIELDS = ("version", "on_screen", "line", "intent", "method", "check", "inputs", "status", "takes")
INPUT_FIELDS = ("references", "voice", "masks")
VOICE_FIELDS = ("media", "range")
TAKE_FIELDS = ("generation", "media", "note")
# ``--set`` paths and how their text is read.
SET_PATHS = {
    "line": "text", "intent": "text", "check": "text", "status": "status",
    "method": "methods", "on_screen": "list",
    "inputs.references": "list", "inputs.masks": "list",
    "inputs.voice.media": "text", "inputs.voice.range": "range",
}

__all__ = [
    "FIELDS", "METHOD_CODES", "PLAN_KEY", "PLAN_VERSION", "STATUSES", "ShotPlanError",
    "apply_assignments", "list_plans", "merge_plan", "plan_label", "plan_rows", "read_plan",
    "resolve_shot", "set_plan", "show_plan", "validate_plan",
]


class ShotPlanError(ValueError):
    """A plan, patch, or shot reference that cannot be used, with a message saying why."""


class _ShotGone(ShotPlanError):
    """The shot left the timeline between reading it and committing its plan."""


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ShotPlanError(f"plan.{where} must be a string")
    return value


def _texts(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ShotPlanError(f"plan.{where} must be a list of non-empty strings")
    return list(value)


def _unknown(value: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    extra = sorted(str(key) for key in value if key not in allowed)
    if extra:
        raise ShotPlanError(f"unknown key(s) under {where}: {', '.join(extra)}; allowed: {', '.join(allowed)}")


def validate_plan(plan: Any) -> dict[str, Any]:
    """Return a checked copy of *plan* (``version`` filled in) or raise :class:`ShotPlanError`."""
    if not isinstance(plan, Mapping):
        raise ShotPlanError("plan must be a JSON object")
    _unknown(plan, FIELDS, "plan")
    out = copy.deepcopy(dict(plan))
    version = out.setdefault("version", PLAN_VERSION)
    if isinstance(version, bool) or version != PLAN_VERSION:
        raise ShotPlanError(f"plan.version must be {PLAN_VERSION}")
    for key in ("line", "intent", "check"):
        if key in out:
            _text(out[key], key)
    if "on_screen" in out:
        _texts(out["on_screen"], "on_screen")
    if "method" in out:
        methods = _texts(out["method"], "method")
        if not methods:
            raise ShotPlanError(f"plan.method must name at least one code ({' '.join(METHOD_CODES)}); "
                                "unset it instead of leaving it empty")
        bad = [code for code in methods if code not in METHOD_CODES]
        if bad:
            raise ShotPlanError(f"plan.method has unknown code(s) {', '.join(bad)}; use {' '.join(METHOD_CODES)}")
        if len(set(methods)) != len(methods):
            raise ShotPlanError("plan.method lists a code twice")
        if "K" in methods and len(methods) > 1:
            raise ShotPlanError("plan.method K (keep) stands alone: a kept shot has no other method")
    if "status" in out and out["status"] not in STATUSES:
        raise ShotPlanError(f"plan.status must be one of {', '.join(STATUSES)}")
    if "inputs" in out:
        inputs = out["inputs"]
        if not isinstance(inputs, Mapping):
            raise ShotPlanError("plan.inputs must be an object")
        _unknown(inputs, INPUT_FIELDS, "plan.inputs")
        for key in ("references", "masks"):
            if key in inputs:
                _texts(inputs[key], f"inputs.{key}")
        if "voice" in inputs:
            voice = inputs["voice"]
            if not isinstance(voice, Mapping):
                raise ShotPlanError("plan.inputs.voice must be an object")
            _unknown(voice, VOICE_FIELDS, "plan.inputs.voice")
            if "media" in voice:
                _text(voice["media"], "inputs.voice.media")
            if "range" in voice:
                span = voice["range"]
                if (not isinstance(span, list) or len(span) != 2
                        or not all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
                                   and t >= 0 for t in span) or span[1] <= span[0]):
                    raise ShotPlanError("plan.inputs.voice.range must be [start, end] in seconds into "
                                        "inputs.voice.media (not timeline time), with end > start >= 0")
                if not voice.get("media"):
                    raise ShotPlanError("plan.inputs.voice.range needs plan.inputs.voice.media: the range is "
                                        "seconds into that voice media, not timeline time")
    if "takes" in out:
        takes = out["takes"]
        if not isinstance(takes, list):
            raise ShotPlanError("plan.takes must be a list")
        for index, take in enumerate(takes):
            if not isinstance(take, Mapping):
                raise ShotPlanError(f"plan.takes[{index}] must be an object")
            _unknown(take, TAKE_FIELDS, f"plan.takes[{index}]")
            if not take.get("generation") and not take.get("media"):
                raise ShotPlanError(f"plan.takes[{index}] needs a generation or a media id")
            for key in TAKE_FIELDS:
                if key in take:
                    _text(take[key], f"takes[{index}].{key}")
    if (out.get("status") in ("take_ready", "approved") and not out.get("takes")
            and not (out["status"] == "approved" and out.get("method") == ["K"])):
        raise ShotPlanError(f"plan.status {out['status']} needs at least one take (--add-take); "
                            "only a K (keep) shot is approved without one")
    return out


def combine_patches(first: Mapping[str, Any] | None, second: Mapping[str, Any] | None) -> dict[str, Any]:
    """*second* laid over *first* with the plan's merge rule (``inputs`` and ``inputs.voice`` key by key)."""
    return _merged(dict(first or {}), second or {})


def _merged(plan: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    plan = copy.deepcopy(plan)
    for key, value in patch.items():
        if key == "inputs" and isinstance(value, Mapping) and isinstance(plan.get("inputs"), Mapping):
            inputs = dict(plan["inputs"])
            for name, item in value.items():
                if name == "voice" and isinstance(item, Mapping) and isinstance(inputs.get("voice"), Mapping):
                    inputs["voice"] = {**inputs["voice"], **copy.deepcopy(dict(item))}
                else:
                    inputs[name] = copy.deepcopy(item)
            plan["inputs"] = inputs
        else:
            plan[key] = copy.deepcopy(value)
    return plan


def merge_plan(current: Mapping[str, Any] | None, patch: Mapping[str, Any] | None = None, *,
               replace: bool = False, unset: Sequence[str] = (),
               add_takes: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The plan after one write: *patch* merged (top-level keys replaced, ``inputs`` and
    ``inputs.voice`` merged key by key), *unset* dotted paths removed, *add_takes* appended.
    ``replace`` starts from an empty plan instead of *current*. The result is validated."""
    if patch is not None and not isinstance(patch, Mapping):
        raise ShotPlanError("plan patch must be a JSON object")
    if patch:
        _unknown(patch, FIELDS, "plan")
    stored_problem = None if replace else plan_problem(current)
    if stored_problem is not None and not isinstance(current, Mapping):
        raise ShotPlanError(f"the stored plan is invalid ({stored_problem}); write a whole plan with --replace")
    plan = _merged({} if replace or not current else dict(current), patch or {})
    for path in unset:
        parts = str(path).split(".")
        if parts[0] not in FIELDS or parts[0] == "version":
            raise ShotPlanError(f"cannot unset {path!r}; unset one of {', '.join(f for f in FIELDS if f != 'version')}")
        node: Any = plan
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, Mapping) else None
        if isinstance(node, dict):
            node.pop(parts[-1], None)
        inputs = plan.get("inputs")
        if isinstance(inputs, dict) and isinstance(inputs.get("voice"), dict) and not inputs["voice"]:
            inputs.pop("voice")
        if isinstance(inputs, dict) and not inputs:
            plan.pop("inputs")
    if add_takes:
        plan["takes"] = list(plan.get("takes") or []) + [copy.deepcopy(dict(take)) for take in add_takes]
    try:
        return validate_plan(plan)
    except ShotPlanError as exc:
        if stored_problem is not None:
            raise ShotPlanError(f"{exc} (the stored plan was already invalid: {stored_problem}; "
                                "write a whole plan with --replace)") from exc
        raise


def apply_assignments(assignments: Sequence[str]) -> dict[str, Any]:
    """Turn ``--set path=value`` strings into a patch (``method=LM,R``, ``inputs.voice.range=5,8``)."""
    patch: dict[str, Any] = {}
    for raw in assignments:
        path, sep, value = str(raw).partition("=")
        path = path.strip()
        if not sep or path not in SET_PATHS:
            raise ShotPlanError(f"--set takes PATH=VALUE with PATH one of {', '.join(SET_PATHS)}; got {raw!r}")
        kind = SET_PATHS[path]
        if kind in ("list", "methods"):
            parsed: Any = [item.strip() for item in value.split(",") if item.strip()]
            if kind == "methods":
                parsed = [item.upper() for item in parsed]
        elif kind == "range":
            try:
                parsed = [float(item) for item in value.replace("..", ",").split(",")]
            except ValueError:
                raise ShotPlanError(f"{path} takes START,END seconds; got {value!r}") from None
        else:
            parsed = value
        node = patch
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = parsed
    return patch


def read_plan(shot: Mapping[str, Any]) -> Any:
    """The plan stored on one bundle shot (``{"payload": …}``) exactly as stored, or None.

    A stored value is returned even when it no longer validates (written by hand, or by an older
    shape); :func:`plan_problem` says what is wrong with it."""
    payload = shot.get("payload") if isinstance(shot.get("payload"), Mapping) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    plan = metadata.get(PLAN_KEY)
    return copy.deepcopy(plan if not isinstance(plan, Mapping) else dict(plan))


def plan_problem(plan: Any) -> str | None:
    """Why a stored plan is invalid, or None when it validates (or there is none)."""
    if plan is None:
        return None
    try:
        validate_plan(plan)
    except ShotPlanError as exc:
        return str(exc)
    return None


def _shot_name(shot_id: str, shot: Mapping[str, Any]) -> str:
    payload = shot.get("payload") if isinstance(shot.get("payload"), Mapping) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    return str(metadata.get("name") or metadata.get("title") or payload.get("name") or shot_id)


def resolve_shot(bundle: Mapping[str, Any], ref: str) -> str:
    """A shot id from a shot id, an occurrence id, or an exact (unique) shot name."""
    shots = bundle.get("shots") or {}
    if ref in shots:
        return ref
    for row in bundle.get("placements") or []:
        if row.get("occurrence_id") == ref:
            return str(row["shot_id"])
    named = sorted(sid for sid, shot in shots.items() if _shot_name(sid, shot) == ref)
    if len(named) == 1:
        return named[0]
    if named:
        raise ShotPlanError(f"shot name {ref!r} is ambiguous; use a shot id: {', '.join(named)}")
    names = ", ".join(f"{sid} ({_shot_name(sid, shot)})" for sid, shot in sorted(shots.items()))
    raise ShotPlanError(f"no shot {ref!r} on this timeline; shots: {names or 'none'}")


def plan_rows(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per shot in time order: placement, name, whether it has a voiceover_script, and its plan."""
    shots = bundle.get("shots") or {}
    rows, seen = [], set()
    placements = sorted(bundle.get("placements") or [],
                        key=lambda r: (float((r.get("placement") or {}).get("start_ms") or 0), str(r.get("occurrence_id"))))
    for row in placements:
        sid = str(row.get("shot_id"))
        shot = shots.get(sid) or {}
        start = float((row.get("placement") or {}).get("start_ms") or 0) / 1000.0
        bindings = (shot.get("payload") or {}).get("text_bindings") or []
        rows.append({"shot_id": sid, "occurrence_id": row.get("occurrence_id"), "name": _shot_name(sid, shot),
                     "start": start, "end": start + float(row.get("duration_ms") or 0) / 1000.0,
                     "shared": sid in seen,
                     "voiceover_script": any(b.get("kind") == "voiceover_script" for b in bindings
                                             if isinstance(b, Mapping)),
                     "plan": read_plan(shot), "invalid": plan_problem(read_plan(shot))})
        seen.add(sid)
    return rows


def plan_label(plan: Mapping[str, Any] | None, *, words: int = 8) -> str:
    """``LM+R · planned · "You have two options."``; ``no plan`` when there is none."""
    if plan is None:
        return "no plan"
    problem = plan_problem(plan)
    if problem is not None:
        return f"invalid plan ({problem}); fix with plan set --replace"
    if not plan:
        return "no plan"
    parts = ["+".join(plan.get("method") or []) or "no method", str(plan.get("status") or "no status")]
    takes = plan.get("takes") or []
    if takes:
        parts.append(f"{len(takes)} take{'s' if len(takes) != 1 else ''}")
    line = " ".join(str(plan.get("line") or "").split())
    if line:
        head = line.split(" ")
        parts.append('"' + " ".join(head[:words]) + ("…" if len(head) > words else "") + '"')
    return " · ".join(parts)


# -- client paths (CLI and SDK) ------------------------------------------------


def _failure(code: str, message: str, details: Mapping[str, Any] | None = None):
    from astrid.sdk.contracts import DomainResult, ErrorObject

    return DomainResult.failure(ErrorObject(code, message, dict(details or {})))


def _open(client: Any, project: str, timeline: str, revision_id: str | None = None) -> dict[str, Any]:
    from astrid.sdk.timeline.store import _error_text

    opened = client.timelines.open_bundle(project, timeline, **({"revision_id": revision_id} if revision_id else {}))
    if not opened.ok or not isinstance(opened.data, Mapping):
        raise ShotPlanError(f"could not open timeline {timeline!r} in {project!r}: {_error_text(opened.error)}")
    return dict(opened.data["bundle"])


def _drop_saved_edits(tl: Any, pending_id: str | None) -> list[str]:
    """Drop the saved (pending) copy of a failed plan write from the session outbox; its id, if any."""
    if tl is None or not pending_id:
        return []
    try:
        tl.pending_drop(pending_id, reason="shot plan write: re-run plan set instead of retrying")
    except Exception:  # noqa: BLE001 - best effort: the failure itself is what gets reported
        return []
    return [pending_id]


def show_plan(client: Any, project: str, timeline: str, shot: str, *, revision_id: str | None = None):
    """``DomainResult`` with ``{shot_id, name, occurrences, voiceover_script, plan}`` for one shot."""
    from astrid.sdk.contracts import DomainResult

    try:
        bundle = _open(client, project, timeline, revision_id)
        sid = resolve_shot(bundle, shot)
    except ShotPlanError as exc:
        return _failure("validation_error", str(exc), {"shot": shot})
    rows = [row for row in plan_rows(bundle) if row["shot_id"] == sid]
    shot_row = bundle["shots"][sid]
    return DomainResult.success({
        "shot_id": sid, "name": _shot_name(sid, shot_row),
        "occurrences": [{"occurrence_id": r["occurrence_id"], "start": r["start"], "end": r["end"]} for r in rows],
        "voiceover_script": any(r["voiceover_script"] for r in rows),
        "revision_id": (bundle.get("base_parent") or {}).get("revision_id"),
        "plan": read_plan(shot_row), "invalid": plan_problem(read_plan(shot_row))})


def list_plans(client: Any, project: str, timeline: str, *, revision_id: str | None = None):
    """``DomainResult`` with ``{revision_id, shots: [row…], unplanned, not_approved}`` in time order."""
    from astrid.sdk.contracts import DomainResult

    try:
        bundle = _open(client, project, timeline, revision_id)
    except ShotPlanError as exc:
        return _failure("validation_error", str(exc), {"timeline": timeline})
    rows = plan_rows(bundle)
    unique = [row for row in rows if not row["shared"]]
    return DomainResult.success({
        "revision_id": (bundle.get("base_parent") or {}).get("revision_id"),
        "shots": rows,
        "unplanned": [row["shot_id"] for row in unique if row["plan"] is None],
        "invalid": [row["shot_id"] for row in unique if row["invalid"]],
        "not_approved": [row["shot_id"] for row in unique
                         if row["plan"] is not None and not row["invalid"]
                         and row["plan"].get("status") != "approved"
                         and row["plan"].get("method") != ["K"]],
    })


def _canonical_media(client: Any, project: str, value: Any) -> Any:
    """One media id in canonical form. A full digest (bare or labelled) needs no lookup; a prefix is
    resolved in *project* by the shared resolver and refused when it names nothing or is ambiguous."""
    from astrid.sdk.media_ids import MediaIdError, resolve_media_id

    if not isinstance(value, str):
        return value  # validate_plan refuses a non-string id
    try:
        return resolve_media_id(client, project, value)
    except MediaIdError as exc:
        raise ShotPlanError(exc.message) from exc


def canonical_plan_media(client: Any, project: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of *plan* with every media id in ``inputs`` and ``takes`` in canonical ``sha256:`` form."""

    def media(value: Any) -> Any:
        return _canonical_media(client, project, value)

    out = copy.deepcopy(dict(plan))
    inputs = out.get("inputs")
    if isinstance(inputs, Mapping):
        inputs = dict(inputs)
        for key in ("references", "masks"):
            if isinstance(inputs.get(key), list):
                inputs[key] = [media(item) for item in inputs[key]]
        voice = inputs.get("voice")
        if isinstance(voice, Mapping) and isinstance(voice.get("media"), str):
            inputs["voice"] = {**voice, "media": media(voice["media"])}
        out["inputs"] = inputs
    takes = out.get("takes")
    if isinstance(takes, list):
        out["takes"] = [
            {**take, "media": media(take["media"])} if isinstance(take, Mapping) and isinstance(take.get("media"), str) else take
            for take in takes
        ]
    return out


def set_plan(client: Any, project: str, timeline: str, shot: str, *, patch: Mapping[str, Any] | None = None,
             replace: bool = False, unset: Sequence[str] = (), add_takes: Sequence[Mapping[str, Any]] = (),
             message: str | None = None, idempotency_key: str | None = None, outbox_path: Any = None):
    """Merge a change into one shot's plan and commit it as a new timeline revision.

    The change is computed against the head the commit lands on (a conflicting head re-runs it),
    validated before anything is sent, and written as ``metadata.plan`` only."""
    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.timeline import Timeline, TimelineEditError

    try:
        bundle = _open(client, project, timeline)
        sid = resolve_shot(bundle, shot)
        # Media ids are written in their canonical form, so every reader sees one spelling.
        if patch:
            patch = canonical_plan_media(client, project, patch)
        add_takes = [{**take, "media": _canonical_media(client, project, take["media"])}
                     if isinstance(take, Mapping) and isinstance(take.get("media"), str) else take
                     for take in add_takes]
        # Validate once up front so a bad patch is refused without opening a commit.
        current = read_plan(bundle["shots"][sid])
        preview = merge_plan(current, patch, replace=replace, unset=unset, add_takes=add_takes)
    except ShotPlanError as exc:
        return _failure("validation_error", str(exc), {"shot": shot})
    if preview == current:
        return DomainResult.success({"shot_id": sid, "revision_id": (bundle.get("base_parent") or {}).get("revision_id"),
                                     "label": None, "unchanged": True, "plan": preview})
    written: dict[str, Any] = {}

    def edit(t: Any) -> None:
        if sid not in (t.bundle.get("shots") or {}):
            raise _ShotGone(f"shot {sid} is no longer on timeline {timeline!r} (it changed while the plan "
                            "was being written)")
        payload = t.bundle["shots"][sid].setdefault("payload", {})
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = payload["metadata"] = {}
        plan = merge_plan(metadata.get(PLAN_KEY), patch, replace=replace, unset=unset, add_takes=add_takes)
        metadata[PLAN_KEY] = plan
        written.clear()
        written.update(plan)

    label = message or f"Plan shot {_shot_name(sid, bundle['shots'][sid])}"
    from astrid.sdk.timeline.sync import CommitConflict, CommitOutcomeUnknown

    # A key is always sent, so a write whose outcome is unknown can be confirmed by re-running it.
    key = idempotency_key or f"shot-plan-{uuid.uuid4().hex}"
    tl = None
    try:
        tl = Timeline.open(project, timeline, client=client, outbox_path=outbox_path)
        result = tl.commit(edit, label=label, key=key)
    except CommitOutcomeUnknown as exc:
        # The request was sent: it may have landed. Keep its record so the same key reconciles it.
        return _failure("outcome_unknown", (
            f"the plan write for shot {sid} was sent but its outcome is unknown: it may or may not have landed. "
            f"Re-run the same `timelines shots plan set` command with `--idempotency-key {key}` to confirm."),
            {"shot": sid, "idempotency_key": key, "cause": str(exc)})
    except (ShotPlanError, TimelineEditError) as exc:
        # A plan write is recomputed from the head every time, so a saved retry of it is never
        # wanted: drop it, and tell the user to run the same command again.
        dropped = _drop_saved_edits(tl, getattr(exc, "pending_id", None))
        again = "Nothing was committed; run the same `timelines shots plan set` command again."
        if isinstance(exc, _ShotGone):
            return _failure("conflict", f"{exc}. {again}", {"shot": sid, "dropped_saved_edits": dropped})
        if isinstance(exc, ShotPlanError):
            return _failure("validation_error", str(exc), {"shot": sid})
        if isinstance(exc, CommitConflict):
            return _failure("conflict", f"timeline {timeline!r} changed while the plan for shot {sid} was being "
                            f"written. {again}", {"shot": sid, "dropped_saved_edits": dropped})
        headline = getattr(exc, "headline", None) or str(exc)
        return _failure("commit_failed", f"{str(headline).rstrip('.')}. {again}",
                        {"shot": sid, "dropped_saved_edits": dropped})
    data = result.get("data") if isinstance(result, Mapping) else None
    revision = data.get("accepted_revision_id") if isinstance(data, Mapping) else None
    return DomainResult.success({"shot_id": sid, "revision_id": revision, "label": label,
                                 "idempotency_key": key, "plan": json.loads(json.dumps(written))})
