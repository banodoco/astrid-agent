"""User and project preference shortcuts over runtime Markdown documents."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from astrid.core.cli.domain_documents import _render
from astrid.core.cli.domain_output import DomainResult, ErrorObject, print_result
from astrid.core.cli.registration import CommandSpec, register_commands, register_product_commands


def _render_preferences(result, as_json):
    if as_json or not result.ok:
        return print_result(result, as_json=as_json)
    data = result.data
    if "precedence" in data:
        print(data["precedence"])
        for scope in ("user", "project"):
            row = data[scope]
            print(f"\n{scope.capitalize()} preferences" + (f" ({row['project_id']})" if row and scope == "project" else ""))
            print(row["content"] or "(empty)" if row else "(no project selected)")
    else:
        print(f"{data['scope'].capitalize()} preferences")
        print(data["content"] or "(empty)")
    return 0


def _view(args):
    result = args.client.preferences.get(args.scope, project=args.project) if getattr(args, "scope", None) else args.client.preferences.view(project=args.project)
    return _render_preferences(result, args.json)


def _common(parser):
    parser.add_argument("--project", help="Project ID or slug; defaults to the runtime selection.")
    parser.add_argument("--json", action="store_true", help="Print the five-key SDK envelope.")


def _configure_user(parser):
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_view, scope="user", project=None)


def _configure_project(parser):
    _common(parser)
    parser.set_defaults(handler=_view, scope="project")


def _configure_context(parser):
    _common(parser)
    parser.set_defaults(handler=_view, scope=None)


def _checkout(args):
    return _render(args.client.preferences.checkout(scope=args.scope, file=args.file, project=args.project), as_json=args.json)


def _checkin(args):
    return _render(args.client.preferences.checkin(args.file), as_json=args.json)


def _edit(args):
    return _render(args.client.preferences.edit(scope=args.scope, project=args.project), as_json=args.json)


_DEFAULT_SLOTS = {
    "image": ("image/t2i",),
    "image/t2i": ("image/t2i",),
    "edit": ("image/edit",),
    "image/edit": ("image/edit",),
    "video": ("video/t2v", "video/i2v"),
    "video/t2v": ("video/t2v",),
    "video/i2v": ("video/i2v",),
    "music": ("audio/music",),
    "audio/music": ("audio/music",),
    "voice": ("audio/tts",),
    "audio/tts": ("audio/tts",),
    "foley": ("audio/sfx",),
    "audio/sfx": ("audio/sfx",),
}


def _default_error(exc: Exception):
    code = "validation_error"
    message = str(exc) or type(exc).__name__
    if "unavailable" in message.lower() or "runtime" in message.lower():
        code = "service_unavailable"
    return DomainResult.failure(ErrorObject(code, message, {}))


def _default_render(result, as_json: bool):
    if as_json or not result.ok:
        return print_result(result, as_json=as_json)
    data = result.data
    if (isinstance(data, dict) and isinstance(data.get("slots"), list)
            and all(isinstance(row, dict) for row in data["slots"])):
        for row in data["slots"]:
            print(f"{row['slot']}: {row.get('selected') or '(unset)'} ({row.get('source', 'none')})")
        if data.get("library"):
            print("ComfyUI workflows: " + ", ".join(data["library"]))
        return 0
    if isinstance(data, dict) and data.get("message"):
        print(data["message"])
        if data.get("warning"):
            print(f"Warning: {data['warning']}")
        return 0
    return print_result(result)


def _slot_keys(slot: str) -> tuple[str, ...]:
    try:
        return _DEFAULT_SLOTS[slot]
    except KeyError as exc:
        raise ValueError(f"unknown defaults slot {slot!r}; choose image, edit, video, music, voice, or foley") from exc


def _entry_support(entry_id: str) -> tuple[str, set[str], list[dict[str, Any]]]:
    from astrid.core.generation import entries
    from astrid.core.model_catalog.registry import ModelRegistry

    # find_entry is the shared ambiguity authority for workflow/action entries.
    workflow = entries.find_entry(entry_id)
    if workflow is not None:
        kind = workflow.get("kind")
        modes = set(workflow.get("modes", []))
        details = entries.describe_entry(entry_id)
        input_rows: list[dict[str, Any]] = []
        for mode_row in details.get("mode_descriptions", {}).values():
            input_rows.extend(mode_row.get("inputs", []))
        input_rows.extend(details.get("inputs", []))
        return kind, modes, input_rows

    registry = ModelRegistry.load_default()
    try:
        model = registry.get(entry_id)
    except KeyError as exc:
        raise ValueError(f"unknown local generation entry {entry_id!r}") from exc
    local_modes = {mode for mode, spec in model.modes.items() if spec.backends.get("local")}
    if not local_modes:
        raise ValueError(f"entry {entry_id!r} is hosted-only; hosted runs are a one-off choice")
    return model.modality, local_modes, []


def _validate_default_entry(slot: str, entry_id: str) -> tuple[str, ...]:
    kind, modes, input_rows = _entry_support(entry_id)
    keys = _slot_keys(slot)
    slot_kind, _ = keys[0].split("/", 1)
    if kind != slot_kind:
        raise ValueError(f"entry {entry_id!r} has kind {kind!r}; {slot!r} requires {slot_kind!r}")
    supported = tuple(key for key in keys if key.split("/", 1)[1] in modes)
    if not supported:
        raise ValueError(f"entry {entry_id!r} does not support the mode required by {slot!r}")
    if slot in {"foley", "audio/sfx"}:
        has_video = any(
            "video" in str(row.get("name", "")).lower()
            or "video" in str(row.get("type", "")).lower()
            or "video" in str(row.get("file_custody", "")).lower()
            for row in input_rows
        )
        if not has_video:
            raise ValueError(f"entry {entry_id!r} has no video input and cannot be a foley default")
    return supported


def _generation_inventory():
    from astrid.core.generation import entries
    from astrid.core.model_catalog.registry import ModelRegistry

    rows = []
    for model in ModelRegistry.load_default().list_all(include_closed=True):
        for mode, spec in model.modes.items():
            backend = spec.backends.get("local")
            if backend:
                rows.append({"id": model.id, "kind": model.modality, "mode": mode,
                             "backend": "local", "template": backend.template,
                             "source": "catalog", "registry_id": model.id})
    for entry in [*entries.user_entries(), *entries.ready_entries()]:
        source = entry.get("source", "user-entry")
        rows.extend({"id": entry["id"], "kind": entry["kind"], "mode": mode,
                     "backend": "local", "source": source, "entry": entry,
                     "registry_id": entry.get("registry_id"),
                     "reason": entry.get("ambiguity", "")}
                    for mode in entry.get("modes", []))
    return rows


def _library_names(target_client) -> list[str]:
    record = getattr(target_client, "generation_target", None)
    root = getattr(record, "root", None)
    if not root:
        return []
    workflows = Path(root) / "user" / "default" / "workflows"
    try:
        return sorted(path.name for path in workflows.iterdir() if path.is_file())
    except OSError:
        return []


def _show_defaults(args):
    from astrid.core.generation import defaults
    from astrid.core.generation.listing import _local_readiness
    from astrid.core.model_catalog.registry import ModelRegistry
    from astrid.core.generation.preflight import select_generation_target

    try:
        configured, _version = defaults.load(args.client)
        target = select_generation_target(args.target)
        target_schema = None
        if target is not None:
            try:
                target_schema = target.object_info()
            except Exception:
                target_schema = None
        target_record = getattr(target, "target", None)
        registry = ModelRegistry.load_default()
        inventory = _generation_inventory()
        slots = []
        keys = _slot_keys(args.slot) if args.slot else tuple(defaults.BUILTINS) + ("audio/music", "audio/tts", "audio/sfx")
        for key in keys:
            kind, mode = key.split("/", 1)
            rows = defaults.candidates(target, kind=kind, mode=mode,
                                       configured=configured, entries=inventory)
            builtin = list(defaults.BUILTINS.get(key, ()))
            candidates = []
            for candidate in rows:
                entry_id = candidate["entry"]
                entry = candidate.get("candidate")
                ready_info = (_local_readiness(entry, registry, target_record, target_schema)
                              if entry is not None else
                              {"ready": False, "reason": f"entry {entry_id!r} is not available on this machine",
                               "fix": "Import the local generation entry again."})
                ready = ready_info.get("ready") is True
                fixes = []
                if not ready and candidate.get("origin") == "user":
                    fixes.append({"argv": ["astrid", "preferences", "defaults", "reset", key],
                                  "scope": "read_only"})
                candidates.append({"entry": entry_id, "origin": candidate["origin"], "ready": ready})
                if candidate["origin"] == "user":
                    readiness = {"entry": entry_id, "target": getattr(target_record, "url", None),
                                 "status": "ready" if ready else "blocked",
                                 "issues": [] if ready else [{"code": "missing_dependency",
                                                              "message": ready_info.get("reason", "entry is not ready"),
                                                              "fix": ready_info.get("fix", "")}],
                                 "fixes": fixes}
            chosen = candidates[0] if candidates else None
            source = chosen["origin"] if chosen else "none"
            slots.append({"slot": key, "kind": kind, "mode": mode,
                          "configured": configured["slots"].get(key), "builtin": builtin,
                          "selected": chosen["entry"] if chosen else None,
                          "source": source, "candidates": candidates,
                          "readiness": readiness if configured["slots"].get(key) else
                          {"entry": chosen["entry"] if chosen else None,
                           "status": "ready" if chosen and chosen["ready"] else "blocked" if chosen else "none",
                           "issues": [], "fixes": []}})
            readiness = None
        data = {"target": getattr(getattr(target, "target", None), "url", None),
                "library": _library_names(target), "slots": slots}
        return _default_render(DomainResult.success(data), args.json)
    except Exception as exc:  # runtime/target failures use the same stable command envelope
        return _default_render(_default_error(exc), args.json)


def _set_defaults(args):
    from astrid.core.generation import defaults

    try:
        slots = _validate_default_entry(args.slot, args.entry)
        updated = []
        for key in slots:
            defaults.save(args.client, key, args.entry)
            updated.append(key)
        warning = None
        try:
            from astrid.core.generation.availability import entry_availability
            from astrid.core.generation import entries
            entry = entries.find_entry(args.entry)
            if entry is not None and any(not entry_availability(entry, key.split("/", 1)[1]).get("available", True) for key in updated):
                warning = "saved, but the entry is not ready on the local target"
        except Exception:
            pass
        return _default_render(DomainResult.success({"message": "Saved defaults: " + ", ".join(updated),
                                                     "slots": updated, "entry": args.entry,
                                                     "warning": warning}), args.json)
    except Exception as exc:
        return _default_render(_default_error(exc), args.json)


def _reset_defaults(args):
    from astrid.core.generation import defaults

    try:
        slots = _slot_keys(args.slot)
        for key in slots:
            defaults.save(args.client, key, None)
        return _default_render(DomainResult.success({"message": "Reset defaults: " + ", ".join(slots),
                                                     "slots": list(slots)}), args.json)
    except Exception as exc:
        return _default_render(_default_error(exc), args.json)


def _configure_defaults_show(parser):
    parser.add_argument("slot", nargs="?", help="Optional slot or slot/mode.")
    parser.add_argument("--target", default="auto", help="Resolved ComfyUI target.")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_show_defaults)


def _configure_defaults_set(parser):
    parser.add_argument("slot", help="Slot or slot/mode.")
    parser.add_argument("entry", help="Local generation entry ID.")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_set_defaults)


def _configure_defaults_reset(parser):
    parser.add_argument("slot", help="Slot or slot/mode.")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_reset_defaults)


def _configure_defaults(parser):
    subparsers = parser.add_subparsers(dest="defaults_command", required=True)
    register_commands(subparsers, (
        CommandSpec("show", help="Show built-in and saved local generation defaults.", configure=_configure_defaults_show),
        CommandSpec("set", help="Save a local generation default for the user.", configure=_configure_defaults_set),
        CommandSpec("reset", help="Remove a saved override and return to built-ins.", configure=_configure_defaults_reset),
    ))


def _configure_edit(parser):
    _common(parser)
    parser.add_argument("--scope", choices=("user", "project"), required=True)
    parser.set_defaults(handler=_edit)


def _configure_checkout(parser):
    _configure_edit(parser)
    parser.add_argument("--file", type=Path, required=True, help="New Markdown working-copy path.")
    parser.set_defaults(handler=_checkout)


def _configure_checkin(parser):
    parser.add_argument("file", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_checkin)


COMMANDS = (
    CommandSpec("defaults", help="Show or change local generation defaults.", configure=_configure_defaults),
    CommandSpec("user", help="Read your lasting user preferences.", configure=_configure_user),
    CommandSpec("project", help="Read current or specified project overrides.", configure=_configure_project),
    CommandSpec("context", help="Read labeled user/project preferences and precedence.", configure=_configure_context),
    CommandSpec("checkout", help="Fetch Markdown preferences for editing.", configure=_configure_checkout),
    CommandSpec("checkin", help="Save an edited checkout; conflicts preserve your file.", configure=_configure_checkin),
    CommandSpec("edit", help="Checkout, open $EDITOR, then safely check in changes.", configure=_configure_edit),
)


def build_parser(client: Any):
    parser = argparse.ArgumentParser(prog="astrid preferences", description="Markdown defaults: current request > project > user > defaults. Temporary requests stay in conversation. Persist only when asked.")
    _common(parser)
    parser.set_defaults(handler=_view, client=client, scope=None)
    register_product_commands(parser.add_subparsers(dest="command"), COMMANDS, family="preferences", client=client)
    return parser
