"""Pure helpers for durable refused timeline changes."""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone


def _suffix_options(state, commands):
    """Keep commit controls for a queued suffix, but partition its own commands."""
    options = copy.deepcopy(state.get("commit_options") or {})
    steps = options.get("steps")
    if steps is not None:
        label = options.get("label") or "Timeline edit"
        patch = [copy.deepcopy(change) for command in commands
                 if command.get("op") == "doc.patch"
                 for change in command.get("args", {}).get("changes", [])]
        options["steps"] = [{"id": "edit", "label": label, "intent": "", "tags": [],
                             "command_ordinals": list(range(len(commands))), "patch": patch}]
    return options


def snapshot(state, flight, failure):
    body = json.loads(flight["body"])
    options = {key: copy.deepcopy(body[key]) for key in ("label", "steps", "merge", "reads") if key in body}
    base = {"revision_id": body["base_revision_id"], "publication_id": body["base_publication_id"]}
    evaluation = copy.deepcopy(flight.get("evaluation", state.get("evaluation")))
    part = {"base": base, "commands": copy.deepcopy(body["commands"]),
            "commit_options": options, "rationale": copy.deepcopy(body.get("rationale")),
            "group_id": copy.deepcopy(body.get("group_id")),
            "media_input": copy.deepcopy(flight.get("media_input")),
            "imports": copy.deepcopy(flight.get("imports", [])), "evaluation": evaluation}
    parts = [part]
    suffix = state["pending"][flight["count"]:]
    if suffix:
        parts.append({"base": copy.deepcopy(state["base"]), "commands": copy.deepcopy(suffix),
                      "commit_options": _suffix_options(state, suffix),
                      "rationale": copy.deepcopy(state.get("rationale")),
                      "media_input": copy.deepcopy(state.get("media_input")),
                      "imports": copy.deepcopy(state.get("media_imports", [])),
                      "evaluation": copy.deepcopy(state.get("evaluation"))})
    logical_key = flight.get("logical_key")
    if logical_key is None and flight.get("explicit_key"):
        logical_key = flight["key"]
    generated = logical_key is None
    now = datetime.now(timezone.utc).isoformat()
    details = failure.get("details") or {}
    return {"id": "pc_" + uuid.uuid4().hex[:12], "state": "pending", "rev": 2,
            "key": logical_key or "clash-" + uuid.uuid4().hex[:12], "key_generated": generated,
            "label": body.get("label") or "Timeline edit",
            "origin": "function" if isinstance(evaluation, dict) and evaluation.get("kind") == "function" else "list",
            "parts": parts, "part": 0, "flight": copy.deepcopy(flight), "failure": copy.deepcopy(failure),
            "head": copy.deepcopy(details.get("current")), "diff": None, "attempts": 1,
            "receipts": [], "closed_by": None, "versions": [],
            "created_at": now, "updated_at": now, "dropped_reason": None}


def migrate(state):
    changed = False
    records = state.get("pending_changes")
    if isinstance(records, dict):
        for record in records.values():
            if not isinstance(record, dict):
                continue
            legacy = {"choices", "candidate", "flight_fence"} & record.keys()
            missing = {"attempts", "closed_by", "diff"} - record.keys()
            invalid_state = record.get("state") == "committing"
            if legacy or missing or invalid_state:
                for key in legacy:
                    record.pop(key, None)
                record.setdefault("attempts", 1)
                record.setdefault("closed_by", None)
                record.setdefault("diff", None)
                if invalid_state:
                    record["state"] = "pending"
                record["rev"] = int(record.get("rev", 0)) + 1
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                changed = True
    rejected = state.get("rejected")
    if not isinstance(rejected, dict) or rejected.get("failure", {}).get("details", {}).get("status") != "conflict":
        return changed
    saved = {**state, **{key: copy.deepcopy(rejected[key]) for key in
                        ("base", "commit_options", "evaluation") if key in rejected},
             "pending": copy.deepcopy(rejected["commands"])}
    record = snapshot(saved, rejected["flight"], rejected["failure"])
    state.setdefault("pending_changes", {})[record["id"]] = record
    state.pop("rejected")
    return True


def clash(record):
    details = record["failure"].get("details") or {}
    return {"pending_id": record["id"], "label": record["label"], "key": record["key"],
            "origin": record["origin"], "base": copy.deepcopy(record["parts"][0]["base"]),
            "head": copy.deepcopy(record["head"]),
            "runtime": {"reason": copy.deepcopy(details.get("reason")),
                        "conflicts": copy.deepcopy(details.get("conflicts", []))},
            "next": (f'tl.retry("{record["id"]}", your_function)' if record["origin"] == "function"
                     else f'tl.retry("{record["id"]}")')}
