"""Append-only working-copy records with a verifiable hash chain."""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import getpass
import hashlib
import json
import os
import shlex
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from runtime_protocol import platform

from astrid.sdk.timeline.doc import _strip_derived

VERSION = 1
_CAP = 200
_ZERO = None


@dataclass(frozen=True)
class JournalRecord:
    v: int
    seq: int
    id: str
    at: str
    project: str
    timeline: str
    draft: str | None
    actor: Mapping[str, Any]
    source: Mapping[str, Any]
    kind: str
    outcome: str
    label: str
    calls: tuple = ()
    before: str | None = None
    after: str | None = None
    clips: tuple[str, ...] = ()
    cuts: tuple[str, ...] = ()
    reason: str | None = None
    undo_step: str | None = None
    evidence: tuple = ()
    error: str | None = None
    prev: str | None = None
    hash: str = ""

    @property
    def sequence(self):
        return self.seq

    @property
    def previous(self):
        return self.prev

    @property
    def digest(self):
        return self.hash

    @property
    def payload(self):
        return {key: value for key, value in self.to_json().items() if key not in {"v", "seq", "id", "at", "prev", "hash"}}

    def to_json(self) -> dict[str, Any]:
        return {field.name: _plain(getattr(self, field.name)) for field in dataclasses.fields(self)}


