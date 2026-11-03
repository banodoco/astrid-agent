"""Check out, validate, or commit a detached timeline JSON document."""

import argparse
import copy
import hashlib
import json
import shlex
from pathlib import Path

from astrid.core.timeline.authoring_feedback import authoring_change_summary, publication_feedback
from astrid.sdk import AstridClient
from astrid.sdk.authoring_bundle import (
    diff_authoring_candidate,
    import_authoring_media,
    open_authoring_bundle,
    plan_authoring_media,
)
from astrid.sdk.autobootstrap import ensure_runtime
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _json
from astrid.sdk.timeline.emitter import DocumentPatchError, emit_document_commands
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_swap_media
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.workspace_client import WorkspaceClient, resolve_runtime_connection


def typed(result):
    if not result.ok:
        raise RuntimeError(str(result.error))
    return result.data


def data(response):
    return response.get("data", response)


def resolve_media_rebinds(transport, project_id, candidate, base):
    """Resolve exact old/new pins for same-alias document rebinds."""
    selections, verified = {}, []
    for shot_id, shot in candidate.get("shots", {}).items():
        before_assets = base["shots"][shot_id]["internal_timeline"].get("registry", {}).get("assets", {})
        after_assets = shot["internal_timeline"].get("registry", {}).get("assets", {})
        for alias, old_entry in before_assets.items():
            new_entry = after_assets.get(alias)
            if not isinstance(old_entry, dict) or not isinstance(new_entry, dict):
                continue
            if old_entry.get("media_id") == new_entry.get("media_id"):
                continue
            owner = next(
                "occurrence:" + row["occurrence_id"]
                for row in base["placements"]
                if row["shot_id"] == shot_id
            )

            def read_pin(entry):
                generation = entry.get("generationId") or entry.get("generation_id")
                variant = entry.get("variantId") or entry.get("variant_id")
                if not isinstance(generation, str) or not isinstance(variant, str):
                    raise RuntimeError(
                        f"{owner}/asset:{alias} lacks an exact generation/variant pin; "
                        "re-check out the document before publishing"
                    )
                selection = data(transport.get_project_media_selection(
                    project_id, {"asset": alias, "generation_id": generation, "variant_id": variant}
                ))
                if selection.get("asset") != alias or selection.get("object_id") != entry.get("media_id"):
                    raise RuntimeError(f"{owner}/asset:{alias} Runtime selection does not match the document")
                return selection

            old_media, new_media = read_pin(old_entry), read_pin(new_entry)
            selections[(owner, alias)] = {"old": old_media, "new": new_media}
            for pin in (old_media, new_media):
                value = data(transport.get_project_verified_media(project_id, pin))
                if value not in verified:
                    verified.append(value)
    return selections, verified


def emit_document_publish_commands(base, candidate, media_selections, verified_media):
    """Capture verified media swaps before emitting the remaining document patches."""
    candidate = copy.deepcopy(candidate)
    source = base.get("source_mapping", {}).get("_runtime_document")
    if source is not None:
        # Detached files do not carry the private source. Only attach the exact
        # source read by this publication session; all other provenance must match.
        candidate_source = candidate["source_mapping"].pop("_runtime_document", source)
        if _json(candidate_source) != _json(source):
            raise DocumentPatchError("document canonical source differs from the original base")
        candidate["source_mapping"]["_runtime_document"] = copy.deepcopy(source)
        publication = candidate["base_parent"].get("publication_id")
        if publication is None:
            candidate["base_parent"]["publication_id"] = base["base_parent"]["publication_id"]
    working = copy.deepcopy(base)
    commands = []
    for (owner, alias), pair in media_selections.items():
        occurrence_id = owner.removeprefix("occurrence:")
        placement = next(
            row for row in working["placements"] if row["occurrence_id"] == occurrence_id
        )
        clips = working["shots"][placement["shot_id"]]["internal_timeline"]["clips"]
        target = next(row for row in clips if row.get("asset") == alias)
        select = f"{owner}/clip:{target['id']}"
        try:
            command = capture_occurrence_swap_media(
                working, select, pair["old"], pair["new"], verified_media=verified_media
            )
            working = execute(working, [command], verified_media=verified_media).bundle
        except (CommandError, KeyError, StopIteration, TypeError) as exc:
            raise DocumentPatchError(
                f"{owner}/asset:{alias} native same-alias media rebind is held: {exc}"
            ) from exc
        commands.append(command)
    commands.extend(emit_document_commands(working, candidate))
    return commands


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def workspace():
    # Public launcher receipt; no private discovery/CAS/database reads.
    receipt = ensure_runtime(start_pack_host=False)
    endpoint, token = resolve_runtime_connection(
        receipt["endpoint"], Path(receipt["credential_file"])
    )
    return WorkspaceClient(endpoint, token)


