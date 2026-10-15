"""Command replay onto an exact detached head; the sole semantic rebase path."""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Any

from astrid.core.timeline.compatibility import require_writable_candidate
from astrid.sdk.timeline import commands
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.transaction import edit_transaction


@dataclass(frozen=True)
class RebaseResult:
    document: Any = None
    plan: Any = None
    id_map: Any = None
    conflicts: tuple = ()


def _rewrite(call, id_map):
    """Rewrite only identities actually present in this record, never opaque text."""
    identities = set(call.resolved) | set(call.created or ()) | set((call.bindings or {}).values())
    def identity(value):
        if value not in identities:
            return value
        if value in id_map:
            return id_map[value]
        if value.startswith("cut:") and value.endswith((":start", ":end")):
            cut, edge = value.rsplit(":", 1)
            return id_map.get(cut, cut) + ":" + edge
        return value
    def mapped_moment_text(text):
        from astrid.core.timeline import moments as mo
        def rewrite_moment(moment):
            cut = "cut:" + moment.cut if moment.cut else None
            return replace(moment, cut=id_map.get(cut, cut)[4:] if cut else None,
                           base=rewrite_moment(moment.base) if moment.base else None)
        original = mo.parse(text)
        rewritten = rewrite_moment(original)
        return mo.format_moment(rewritten) if rewritten != original else text
    def time_ref(text):
        # Only recorded bindings prove that an argument is an authored TimeRef.
        # Parse the existing grammar; never replace arbitrary text or opaque IDs.
        anchor = (call.bindings or {}).get(text)
        return mapped_moment_text(text) if anchor and identity(anchor) != anchor else text
    def expectation(key, state):
        value = copy.deepcopy(state)
        prop = key.split("::", 1)[-1]
        if (prop in {"intent.on", "intent.until", "app.astrid.on", "app.astrid.until"}
                and value.get("state") == "present" and isinstance(value.get("value"), str)):
            value["value"] = mapped_moment_text(value["value"])
        return value
    def path(key):
        if "::" not in key:
            return key
        sid, prop = key.split("::", 1)
        return f"{identity(sid)}::{prop}"
    rewritten = replace(call, resolved=tuple(identity(sid) for sid in call.resolved),
                        created=tuple(identity(sid) for sid in call.created or ()),
                        bindings={time_ref(text): identity(sid) for text, sid in (call.bindings or {}).items()},
                        expect={path(key): expectation(key, value) for key, value in call.expect.items()},
                        authorship={path(key): value for key, value in (call.authorship or {}).items()},
                        args={key: time_ref(value) if isinstance(value, str) and key in
                              {"moment", "on", "until", "at", "when", "before", "land"}
                              else copy.deepcopy(value) for key, value in call.args.items()})
    if call.op == "timeline.add" and rewritten.created:
        clips = [sid for sid in rewritten.created if "/clip:" in sid]
        if len(clips) == 1:
            rewritten = replace(rewritten, args={**rewritten.args, "clip_id": clips[0].split("/clip:", 1)[1]})
    if call.op in {"cut.split", "cut.duplicate"} and rewritten.args.get("id"):
        old = "cut:" + str(rewritten.args["id"])
        rewritten = replace(rewritten, args={**rewritten.args, "id": id_map.get(old, old)[4:]})
    return commands.EditCommand.coerce(rewritten)


def rebase(calls, onto, *, id_map=None):
    """Replay recorded calls through the runner, preserving guards and anchor IDs.

    Ordinary occurrence edits retain their scoped identities. Created identities
    that collide are forked here and returned in the existing id_map; callers
    may also supply a previously established map. No document merge or
    selector fallback participates. Both inputs are detached and remain unchanged.
    """
    mapping = copy.deepcopy(dict(id_map or {}))
    used = {}
    tl = Checkout(copy.deepcopy(onto))
    try:
        require_writable_candidate(tl.document())
        with edit_transaction(tl, "Rebase commands", quiet=True, persist=False) as tx:
            tx.canonical = True
            for value in calls:
                call = commands.EditCommand.coerce(value)
                if any(getattr(call, field) is None for field in ("expect", "bindings", "created", "authorship")):
                    raise commands.CommandError("Rebase requires a runner-recorded OpCall, including original preconditions")
                identities = set(call.resolved) | set(call.created or ()) | set((call.bindings or {}).values())
                used.update((sid, mapping[sid]) for sid in identities if sid in mapping and mapping[sid] != sid)
                rewritten = _rewrite(call, used)
                # A created identity colliding with head content (or a retired
                # cut reservation) is a fork, never an edit of that head entity.
                from astrid.sdk.timeline.identity import cut_parts, cut_reservations, next_cut_id
                states = commands.entity_states(tl)
                mark = cut_reservations(tl.bundle)
                for original, sid in zip(call.created or (), rewritten.created or ()):
                    if sid.startswith("cut:"):
                        number, suffix = cut_parts(sid[4:])
                        reserved = (suffix <= mark["suffixes"].get(str(number), 0) if suffix
                                    else number <= mark["number"])
                        if reserved:
                            used[original] = "cut:" + next_cut_id(tl.bundle, sid[4:])
                    elif "/clip:" in sid and sid in states:
                        prefix, local = sid.split("/clip:", 1)
                        used[original] = prefix + "/clip:" + tl._new_id(local)
                commands.execute(tl, _rewrite(call, used))
        if tx.plan.blocking or (tx.report is not None and not tx.report.valid):
            raise commands.CommandError("; ".join(tx.plan.blocking or tx.report.problems))
        return RebaseResult(tl.document(), tx.plan, used)
    except commands.SemanticConflict as exc:
        return RebaseResult(id_map={}, conflicts=(exc.conflict,))
    except TimelineEditError as exc:
        return RebaseResult(id_map={}, conflicts=(dict(kind="dependent", id="", path="",
                            base=None, theirs=None, ours=str(exc)),))
