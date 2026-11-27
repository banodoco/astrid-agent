"""Read-only public workflow requirements preview (P90 WS9).

The reviewed VibeComfy producer owns the exact plan and its digest. This
module selects the existing target, reads existing approvals and transports
that plan; preview never installs, publishes, or creates a grant.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .preflight import GenerationReadinessError, readiness_envelope


@dataclass(frozen=True)
class PreviewSource:
    path: Path
    entry: dict[str, Any] | None
    approvals: list[dict[str, Any]]


def resolve_source(value: str) -> PreviewSource:
    """Resolve FILE or ENTRY using metadata only; never execute Python to inspect it."""
    from vibecomfy.entries import entries_dir, load_entry
    from .entries import contributed_entries, _ready_entry_rows
    from .node_approvals import entry_approval_directory, read_entry_approvals

    path = Path(value).expanduser()
    root = entries_dir()
    if path.exists():
        manifest = path / "entry.json" if path.is_dir() else path
        if manifest.name != "entry.json" or not manifest.is_file():
            return PreviewSource(path.resolve(), None, [])
    else:
        manifest = root / value / "entry.json" if Path(value).name == value else None
        if manifest is None or not manifest.is_file():
            rows = [row for row in contributed_entries() if row["id"] == value]
            rows.extend(_ready_entry_rows(value))
            if len(rows) != 1 or not rows[0].get("bundle"):
                raise ValueError("Select an existing workflow file or unambiguous workflow entry.")
            row = rows[0]
            bundle = Path(row["bundle"]).expanduser()
            if not bundle.exists():
                raise ValueError("The selected entry has no readable workflow bundle.")
            return PreviewSource(bundle.resolve(), row, [])
    entry = load_entry(manifest)
    approvals = []
    # Only a saved entry selected from its actual authority owns a P56 sidecar.
    # An arbitrary manifest file may be previewed but cannot import approvals.
    if manifest.parent.absolute() == (root / entry["id"]).absolute():
        entry = {**entry, "source": "user-entry"}
        directory = entry_approval_directory(entry)
        if directory is not None:
            approvals = read_entry_approvals(directory)
    return PreviewSource(manifest.absolute(), entry, approvals)


def read_grant_status(kind: str, subject: str, digest: str) -> dict[str, Any]:
    """Observe the current actor's protected P35 ledger over the existing GET.

    Identity and normal status shape mirror RuntimeService.grant_status. No
    local fallback and no generic app-state PUT. A failed read is not absence
    and cannot confer authority. WS2/RS1 must supply the actual grant subject
    and digest; this helper never derives them from a guessed repo name.
    """
    from astrid.core import user_state

    if kind not in ("pack", "model", "node"):
        raise ValueError("invalid grant kind")
    if not isinstance(subject, str) or not subject or len(subject) > 96:
        raise ValueError("invalid grant subject")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid grant digest")
    encoded = json.dumps([kind, subject, digest], sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    grant_id = kind + "-" + hashlib.sha256(encoded).hexdigest()[:24]

    def result(state, generation=None):
        return {"grant_id": grant_id, "state": state,
                "authorized": state == "approved", "generation": generation}

    try:
        document = user_state.read("grants." + kind)
    except user_state.UserStateError as exc:
        return result("unavailable" if exc.code == "unavailable" else "unknown")
    except (OSError, KeyError, TypeError, ValueError):
        return result("unknown")
    content = document.content
    if content is None:
        return result("absent", 0)
    if (not isinstance(content, dict) or content.get("schema") != "astrid.grants.v1"
            or content.get("kind") != kind or not isinstance(content.get("grants"), dict)):
        return result("unknown")
    grants = content["grants"]
    if grant_id not in grants:
        return result("absent", 0)
    row = grants[grant_id]
    if (not isinstance(row, dict) or row.get("subject") != subject or row.get("digest") != digest
            or row.get("state") not in ("approved", "revoked")
            or type(row.get("generation")) is not int or row["generation"] < 1):
        return result("unknown")
    return result(row["state"], row["generation"])


def preview_envelope(plan, *, entry, target, issues=(), fixes=()):
    """Transport one complete producer plan, never an Agent-created digest.

    The caller maps the actual WS2/RS1 gap/action fields to readiness issues
    and P56 fixes. Do not derive readiness from a guessed status field.
    """
    from astrid.core.contracts.errors import redact_credential_data

    if (not isinstance(plan, dict) or not {"requirements", "gaps", "bytes", "digest"} <= plan.keys()
            or not isinstance(plan["requirements"], list) or not isinstance(plan["gaps"], list)):
        raise ValueError("requirements producer returned an invalid plan")
    # The digest must describe the displayed plan. Do not display a redacted
    # approximation as the exact approval object, and do not bypass the
    # common credential filter. RS1 checks the actual public JSON here.
    if redact_credential_data(plan) != plan:
        raise ValueError("requirements producer returned non-public plan data")
    if any(fix.get("who") not in ("agent", "human") for fix in fixes):
        raise ValueError("each requirements fix must name who acts")
    envelope = readiness_envelope(entry=entry, target=target, issues=issues,
                                  fixes=fixes, plan=plan)
    if envelope["status"] == "blocked":
        raise GenerationReadinessError(envelope)
    return envelope


def _capacity(*roots):
    """Only a single observed filesystem capacity; never guess a multi-volume budget."""
    import shutil

    try:
        paths = [Path(root) for root in roots]
        if not paths or not all(path.is_dir() for path in paths):
            return None
        devices = {path.stat().st_dev for path in paths}
        if len(devices) != 1:
            return None
        return min(shutil.disk_usage(path).free for path in paths)
    except (OSError, TypeError, ValueError):
        return None


def _placement(selector):
    """Use the existing placement resolver without opening or migrating Runtime."""
    from astrid.core import user_state
    from . import placement, settings

    class ReadOnlySettings:
        def get_user_app_state(self, key):
            if key != settings.KEY:
                raise ValueError("unexpected placement settings key")
            try:
                document = user_state.read(key)
            except user_state.UserStateError:
                if selector == "auto":
                    raise ValueError("Saved placement unavailable; select --placement explicitly.")
                return {"content": settings.empty_document(), "version": 0}
            if document.content is None:
                # The same legacy projection as settings.read, but no migration PUT.
                content = settings.validate_document({**settings.empty_document(), **settings._legacy(None)})
            else:
                content = settings.validate_document(document.content)
            return {"content": content, "version": document.version}

    with settings.using(ReadOnlySettings()):
        selected = placement.resolve_placement(placement.request_from_selector(selector))
    return placement.validate_placement(selected, machine_kinds=("runpod",), live_leases=lambda _: ())


def _attached_target(endpoint):
    """Read live paths, with native process evidence for root/Python; no launch."""
    from .backends.comfy_target import ComfyTarget, ComfyTargetClient, validate_attached_url
    from astrid.packs.vibecomfy.local import _observed_launch_facts

    endpoint = validate_attached_url(endpoint)
    result = {"placement_class": "attached", "location": "local", "owner": "human",
              "endpoint": endpoint, "root": None, "python": None, "comfyui_root": None,
              "models_root": None, "custom_nodes_root": None, "capacity_bytes": None}
    client = ComfyTargetClient(ComfyTarget(endpoint, "attached"))
    try:
        stats = client.request_json("/system_stats", timeout=1.0)
        if not isinstance(stats, dict) or not isinstance(stats.get("system"), dict):
            raise ValueError("invalid target facts")
        folders = client.request_json("/internal/folder_paths", timeout=5.0)
    except (OSError, ValueError, TimeoutError) as exc:
        cause = exc.__cause__
        result["selection_state"] = "timeout" if isinstance(exc, TimeoutError) or isinstance(cause, TimeoutError) else "unknown"
        return result
    if not isinstance(folders, dict):
        return result

    def single(value):
        values = [value] if isinstance(value, str) else value
        if (not isinstance(values, list) or len(values) != 1
                or not isinstance(values[0], str) or not Path(values[0]).is_absolute()):
            return None
        return Path(values[0])

    nodes = single(folders.get("custom_nodes"))
    if nodes is None:
        return result
    # This existing native observation verifies main.py, argv, the selected
    # interpreter and the listener birth. HTTP hints alone are not authority.
    root = nodes.parent
    facts = _observed_launch_facts({"url": endpoint, "base_dir": str(root)})
    if not facts.get("python"):
        return result
    result.update(root=str(root), python=facts["python"], comfyui_root=str(root),
                  custom_nodes_root=str(nodes))
    # The producer supports one models root, not a heterogeneous folder map.
    # Accept only the observed canonical common root; multiple/aliased/extra
    # model paths remain unknown rather than inventing a writable destination.
    checkpoints = single(folders.get("checkpoints"))
    if checkpoints is not None and checkpoints.name == "checkpoints":
        candidate = checkpoints.parent
        # Bind every reported model folder, excluding non-model Comfy folders.
        from .backends.comfy_target import _COMFY_MODEL_FOLDERS
        keys = set(_COMFY_MODEL_FOLDERS.values())
        if all(single(folders.get(key)) == candidate / key for key in keys):
            result["models_root"] = str(candidate)
    # Human environment capacity is not a substitute for the transfer owner's
    # scoped disk budget. Leave it unknown when folders are not fully bound.
    return result


def selected_target(selector):
    """Map actual selected recipe/receipt paths, never the controller interpreter."""
    from . import asset_alignment
    from .engine_contracts import EngineError
    from .engine_recipe import recipe_digest

    selected = _placement(selector)
    if selected.kind == "runpod":
        # No machine claim or inference of a future paid target's identity.
        return {"placement_class": "runpod", "location": "remote", "owner": None,
                "root": None, "python": None, "comfyui_root": None,
                "models_root": None, "custom_nodes_root": None, "capacity_bytes": None}
    if selected.kind == "attached":
        return _attached_target(selected.endpoint)
    try:
        actual = asset_alignment.selected_target()
    except (EngineError, OSError, ValueError, KeyError):
        return {"placement_class": "local", "location": "local", "owner": "astrid",
                "root": None, "python": None, "comfyui_root": None,
                "models_root": None, "custom_nodes_root": None, "capacity_bytes": None}
    comfy = actual.root / next(source.relative_path for source in actual.recipe.sources
                               if source.name == "comfyui")
    return {
        "root": str(actual.root), "python": actual.python,
        "comfyui_root": str(comfy), "models_root": str(actual.models_root),
        "custom_nodes_root": str(actual.custom_nodes_root),
        "placement_class": "local", "location": "local", "owner": "astrid",
        "endpoint": actual.url, "device": selected.device,
        "capacity_bytes": _capacity(actual.root, actual.models_root, actual.custom_nodes_root),
        "recipe": {"sha256": recipe_digest(actual.recipe),
                   "receipt_sha256": actual.receipt_sha256,
                   "models_lock_sha256": actual.models_lock_sha256},
    }


def _public_plan(value, digest):
    from astrid.core.contracts.errors import redact_credential_data

    if not isinstance(value, dict) or redact_credential_data(value) != value:
        raise ValueError("requirements producer returned non-public plan data")
    if value.get("schema") != "vibecomfy.requirements-plan.v1" or value.get("digest") != digest(value):
        raise ValueError("requirements producer returned an invalid plan identity")
    return value


def _consent_records(value, supplied, saved):
    """Check public P56 codes against the producer's exact existing records."""
    from .node_approvals import approval_record, consent_matches, consent_token

    choices = [choice for choice in supplied if choice != "user_env"]
    records = {row["approval_sha256"]: row for row in saved}
    current = {}
    for item in value["requirements"]:
        if item["kind"] != "node" or item["approval"]["record"] is None:
            continue
        approval = item["approval"]
        node = item["node"]
        placement_class = value["target"]["placement_class"]
        record = approval_record(node, placement_class=placement_class)
        code = approval["consent_code"]
        if approval["record"] != record or code != consent_token(record):
            raise ValueError("requirements producer returned inconsistent P56 approval evidence")
        current["code=" + code] = (record, node)
    if any(choice not in current for choice in choices):
        return saved, False
    for choice in choices:
        record, node = current[choice]
        if not consent_matches(choice.removeprefix("code="), node,
                               placement_class=value["target"]["placement_class"]):
            return saved, False
        records[record["approval_sha256"]] = record
    return list(records.values()), True


