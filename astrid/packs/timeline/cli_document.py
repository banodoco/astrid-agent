"""Detached timeline document commands, composed from the public SDK."""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import Path

from astrid.core.cli.argtypes import input_file, output_file
from astrid.core.cli.domain_output import DomainResult, ErrorObject, print_result
from astrid.core.cli.registration import CommandSpec, register_commands
from astrid.sdk.authoring_bundle import (
    diff_authoring_candidate,
    import_authoring_media,
    plan_authoring_media,
    publish_authoring_candidate,
)
from astrid.sdk.autobootstrap import ensure_runtime
from astrid.sdk.timeline_editing import add_authoring_shot
from astrid.sdk.workspace_client import WorkspaceClient, resolve_runtime_connection


def typed(result):
    if not result.ok:
        raise RuntimeError(str(result.error))
    return result.data


def data(response):
    return response.get("data", response)


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def candidate(args):
    value = json.loads(args.file.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("document must be a JSON object")
    return value


def workspace():
    # Public launcher receipt; no private discovery/CAS/database reads.
    receipt = ensure_runtime(start_pack_host=False)
    endpoint, token = resolve_runtime_connection(
        receipt["endpoint"], Path(receipt["credential_file"])
    )
    return WorkspaceClient(endpoint, token)


class Writer:
    def __init__(self, transport):
        self.transport = transport

    def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
        return data(self.transport.publish_parent_composition(
            project_id, timeline_id, publication, idempotency_key=idempotency_key
        ))


def checkout(args):
    if args.file.exists():
        raise FileExistsError(f"Refusing to overwrite {args.file}; choose a new checkout file")
    project = typed(args.client.projects.show(args.project))
    project_id = project.get("project_id") or project["id"]
    opened = typed(args.client.timelines.open_bundle(project_id, args.timeline))
    if opened["project_id"] != project_id:
        raise RuntimeError("Canonical timeline inspection did not confirm the requested project")
    if not opened["is_current_head"] or opened["revision_id"] != opened["head_revision_id"]:
        raise RuntimeError("Canonical timeline inspection omitted a consistent current head")
    work = opened["bundle"]
    dump(args.file, work)
    return {"file": str(args.file), "head": opened["revision_id"], "placements": len(work["placements"])}


def add_shot(args):
    work = candidate(args)
    occurrence = args.occurrence_id or f"occurrence-{uuid.uuid4().hex[:12]}"
    shot = add_authoring_shot(
        work, name=args.name, shot_id=args.shot_id, occurrence_id=occurrence, start_ms=args.start_ms
    )
    shot_id = next(key for key, value in work["shots"].items() if value is shot)
    if args.duration_ms is not None:
        for row in work["placements"]:
            if row["occurrence_id"] == occurrence:
                row["duration_ms"] = args.duration_ms
    dump(args.file, work)
    return {"shot_id": shot_id, "placements": len(work["placements"]), "document": work,
            "next": "add tracks and clips to document['shots'][shot_id]['internal_timeline'], then check and publish"}


def bind_script(args):
    work = candidate(args)
    shot = work["shots"][args.shot]
    narration = args.text_file.read_text(encoding="utf-8")
    metadata = shot["payload"].get("metadata", {})
    if "voiceover_script" in metadata and metadata["voiceover_script"] != narration:
        raise RuntimeError("Duplicate metadata narration conflicts; reconcile explicitly before registering")
    for binding in shot["payload"].get("text_bindings", []):
        if binding.get("kind") == "voiceover_script" and "text" in binding and binding["text"] != narration:
            raise RuntimeError("Embedded narration conflicts; reconcile explicitly before registering")
    # New shots must first be published without text so they are registered.
    shown = typed(args.client.timelines.open_composition(work["project_id"], work["timeline_id"]))
    if shown["summary"]["head_revision_id"] != work["base_parent"]["revision_id"]:
        raise RuntimeError("Stale checkout: reopen before registering narration")
    pin = typed(args.client.shots.set_text_binding(
        work["project_id"], shot_id=args.shot, kind="voiceover_script", text=narration,
        expected_head=args.expected_head, idempotency_key=args.idempotency_key,
    ))
    shot["payload"]["text_bindings"] = [
        binding for binding in shot["payload"].get("text_bindings", [])
        if binding.get("kind") != "voiceover_script"
    ] + [pin]
    metadata.pop("voiceover_script", None)
    dump(args.file, work)
    return {"binding": pin, "file": str(args.file), "document": work,
            "next": "check then publish to pin this binding"}


def check(args):
    plan = plan_authoring_media(candidate(args), base_directory=args.file.parent)
    return {"validation": plan["validation"], "diff": diff_authoring_candidate(plan["candidate"]),
            "media_imports": plan["imports"]}


def publish(args):
    work = candidate(args)
    plan = plan_authoring_media(work, base_directory=args.file.parent)
    typed(args.client.timelines.open_composition(work["project_id"], work["timeline_id"]))
    # Runtime evaluates accepted-request replay before the publication's expected head.
    transport = workspace()
    imported = import_authoring_media(plan, transport)
    # A failed parent CAS can retry without the original local media files.
    dump(args.file, imported["candidate"])
    try:
        receipt = publish_authoring_candidate(
            imported["candidate"], Writer(transport), idempotency_key=args.idempotency_key,
            media_imports=imported["imports"],
        )
    except RuntimeError as exc:
        exc.details = {**getattr(exc, "details", {}), "document": imported["candidate"],
                       "media_imports": imported["imports"]}
        raise
    receipt["media_imports"] = imported["imports"]
    receipt["document"] = imported["candidate"]
    check_action = {
        "command": shlex.join(["python3", "-m", "astrid", "timelines", "document", "check", "--file", str(args.file)]),
        "scope": "candidate_preflight",
        "description": "Check the document's validation, property differences and media import plan.",
    }
    receipt["next_actions"].append(check_action)
    receipt["update"] += "\n" + check_action["description"] + "\n" + check_action["command"]
    return receipt


def _human(result):
    value = result.data
    if "update" in value:
        return value["update"].splitlines()[0] + "\nnext: " + value["next_actions"][0]["command"]
    if "validation" in value:
        return (f"Document valid. Property differences: {value['diff']['change_count']}; "
                f"media imports planned: {len(value['media_imports'])}. Use --json for details.")
    if "binding" in value:
        return f"Pinned narration in {value['file']}. Check then publish."
    if "shot_id" in value:
        return f"Added shot {value['shot_id']}; {value['placements']} placements. Check then publish."
    return f"Checked out {value['head']} to {value['file']}; {value['placements']} placements."


def _run(args):
    try:
        result = DomainResult.success(args.document_handler(args))
    except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
        result = DomainResult.failure(ErrorObject(
            getattr(exc, "code", "validation_error"), str(exc), getattr(exc, "details", {})
        ))
    return print_result(result, as_json=args.json, human_renderer=_human)


def configure_document(parser):
    commands = parser.add_subparsers(dest="document_command", required=True)
    for name, handler, help_text in (
        ("checkout", checkout, "Check out the complete pinned composition as an editable JSON document."),
        ("add-shot", add_shot, "Add and place a new empty shot in a document, editing the file in place."),
        ("bind-script", bind_script, "Register narration and pin its descriptor in a document, editing the file in place."),
        ("check", check, "Validate a document and report its diff and media import plan."),
        ("publish", publish, "Import media, rewrite the document with managed digests, and publish one timeline revision."),
    ):
        def configure(subparser, name=name, handler=handler):
            subparser.add_argument("--file", type=output_file if name == "checkout" else input_file, required=True)
            subparser.add_argument("--json", action="store_true", help="Print the product JSON envelope.")
            if name == "checkout":
                subparser.add_argument("--project", required=True, help="Runtime project slug or ID")
                subparser.add_argument("--timeline", required=True, help="Runtime timeline slug or ID")
            if name == "add-shot":
                subparser.add_argument("--name")
                subparser.add_argument("--shot-id")
                subparser.add_argument("--occurrence-id")
                subparser.add_argument("--start-ms", type=float, default=0)
                subparser.add_argument("--duration-ms", type=float)
            if name == "bind-script":
                subparser.add_argument("--shot", required=True)
                subparser.add_argument("--text-file", type=input_file, required=True)
                subparser.add_argument("--expected-head", type=int, required=True)
            if name in {"bind-script", "publish"}:
                subparser.add_argument("--idempotency-key", required=True)
            subparser.set_defaults(handler=_run, document_handler=handler)
        register_commands(commands, (CommandSpec(name, help=help_text, configure=configure),))