def _plain(value: Any) -> Any:
    if hasattr(value, "to_json") and not isinstance(value, Mapping):
        return _plain(value.to_json())
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _from_json(value: Mapping[str, Any]) -> JournalRecord:
    kwargs = {field.name: value[field.name] for field in dataclasses.fields(JournalRecord) if field.name in value}
    for name in ("calls", "clips", "cuts", "evidence"):
        if name in kwargs:
            kwargs[name] = tuple(kwargs[name] or ())
    for name in ("actor", "source"):
        if name in kwargs:
            kwargs[name] = dict(kwargs[name] or {})
    return JournalRecord(**kwargs)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(previous: str | None, body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(((previous or "") + _canonical(body)).encode("utf-8")).hexdigest()


def document_digest(document: Any) -> str:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def authored_digest(bundle):
    return document_digest({"parent": bundle["parent"], "placements": bundle["placements"],
        "shots": {key: {field: row[field] for field in ("payload", "internal_timeline")}
                  for key, row in bundle["shots"].items()}})


def begin_nudge(clip, seconds, frames):
    """Observe the helper's literal and retiming inputs before mutation."""
    from astrid.sdk import timeline_intent as intent

    from ._doc_patch import Closure, value_at
    from .checkout import Checkout

    tl = clip._tl
    if (not tl.bundle.get("base_parent", {}).get("publication_id")
            or "_runtime_document" not in tl.bundle.get("source_mapping", {})
            or "structure_base" in tl.bundle or clip.shot_id not in tl.bundle.get("shots", {})
            or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or isinstance(frames, bool) or not isinstance(frames, int)):
        return None
    before = copy.deepcopy(tl.bundle)
    original = copy.deepcopy(before)
    original["parent"] = copy.deepcopy(original["base_parent_payload"])
    for shot in original["shots"].values():
        shot["payload"] = copy.deepcopy(shot["base_payload"])
        shot["internal_timeline"] = copy.deepcopy(shot["base_internal_timeline"])
    calls = _saved_helper_calls(tl, original)
    if calls is None:
        return None
    calls.extend(copy.deepcopy(tl._helper_calls))
    boundary = original
    if calls:
        from .commands import execute

        # Use the existing provider projection of preceding commands. Compiling
        # the detached preview alone gives a different successor ownership.
        boundary = execute(original, [command for call in calls for command in call["commands"]]).bundle
    # Checkout migrates legacy intent on a private copy. Authenticate that
    # view, but capture reads and patch preconditions from the exact source.
    if authored_digest(Checkout(copy.deepcopy(boundary)).document()) != authored_digest(before):
        return None
    before = boundary
    closure = Closure(before)
    refs = [ref for ref, owners in closure.owners.items()
            if ref.startswith("internal-revision:")
            and any(local == clip.shot_id for _, local in owners)]
    rows = [row for row in before["placements"] if row["shot_id"] == clip.shot_id]
    if len(refs) != 1 or len(rows) != 1:
        return None
    ref = refs[0]
    path = ["clips", "id:" + clip.id]
    # Consume the whole original row as a finite literal, so a peer's change
    # to this helper's selected clip cannot silently become its new input.
    state = value_at(closure.payload(ref), ref, path)
    json.dumps(state, allow_nan=False)
    reads = [{"kind": "field", "ref": ref, "path": path, "state": state}]
    sites = [("parent", ["config", "theme_overrides"]),
             ("parent", ["occurrences", "occurrence_id:" + rows[0]["occurrence_id"], "placement", "start_ms"]),
             ("parent", ["occurrences", "occurrence_id:" + rows[0]["occurrence_id"], "at_ms"])]
    for root, tokens in sites:
        reads.append({"kind": "field", "ref": root, "path": tokens,
                      "state": value_at(closure.payload(root), root, tokens)})
    if intent.on(clip.data) or intent.cut_of(clip.data):
        reads.extend(_retime_reads(closure))
    return {"op": "nudge", "shot": clip.shot_id, "clip": clip.id,
            "seconds": seconds, "frames": frames, "reads": reads, "_before": before,
            "base": {key: before["base_parent"][key] for key in ("revision_id", "publication_id")}}


def _retime_reads(closure):
    """Conservative inputs of on/retime, including unchanged consulted clips."""
    from ._doc_patch import identities, value_at

    reads = []

    def field(ref, payload, path):
        state = value_at(payload, ref, path)
        json.dumps(state, allow_nan=False)
        reads.append({"kind": "field", "ref": ref, "path": path, "state": state})

    def collection(ref, payload, path, key):
        state = value_at(payload, ref, path)
        if state["state"] == "absent":
            field(ref, payload, path)
            return []
        rows = state["value"]
        order = identities(rows, key, ref=ref, path=path)
        reads.append({"kind": "collection", "ref": ref, "path": path,
                      "members": sorted(order), "order": order})
        return rows

    for ref in closure.images:
        payload = closure.payload(ref)
        if ref.startswith("shot-revision:"):
            for key in ("name", "metadata"):
                field(ref, payload, [key])
            continue
        if ref == "parent":
            field(ref, payload, ["config"])
            for row in collection(ref, payload, ["occurrences"], "occurrence_id"):
                path = ["occurrences", "occurrence_id:" + row["occurrence_id"]]
                for key in ("placement", "at_ms", "duration_ms"):
                    field(ref, payload, [*path, key])
        for key in ("tracks", "registry"):
            field(ref, payload, [key])
        for row in collection(ref, payload, ["clips"], "id"):
            path = ["clips", "id:" + row["id"]]
            for key in ("at", "hold", "from", "to", "duration", "speed", "track",
                        "clipType", "type", "kind", "asset", "app"):
                field(ref, payload, [*path, key])
            # Picture diagnostics consult deliberate flags, not a peer's text
            # or visual parameters on another cut.
            if isinstance(row.get("params"), dict):
                for key in ("app", "deliberate", "deliberate_hold"):
                    field(ref, payload, [*path, "params", key])
            else:
                field(ref, payload, [*path, "params"])
    return reads


def finish_nudge(tl, capture):
    from .emitter import emit_document_commands

    before = capture.pop("_before")
    if before["placements"] != tl.bundle["placements"]:
        return  # Placement-changing helpers retain ordinary atomic publication.
    after = copy.deepcopy(before)
    after["parent"] = copy.deepcopy(tl.bundle["parent"])
    for local, shot in tl.bundle["shots"].items():
        for field in ("payload", "internal_timeline"):
            after["shots"][local][field] = copy.deepcopy(shot[field])
    if authored_digest(before) == authored_digest(after):
        return
    capture.update(before=authored_digest(before), after=authored_digest(after),
                   commands=emit_document_commands(before, after))
    tl._helper_calls.append(capture)


def helper_batch(tl, base, candidate):
    """Replay untrusted journal claims; only a complete original chain is bounded."""
    from ._command_state import equal
    from ._common import TimelineEditError
    from .checkout import Checkout

    calls = _saved_helper_calls(tl, base)
    if calls is None:
        return None
    calls.extend(copy.deepcopy(tl._helper_calls))
    if not calls:
        return None
    replay = Checkout(copy.deepcopy(base))
    commands, steps = [], []
    for call in calls:
        if call.get("before") != authored_digest(base if not steps else replay.document()):
            return None  # an uncaptured edit occurred between helper operations
        if call.get("op") != "nudge":
            raise TimelineEditError("unknown helper capture")
        selected = [clip for clip in replay.clips() if clip.id == call.get("clip")
                    and clip.shot_id == call.get("shot")]
        if len(selected) != 1:
            raise TimelineEditError("helper capture has no unique original clip")
        selected[0].nudge(call["seconds"], frames=call["frames"])
        if len(replay._helper_calls) != len(steps) + 1 or not equal(replay._helper_calls[-1], call):
            raise TimelineEditError("helper capture differs from its original operation")
        start = len(commands)
        commands.extend(copy.deepcopy(call["commands"]))
        steps.append({"id": f"helper-{len(steps)}", "label": "Nudge " + call["clip"],
            "intent": "nudge", "tags": [], "command_ordinals": list(range(start, len(commands))),
            "patch": [copy.deepcopy(change) for command in call["commands"]
                      for change in command["args"]["changes"]], "reads": copy.deepcopy(call["reads"])})
    if authored_digest(replay.document()) != authored_digest(candidate):
        return None  # final direct edits cannot inherit the helper's bounded reads
    return commands, steps


def _saved_helper_calls(tl, base):
    from ._common import TimelineEditError

    calls = []
    if tl.path:
        path = journal_path(str(base["project_id"]), str(base["timeline_id"]))
        if verify(path):
            raise TimelineEditError("helper journal is damaged; run timelines log --verify")
        rows = [row for row in read(path) if row.draft == Path(tl.path).stem
                and row.kind == "save" and row.outcome == "changed"]
        # The authenticated base digest selects a start, never an arbitrary suffix.
        basis = {key: base["base_parent"][key] for key in ("revision_id", "publication_id")}
        starts = [i for i, row in enumerate(rows) if row.calls
                  and row.calls[0].get("base") == basis
                  and row.calls[0].get("before") == authored_digest(base)]
        if len(starts) > 1:
            raise TimelineEditError("helper journal has ambiguous original capture")
        if starts:
            for row in rows[starts[0]:]:
                if not row.calls:
                    return None  # old snapshots or a direct edit keep the atomic policy
                calls.extend(row.calls)
    return calls


def digest(text: str) -> str:
    """Digest the canonical document in a saved JSON working copy."""
    return document_digest(_strip_derived(json.loads(text)))


def one_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _drafts_root() -> Path:
    from astrid.sdk.timeline_checkout import drafts_root
    return Path(drafts_root())


def _timeline_key(project: str, timeline: str) -> str:
    return hashlib.sha256(f"{project}\0{timeline}".encode()).hexdigest()[:20]


def journal_path(project: str, timeline: str) -> Path:
    return _drafts_root() / f"{_timeline_key(str(project), str(timeline))}.journal.jsonl"


def current_actor() -> dict[str, Any]:
    session = os.environ.get("ASTRID_SESSION") or None
    raw = os.environ.get("ASTRID_ACTOR", "").strip()
    if raw:
        kind, _, ident = raw.partition(":")
        if ident and kind in {"agent", "human"}:
            return {"kind": kind, "id": ident, "session": session}
        return {"kind": "human", "id": raw, "session": session}
    try:
        user = getpass.getuser()
    except Exception:
        return {"kind": "unknown", "id": "unknown", "session": session}
    return {"kind": "human", "id": user, "session": session}


def current_source(label: str) -> dict[str, str]:
    cli = sys.modules.get("astrid.packs.timeline.cli")
    if bool(getattr(cli, "_IN_VERB", False)):
        via = "sheet" if str(label).startswith("apply ") else "cli"
        detail = shlex.join(sys.argv[1:])
    else:
        via, detail = "python", str(label)
    return {"via": via, "detail": detail[:_CAP]}


def _last_record(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[-1])
        if not isinstance(row, dict):
            raise ValueError("record is not an object")
        body = {key: value for key, value in row.items() if key != "hash"}
        if _hash(row.get("prev"), body) != row.get("hash"):
            raise ValueError("record hash does not match")
        return row
    except (OSError, ValueError, IndexError) as exc:
        raise ValueError(f"the journal {path} ends in a damaged record; run `timelines log` with --verify") from exc


def _locked(path: Path):
    """Lock appends so two working copies cannot share a sequence number."""

    class Lock:
        def __enter__(self):
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(path.with_name(f".{path.name}.lock"), "a+", encoding="utf-8")
            platform.lock_file(self.handle.fileno())
            return self

        def __exit__(self, *_):
            platform.unlock_file(self.handle.fileno())
            self.handle.close()
    return Lock()


def append(path: str | Path, payload: Mapping[str, Any] | None = None, **fields: Any) -> JournalRecord:
    """Append one record. The optional payload form preserves the early hash-chain helper API."""
    target = Path(path)
    if payload is not None:
        fields = {**dict(payload), **fields}
    with _locked(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and verify(target):
            raise ValueError(f"the journal {target} is damaged; run `timelines log` with --verify")
        last = _last_record(target)
        previous = str(last["hash"]) if last else None
        body = JournalRecord(
            v=VERSION,
            seq=int(last["seq"]) + 1 if last else 1,
            id=str(fields.pop("id", None) or uuid.uuid4().hex),
            at=str(fields.pop("at", None) or _now()),
            project=str(fields.pop("project", "")),
            timeline=str(fields.pop("timeline", "")),
            draft=fields.pop("draft", None),
            actor=dict(fields.pop("actor", current_actor()) or {}),
            source=dict(fields.pop("source", current_source(str(fields.get("label", "edit")))) or {}),
            kind=str(fields.pop("kind", "edit")),
            outcome=str(fields.pop("outcome", "changed")),
            label=str(fields.pop("label", "edit")),
            prev=previous,
            **fields,
        )
        unsigned = {key: value for key, value in body.to_json().items() if key != "hash"}
        record = dataclasses.replace(body, hash=_hash(previous, unsigned))
        data = (json.dumps(record.to_json(), ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
    return record


def record_or_note(owner: Any, path: str | Path, **fields: Any) -> JournalRecord | None:
    try:
        return record_working_copy(path, owner=owner, **fields)
    except Exception as exc:
        note_failure(owner, exc)
        return None


def note_failure(owner: Any, error: BaseException) -> None:
    bundle = getattr(owner, "bundle", {}) or {}
    timeline = str(bundle.get("timeline_id") or getattr(owner, "timeline_id", ""))
    owner.notes.append(f"journal not written: {one_line(error)}; run timelines log {timeline} --verify")


def record_working_copy(path: str | Path, *, owner: Any = None, kind: str, outcome: str, label: str,
                        before: str | None = None, after: str | None = None, **extra: Any) -> JournalRecord | None:
    target = Path(path)
    try:
        target.resolve().relative_to(_drafts_root().resolve())
    except (OSError, ValueError):
        return None
    bundle = getattr(owner, "bundle", {}) or {}
    project = str(bundle.get("project_id") or getattr(owner, "project_id", ""))
    timeline = str(bundle.get("timeline_id") or getattr(owner, "timeline_id", ""))
    if not project or not timeline:
        return None
    return append(journal_path(project, timeline), project=project, timeline=timeline,
                  draft=target.stem, actor=current_actor(), source=current_source(label), kind=kind,
                  outcome=outcome, label=label, before=before, after=after, **extra)


def read(path: str | Path) -> list[JournalRecord]:
    target = Path(path)
    if not target.is_file():
        return []
    out = []
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, Mapping):
                out.append(_from_json(value))
        except (ValueError, TypeError):
            continue
    return out


def _parse_time(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def query(records: Any, *, since: int | str | None = None, actor: str | None = None, op: str | None = None,
          cut: str | None = None, clip: str | None = None, outcome: str | None = None) -> list[JournalRecord]:
    rows = list(records)
    if since is not None:
        text = str(since).strip()
        if text.isdigit():
            rows = [row for row in rows if row.seq >= int(text)]
        else:
            try:
                moment = _parse_time(text)
            except ValueError:
                raise ValueError(f"since: {since!r} is neither a seq nor an ISO time") from None
            rows = [row for row in rows if _parse_time(row.at) >= moment]
    if actor is not None:
        rows = [row for row in rows if actor in (row.actor.get("id"), f"{row.actor.get('kind')}:{row.actor.get('id')}")]
    if op is not None:
        rows = [row for row in rows if row.label == op or any(
            (call.get("op") if isinstance(call, Mapping) else getattr(call, "op", None)) == op for call in row.calls)]
    if cut is not None:
        rows = [row for row in rows if cut in row.cuts]
    if clip is not None:
        rows = [row for row in rows if clip in row.clips]
    if outcome is not None:
        rows = [row for row in rows if row.outcome == outcome]
    return rows


def verify(path: str | Path) -> list[str]:
    target = Path(path)
    if not target.is_file():
        return []
    problems: list[str] = []
    expected, previous = 1, None
    for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not an object")
        except ValueError:
            problems.append(f"line {number}: not a JSON record")
            continue
        seq = row.get("seq")
        if seq != expected:
            problems.append(f"line {number}: seq {seq} where {expected} was expected (a record is missing or out of order)")
        if row.get("prev") != previous:
            problems.append(f"seq {seq}: prev does not match the hash of the record before it (line {number})")
        body = {key: value for key, value in row.items() if key != "hash"}
        if _hash(row.get("prev"), body) != row.get("hash"):
            problems.append(f"seq {seq}: hash does not match the record (it was changed after it was written)")
        expected = seq + 1 if isinstance(seq, int) else expected + 1
        previous = row.get("hash")
    return problems