def _retry_argv(args):
    # A requirements invocation is always a preview; this is a functional
    # recheck command, not an apply promise. Strict P56 fixes omit --dry-run.
    return ["astrid", "generate", "requirements", args.source,
            "--placement", args.placement,
            *[part for value in args.add for part in ("--add", value)],
            *[part for value in args.add_model for part in ("--add-model", value)], "--json"]


def _readiness(value, args, *, consent_valid=True):
    """Map every producer gap to a typed blocking issue and named recheck."""
    issues, fixes = [], []
    rows = {item["id"]: item for item in value["requirements"]}
    gaps = list(value["gaps"])
    if not consent_valid:
        gaps.append({"code": "stale_code_consent", "state": "unapproved",
                     "who": "human", "requirement": None})
    if value["target"].get("selection_state") == "timeout":
        gaps.append({"code": "selected_target_timeout", "state": "timeout",
                     "who": "human", "requirement": None})
    for index, gap in enumerate(gaps):
        code, state, who = gap["code"], gap["state"], gap["who"]
        if state not in ("blocked", "unknown", "timeout", "unapproved") or who not in ("agent", "human"):
            raise ValueError("requirements producer returned an invalid gap")
        fix_id = f"requirements-{index + 1}"
        retry = _retry_argv(args)
        item = rows.get(gap["requirement"])
        if code == "node_code_unapproved":
            if not item or not item["approval"]["consent_code"]:
                raise ValueError("unapproved node lacks its exact public P56 code")
            node = item["node"]
            consent_code = item["approval"]["consent_code"]
            step = (f"Show the person {node['repo']}@{node['commit']} and recipe "
                    f"{item['recipe_sha256']} on the {value['target']['placement_class']} target. "
                    "Third-party code can access that target's files, credentials and network; "
                    "install.py is never run by the controlled installer. Preview runs no code. "
                    f"Only after informed approval, recheck with code={consent_code}; "
                    "this is not a saved approval or Runtime grant.")
            # Freeze any matching explicit branch/tag in the recheck, using the
            # exact producer source. Other unresolved additions remain unresolved.
            for n, arg in enumerate(retry[:-1]):
                if arg == "--add":
                    raw = retry[n + 1]
                    repo = raw.rsplit("@", 1)[0] if "@" in raw else raw
                    if repo.rstrip("/").removesuffix(".git") == node["repo"].rstrip("/").removesuffix(".git"):
                        retry[n + 1] = node["repo"] + "@" + node["commit"]
            retry.extend(["--consent", "code=" + consent_code])
            issue_code = "needs_consent"
        elif code == "stale_code_consent":
            step = "The supplied code is bare, stale or does not match this exact plan. Recheck and show the current warning; do not save or reuse that code."
            issue_code = "needs_consent"
        elif code == "workflow_code_not_admitted":
            step = "Python workflow code has not been admitted. Use a JSON workflow or the existing separately admitted workflow authority; do not execute Python during preview."
            issue_code = "needs_consent"
        elif code in ("target_capacity_unknown", "required_disk_unknown", "insufficient_capacity", "disk_budget_incomplete"):
            step = "Obtain the selected destination's actual capacity and the transfer owner's measured disk budget; recheck. Unknown capacity or disk peak is not zero."
            issue_code = "unknown_input"
        else:
            step = f"Resolve the reported {code} ({state}) for the selected workflow/target, then recheck. Preview cannot repair it or authorize apply."
            issue_code = ("protected_change" if code == "human_environment_change" else
                          "pin_conflict" if code.startswith("conflicting_") or code.endswith("_conflict") else
                          "source_unreachable" if code in ("source_ref_unresolved", "source_tree_unavailable") else
                          "unknown_input")
        issues.append({"code": issue_code, "severity": "error",
                       "message": f"{code}: {state}", "details": {"gap": gap, "state": state},
                       "fix_ids": [fix_id]})
        fixes.append({"id": fix_id, "who": who, "scope": "read_only",
                      "instruction": {"step": step, "then_argv": retry}})
    return issues, fixes


