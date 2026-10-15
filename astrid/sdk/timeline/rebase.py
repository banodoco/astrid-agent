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
    # Local guidance, kept outside the shared conflict wire vocabulary.
    next_step: str | None = None


def _identities(call):
    identities = set(call.resolved) | set(call.created or ()) | set((call.bindings or {}).values())
    for key in (*call.expect, *(call.authorship or {})):
        if "::" in key:
            identities.add(key.split("::", 1)[0])
    for anchor in tuple(identities):
        if anchor.startswith("cut:") and anchor.endswith((":start", ":end")):
            identities.add(anchor.rsplit(":", 1)[0])
    if call.op == "timeline.inverse":
        for write in call.args["writes"]:
            identities.add(write["id"])
            identities.update(write.get("order", []))
            for name in ("before", "after"):
                state = write[name]
                if state.get("state") != "present":
                    continue
                if write["path"] in {"intent.cut", "app.astrid.cut"} and isinstance(state["value"], str):
                    identities.add("cut:" + state["value"])
                if not write["path"] and isinstance(state["value"], dict):
                    cut = state["value"].get("app", {}).get("astrid", {}).get("cut")
                    if cut:
                        identities.add("cut:" + cut)
    return identities


def _mapping(calls, supplied):
    """Admit only used, injective mappings in the existing ScopedId vocabulary."""
    identities = set().union(*(_identities(call) for call in calls))
    used = {sid: supplied[sid] for sid in sorted(identities) if sid in supplied and supplied[sid] != sid}
    destinations = {}
    for sid in sorted(identities):
        target = used.get(sid, sid)
        if sid in used:
            if (not isinstance(target, str) or not commands.is_scoped_id(sid)
                    or not commands.is_scoped_id(target)
                    or sid.rsplit("/", 1)[-1].split(":", 1)[0]
                    != target.rsplit("/", 1)[-1].split(":", 1)[0]):
                raise commands.CommandError(f"Invalid identity mapping {sid!r} -> {target!r}; supply a ScopedId of the same entity kind")
        if target in destinations and destinations[target] != sid:
            raise commands.SemanticConflict("dependent", sid, "id_map", sid, target,
                                            "identity mapping collides with another recorded identity")
        destinations[target] = sid
    return used


def _next_step(conflict):
    if conflict["kind"] == "same-property":
        return "Inspect base, theirs and ours for the named property; choose a value and record a new command on this head."
    if conflict["kind"] == "delete-vs-edit":
        return "Restore the named target, or explicitly discard or retarget the pending edit and record it on this head."
    if conflict["path"] in {"id_map", "created"}:
        return "Allocate distinct identities with the existing fork/add producer and retry with its exact id_map."
    if conflict["path"] == "bindings":
        return "Select an unambiguous surviving anchor and record a new command on this head."
    return "Resolve the reported dependency or blocking finding, then record the intended command on this head before retrying."


