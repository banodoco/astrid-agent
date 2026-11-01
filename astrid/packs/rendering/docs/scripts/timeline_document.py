"""Check out, validate, or publish a detached timeline JSON document."""

import argparse
import json
import shlex
from pathlib import Path
from urllib.parse import urlparse

from astrid.sdk import AstridClient
from astrid.core.path_utils import file_uri_to_path
from astrid.sdk.authoring_bundle import (
    import_authoring_media,
    diff_authoring_candidate,
    open_authoring_bundle,
    plan_authoring_media,
    publish_authoring_candidate,
)
from astrid.sdk.autobootstrap import ensure_runtime
from astrid.sdk.workspace_client import WorkspaceClient, resolve_runtime_connection


def typed(result):
    if not result.ok:
        raise RuntimeError(str(result.error))
    return result.data


def data(response):
    return response.get("data", response)


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def reject_local_media_inputs(value, base_directory, path="candidate"):
    """Avoid silently publishing an old media pin when a local path was edited."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "local_path" and child:
                raise ValueError(
                    f"{child_path} is a local media input; import it with the public "
                    "Astrid media client first, then pin the returned digest"
                )
            if key in {"file", "path", "src"} and isinstance(child, str):
                parsed = urlparse(child)
                local_value = file_uri_to_path(child) if parsed.scheme == "file" else child
                local_path = Path(local_value)
                if parsed.scheme == "file" or local_path.is_absolute():
                    resolved_path = local_path
                else:
                    resolved_path = base_directory / local_path
                if resolved_path.is_file():
                    raise ValueError(
                        f"{child_path} points to local media; import it with the public "
                        "Astrid media client first, then pin the returned digest"
                    )
            reject_local_media_inputs(child, base_directory, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_local_media_inputs(child, base_directory, f"{path}[{index}]")


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


class Writer:
    def __init__(self, transport):
        self.transport = transport

    def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
        return data(
            self.transport.publish_parent_composition(
                project_id, timeline_id, publication, idempotency_key=idempotency_key
            )
        )


def checkout(args):
    if args.file.exists():
        raise FileExistsError(f"Refusing to overwrite {args.file}; choose a new checkout file")
    with AstridClient.open_from_launcher(start_pack_host=False) as client:
        project = typed(client.projects.show(args.project))
        project_id = project.get("project_id") or project["id"]
        timeline_id, inspection = resolve_timeline_scope(client, project_id, args.timeline)
        head = inspection["summary"]["head_revision_id"]
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
    for name in ("check", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--file", type=Path, required=True)
        if name == "publish":
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
    # Verify selection against this launcher before crossing the publication boundary.
    with AstridClient.open_from_launcher(start_pack_host=False) as client:
        typed(client.timelines.open_composition(candidate["project_id"], candidate["timeline_id"]))
    # Runtime owns replay-before-CAS: an identical accepted request may be
    # retried after the canonical head advances, while a new request remains
    # subject to the publication's expected_head.
    transport = workspace()
    imported = import_authoring_media(plan, transport)
    # Save the durable rewrite separately; preserve the editable source file for
    # review/retry if import or parent CAS fails. This is not another media store.
    prepared_path = args.file.with_suffix(".prepared.json")
    dump(prepared_path, imported["candidate"])
    dump(args.file.with_suffix(".imports.json"), imported["imports"])
    receipt = publish_authoring_candidate(
        imported["candidate"], Writer(transport), idempotency_key=args.idempotency_key,
        media_imports=imported["imports"],
    )
    receipt["media_imports"] = imported["imports"]
    receipt_path = args.file.with_suffix(".publication.json")
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