def main(args, *, output=None) -> int:
    """Public preview with the real reviewed producer; no mutation or apply."""
    from .preflight import generation_error
    from astrid.core.contracts.errors import redact_credential_data
    output = sys.stdout if output is None else output
    try:
        from vibecomfy.requirements import plan as canonical_plan, plan_digest
    except ImportError as exc:
        raise generation_error(
            ValueError("The canonical workflow requirements producer is not installed."),
            code="missing_dependency",
            details={"field": "requirements", "producer": "vibecomfy.requirements.plan"},
        ) from exc
    if not callable(canonical_plan) or not callable(plan_digest):
        raise generation_error(ValueError("The installed requirements producer is invalid."),
                               code="missing_dependency")
    # Refuse credentials before source resolution or rendering retry argv.
    from urllib.parse import urlsplit
    raw = [args.source, args.placement, *args.add, *args.add_model, *args.consent]
    if redact_credential_data(raw) != raw:
        raise ValueError("requirements input contains non-public data")
    for value in [*args.add, *(model.split("=", 1)[-1] for model in args.add_model)]:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or parsed.query:
            raise ValueError("requirements source must use a public credential-free URL")
    source = resolve_source(args.source)
    target = selected_target(args.placement)
    options = {"target": target, "add": args.add, "add_models": args.add_model}
    value = _public_plan(canonical_plan(source.path, approvals=source.approvals, **options), plan_digest)
    records, valid = _consent_records(value, args.consent, source.approvals)
    if valid and records != source.approvals:
        # The first read is only a preview. A second real plan binds transient
        # P56 records and re-resolves sources; do not promote stale confirmation.
        value = _public_plan(canonical_plan(source.path, approvals=records, **options), plan_digest)
        _, valid = _consent_records(value, args.consent, source.approvals)
    issues, fixes = _readiness(value, args, consent_valid=valid)
    entry = {"id": source.entry["id"]} if source.entry is not None else None
    envelope = preview_envelope(value, entry=entry, target=value["target"], issues=issues, fixes=fixes)
    print(json.dumps(envelope, indent=2, sort_keys=True), file=output)
    return 0
