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
        return id_map.get(value, value) if value in identities else value
    def path(key):
        if "::" not in key:
            return key
        sid, prop = key.split("::", 1)
        return f"{identity(sid)}::{prop}"
    rewritten = replace(call, resolved=tuple(identity(sid) for sid in call.resolved),
                        created=tuple(identity(sid) for sid in call.created or ()),
                        bindings={text: identity(sid) for text, sid in (call.bindings or {}).items()},
                        expect={path(key): copy.deepcopy(value) for key, value in call.expect.items()},
                        authorship={path(key): value for key, value in (call.authorship or {}).items()},
                        args=copy.deepcopy(dict(call.args)))
    if call.op == "timeline.add" and rewritten.created:
        clips = [sid for sid in rewritten.created if "/clip:" in sid]
        if len(clips) == 1:
            rewritten = replace(rewritten, args={**rewritten.args, "clip_id": clips[0].split("/clip:", 1)[1]})
    if call.op == "timeline.inverse":
        rewritten = replace(rewritten, args={**rewritten.args, "writes": [
            {**write, "id": identity(write["id"])} for write in rewritten.args["writes"]]})
    return commands.EditCommand.coerce(rewritten)


def rebase(calls, onto, *, id_map=None):
    """Replay recorded calls through the runner, preserving guards and anchor IDs.

    The map is supplied by a real identity producer; ordinary occurrence edits
    retain their scoped identities and return an empty map. No document merge or
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
                commands.execute(tl, _rewrite(call, used))
        if tx.plan.blocking or (tx.report is not None and not tx.report.valid):
            raise commands.CommandError("; ".join(tx.plan.blocking or tx.report.problems))
        return RebaseResult(tl.document(), tx.plan, used)
    except commands.SemanticConflict as exc:
        return RebaseResult(id_map={}, conflicts=(exc.conflict,))
    except TimelineEditError as exc:
        return RebaseResult(id_map={}, conflicts=(dict(kind="dependent", id="", path="",
                            base=None, theirs=None, ours=str(exc)),))