def _rewrite(call, id_map):
    """Rewrite only identities actually present in this record, never opaque text."""
    identities = _identities(call)
    def identity(value):
        if value not in identities:
            return value
        if value.startswith("cut:") and value.endswith((":start", ":end")):
            cut, edge = value.rsplit(":", 1)
            return id_map.get(cut, cut) + ":" + edge
        return id_map.get(value, value)
    def moment_text(text):
        from astrid.core.timeline import moments as mo
        def visit(moment):
            cut = "cut:" + moment.cut if moment.cut else None
            return replace(moment, cut=id_map.get(cut, cut)[4:] if cut else None,
                           base=visit(moment.base) if moment.base else None)
        original = mo.parse(text)
        mapped = visit(original)
        return mo.format_moment(mapped) if mapped != original else text
    def time_ref(text):
        anchor = (call.bindings or {}).get(text)
        return moment_text(text) if anchor and identity(anchor) != anchor else text
    def state(path, value, *, sid=None):
        out = copy.deepcopy(value)
        if out.get("state") != "present":
            return out
        prop = path.split("::", 1)[-1]
        if prop in {"intent.on", "intent.until", "app.astrid.on", "app.astrid.until"} and isinstance(out["value"], str):
            out["value"] = moment_text(out["value"])
        elif prop in {"intent.cut", "app.astrid.cut"} and isinstance(out["value"], str):
            cut = "cut:" + out["value"]
            out["value"] = id_map.get(cut, cut)[4:]
        elif not prop and sid and isinstance(out["value"], dict) and "/" in sid:
            out["value"]["id"] = identity(sid).rsplit(":", 1)[1]
            intent = out["value"].get("app", {}).get("astrid", {})
            if intent.get("cut"):
                cut = "cut:" + intent["cut"]
                intent["cut"] = id_map.get(cut, cut)[4:]
            for key in ("on", "until"):
                if isinstance(intent.get(key), str):
                    intent[key] = moment_text(intent[key])
        return out
    def path(key):
        if "::" not in key:
            return key
        sid, prop = key.split("::", 1)
        return f"{identity(sid)}::{prop}"
    rewritten = replace(call, resolved=tuple(identity(sid) for sid in call.resolved),
                        created=tuple(identity(sid) for sid in call.created or ()),
                        bindings={time_ref(text): identity(sid) for text, sid in (call.bindings or {}).items()},
                        expect={path(key): state(key.split("::", 1)[-1], value,
                            sid=commands.write_target(key, call.resolved)[0]) for key, value in call.expect.items()},
                        authorship={path(key): value for key, value in (call.authorship or {}).items()},
                        args={key: time_ref(value) if isinstance(value, str) and key in
                              {"moment", "on", "until", "at", "when", "before", "land"}
                              else copy.deepcopy(value) for key, value in call.args.items()})
    if call.op == "timeline.add" and rewritten.created:
        clips = [sid for sid in rewritten.created if "/clip:" in sid]
        if len(clips) == 1:
            rewritten = replace(rewritten, args={**rewritten.args, "clip_id": clips[0].split("/clip:", 1)[1]})
    if call.op in {"cut.split", "cut.duplicate"} and rewritten.args.get("id"):
        cut = "cut:" + rewritten.args["id"]
        rewritten = replace(rewritten, args={**rewritten.args, "id": id_map.get(cut, cut)[4:]})
    if call.op == "timeline.inverse":
        rewritten = replace(rewritten, args={**rewritten.args, "writes": [
            {**write, "id": identity(write["id"]),
             "before": state(write["path"], write["before"], sid=write["id"]),
             "after": state(write["path"], write["after"], sid=write["id"]),
             **({"order": [identity(sid) for sid in write["order"]]} if "order" in write else {})}
            for write in call.args["writes"]]})
    return commands.EditCommand.coerce(rewritten)


def rebase(calls, onto, *, id_map=None):
    """Replay recorded calls through the runner, preserving guards and anchor IDs.

    The map is supplied by a real identity producer; ordinary occurrence edits
    retain their scoped identities and return an empty map. No document merge or
    selector fallback participates. Both inputs are detached and remain unchanged.
    """
    try:
        recorded = tuple(commands.EditCommand.coerce(value) for value in calls)
        for call in recorded:
            if any(getattr(call, field) is None for field in ("expect", "bindings", "created", "authorship")):
                raise commands.CommandError("Rebase requires a runner-recorded OpCall, including original preconditions")
        used = _mapping(recorded, copy.deepcopy(dict(id_map or {})))
        tl = Checkout(copy.deepcopy(onto))
        require_writable_candidate(tl.document())
        with edit_transaction(tl, "Rebase commands", quiet=True, persist=False) as tx:
            tx.canonical = True
            for call in recorded:
                rewritten = _rewrite(call, used)
                states = commands.entity_states(tl)
                collisions = [sid for sid in rewritten.created if sid in states]
                if collisions:
                    raise commands.SemanticConflict("dependent", collisions[0], "created", None,
                                                    collisions, "created identity already exists")
                commands.execute(tl, rewritten)
        if tx.plan.blocking or (tx.report is not None and not tx.report.valid):
            raise commands.CommandError("; ".join(tx.plan.blocking or tx.report.problems))
        return RebaseResult(tl.document(), tx.plan, used)
    except commands.SemanticConflict as exc:
        return RebaseResult(id_map={}, conflicts=(exc.conflict,), next_step=_next_step(exc.conflict))
    except TimelineEditError as exc:
        return RebaseResult(id_map={}, conflicts=(dict(kind="dependent", id="", path="",
                            base=None, theirs=None, ours=str(exc)),),
                            next_step="Resolve the reported dependency or ambiguity, then record the intended command on this head before retrying.")