def resolve_timeline_scope(client, project_id, timeline_ref):
    """Resolve an explicit timeline ref through canonical public inspection."""
    inspection = typed(client.timelines.open_composition(project_id, timeline_ref))
    native = inspection.get("native_inspection")
    if not isinstance(native, dict) or native.get("project_id") != project_id:
        raise RuntimeError("Canonical timeline inspection did not confirm the requested project")
    timeline_id = native.get("timeline_id")
    head = inspection.get("summary", {}).get("head_revision_id")
    if not isinstance(timeline_id, str) or not timeline_id:
        raise RuntimeError("Canonical timeline inspection omitted the timeline ID")
    if not isinstance(head, str) or not head or native.get("head_revision_id") != head:
        raise RuntimeError("Canonical timeline inspection omitted a consistent current head")
    return timeline_id, inspection


def checkout(args):
    if args.file.exists():
        raise FileExistsError(f"Refusing to overwrite {args.file}; choose a new checkout file")
    with AstridClient.open_from_launcher(start_pack_host=False) as client:
        project = typed(client.projects.show(args.project))
        project_id = project.get("project_id") or project["id"]
        timeline_id, inspection = resolve_timeline_scope(client, project_id, args.timeline)
        head = inspection["summary"]["head_revision_id"]
        history = typed(client.timelines.history(project_id, timeline_id, limit=1))
        head_pin = history.get("head", {}) if isinstance(history, dict) else {}
    transport = workspace()
    parent = data(transport.get_project_parent_composition_revision(project_id, timeline_id, head))
    shots, internals = {}, {}
    for occurrence in parent["payload"]["occurrences"]:
        shot_revision = occurrence.get("shot_revision_id") or occurrence["revision_id"]
        if shot_revision not in shots:
            shots[shot_revision] = data(
                transport.get_project_shot_revision(
                    project_id, occurrence["shot_id"], shot_revision
                )
            )
        shot = shots[shot_revision]
        internal_revision = (
            shot.get("internal_timeline_revision_id")
            or shot["payload"]["internal_timeline_revision_id"]
        )
        if internal_revision not in internals:
            internals[internal_revision] = data(
                transport.get_project_timeline_revision(project_id, timeline_id, internal_revision)
            )
    candidate = open_authoring_bundle(
        parent,
        shot_revisions=list(shots.values()),
        internal_timeline_revisions=list(internals.values()),
    )
    if head_pin.get("revision_id") == head:
        candidate["base_parent"]["publication_id"] = head_pin.get("publication_id")
    dump(args.file, candidate)
    print(
        json.dumps(
            {"file": str(args.file), "head": head, "placements": len(candidate["placements"])}
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("checkout")
    get.add_argument("--project", required=True, help="Runtime project slug or ID")
    get.add_argument("--timeline", required=True, help="Runtime timeline slug or ID")
    get.add_argument("--file", type=Path, required=True)
    bind = commands.add_parser("bind-script", help="Register narration and pin its descriptor into an existing shot checkout")
    bind.add_argument("--file", type=Path, required=True)
    bind.add_argument("--shot", required=True)
    bind.add_argument("--text-file", type=Path, required=True)
    bind.add_argument("--expected-head", type=int, required=True)
    bind.add_argument("--idempotency-key", required=True)
    for name in ("check", "commit"):
        command = commands.add_parser(name, **({"aliases": ["publish"]} if name == "commit" else {}))
        command.add_argument("--file", type=Path, required=True)
        if name == "commit":
            command.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    if args.command == "checkout":
        checkout(args)
        return
    candidate = json.loads(args.file.read_text(encoding="utf-8"))
    if args.command == "bind-script":
        shot = candidate["shots"][args.shot]
        narration = args.text_file.read_text(encoding="utf-8")
        metadata = shot["payload"].get("metadata", {})
        if "voiceover_script" in metadata and metadata["voiceover_script"] != narration:
            raise RuntimeError("Duplicate metadata narration conflicts; reconcile explicitly before registering")
        for binding in shot["payload"].get("text_bindings", []):
            if binding.get("kind") == "voiceover_script" and "text" in binding and binding["text"] != narration:
                raise RuntimeError("Embedded narration conflicts; reconcile explicitly before registering")
        # New shots must first be published without text so they are registered.
        # Text registration and composition pin publication are explicit steps.
        with AstridClient.open_from_launcher(start_pack_host=False) as client:
            shown = typed(client.timelines.open_composition(candidate["project_id"], candidate["timeline_id"]))
            if shown["summary"]["head_revision_id"] != candidate["base_parent"]["revision_id"]:
                raise RuntimeError("Stale checkout: reopen before registering narration")
            pin = typed(client.shots.set_text_binding(
                candidate["project_id"], shot_id=args.shot, kind="voiceover_script",
                text=narration, expected_head=args.expected_head,
                idempotency_key=args.idempotency_key,
            ))
        shot["payload"]["text_bindings"] = [
            binding for binding in shot["payload"].get("text_bindings", [])
            if binding.get("kind") != "voiceover_script"
        ] + [pin]
        if "voiceover_script" in metadata:
            del metadata["voiceover_script"]
        dump(args.file, candidate)
        dump(args.file.with_suffix(".text-binding.json"), pin)
        print(json.dumps({"binding": pin, "file": str(args.file), "next": "check then publish to pin this binding"}))
        return
    plan = plan_authoring_media(candidate, base_directory=args.file.parent)
    validation = plan["validation"]
    diff = diff_authoring_candidate(plan["candidate"])
    report = {"validation": validation, "diff": diff, "media_imports": plan["imports"]}
    dump(args.file.with_suffix(".check.json"), report)
    if args.command == "check":
        print(json.dumps(report, indent=2))
        return
    # Capture supported document fields against the candidate's exact pinned base.
    with AstridClient.open_from_launcher(start_pack_host=False) as client:
        transport = workspace()
        base_publication_id = candidate.get("base_parent", {}).get("publication_id")
        if not isinstance(base_publication_id, str) or not base_publication_id:
            history = typed(client.timelines.history(candidate["project_id"], candidate["timeline_id"], limit=100))
            head = history.get("head", {}) if isinstance(history, dict) else {}
            match = next((row for row in history.get("items", [])
                          if row.get("revision_id") == candidate["base_parent"]["revision_id"]), None)
            base_publication_id = (head.get("publication_id") if head.get("revision_id") == candidate["base_parent"]["revision_id"]
                                   else (match or {}).get("publication_id"))
        if not isinstance(base_publication_id, str) or not base_publication_id:
            raise RuntimeError("Detached document has no exact base publication pin; re-run checkout before publishing")
        session = TimelineSession.open(
            candidate["project_id"], candidate["timeline_id"], client=client,
            outbox_path=args.file.with_suffix(".outbox.json"),
            base_revision_id=candidate["base_parent"]["revision_id"],
            base_publication_id=base_publication_id,
        )
        session_state = session._state()
        # Use the opened session's pinned closure read.  Besides keeping the
        # base and session on one authenticated path, this lets callers supply
        # the same session seam for detached-document publication.
        base = session._capture_bundle(session_state)
        imported = import_authoring_media(plan, transport)
        media_selections, verified_media = resolve_media_rebinds(
            transport, candidate["project_id"], imported["candidate"], base
        )
        try:
            native = {"commands": emit_document_publish_commands(
                base, imported["candidate"], media_selections, verified_media
            )}
        except DocumentPatchError as exc:
            raise RuntimeError(f"document publish is held: {exc}") from exc
    # Save the durable rewrite separately; preserve the editable source file for
    # review/retry if import or parent CAS fails. This is not another media store.
    prepared_path = args.file.with_suffix(".prepared.json")
    dump(prepared_path, imported["candidate"])
    dump(args.file.with_suffix(".imports.json"), imported["imports"])
    command_digest = hashlib.sha256(
        json.dumps(native["commands"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    candidate_digest = hashlib.sha256(
        json.dumps(imported["candidate"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt_path = args.file.with_suffix(".publication.json")
    if receipt_path.exists():
        try:
            previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if isinstance(previous, dict) and previous.get("idempotency_key") == args.idempotency_key:
            if (previous.get("candidate_digest"), previous.get("command_digest")) != (
                candidate_digest, command_digest
            ):
                raise RuntimeError("idempotency key was reused with changed document intent; the original publication remains")
            print(json.dumps({
                "receipt": str(receipt_path), "prepared": str(prepared_path),
                "publication": previous.get("publication"), "update": previous["update"],
                "summary": previous["summary"], "next_actions": previous["next_actions"],
                "artifacts": previous["artifacts"],
            }))
            return
    summary = authoring_change_summary(imported["candidate"], media_imports=imported["imports"])
    if not native["commands"]:
        receipt = {
            "idempotency_key": args.idempotency_key,
            "candidate_digest": candidate_digest,
            "command_digest": command_digest,
            "publication": None,
            "media_imports": imported["imports"],
            **publication_feedback(summary, {}, project_id=candidate["project_id"],
                                   timeline_id=candidate["timeline_id"]),
        }
    else:
        publication = session.commit_many(
            native["commands"], idempotency_key=args.idempotency_key, via="script",
            verified_media=verified_media, label="Publish document",
            merge="atomic",
            base={"revision_id": candidate["base_parent"]["revision_id"],
                  "publication_id": base_publication_id},
        )
        publication_data = publication.get("data", {})
        feedback_publication = {"data": {
            "new_head": publication_data.get("accepted_revision_id"),
            "old_head": publication_data.get("previous_revision_id"),
        }}
        receipt = {
            "idempotency_key": args.idempotency_key,
            "candidate_digest": candidate_digest,
            "command_digest": command_digest,
            "publication": publication,
            "media_imports": imported["imports"],
            **publication_feedback(summary, feedback_publication,
                                   project_id=candidate["project_id"],
                                   timeline_id=candidate["timeline_id"]),
        }
    check_path = args.file.with_suffix(".check.json")
    receipt["artifacts"] = {
        "check": str(check_path), "prepared": str(prepared_path),
        "publication": str(receipt_path),
    }
    check_action = {
        "command": shlex.join(["python3", "-m", "json.tool", str(check_path)]),
        "scope": "candidate_preflight",
        "description": "Inspect the check report's indexed property differences and import plan; this is a local file, not complete historical navigation.",
    }
    receipt["next_actions"].append(check_action)
    receipt["update"] += "\nInspect the local check report (indexed property differences and import plan):\n" + check_action["command"]
    dump(receipt_path, receipt)
    print(
        json.dumps(
            {
                "receipt": str(receipt_path),
                "prepared": str(prepared_path),
                "publication": receipt.get("publication"),
                "update": receipt["update"],
                "summary": receipt["summary"],
                "next_actions": receipt["next_actions"],
                "artifacts": receipt["artifacts"],
            }
        )
    )


if __name__ == "__main__":
    main()
