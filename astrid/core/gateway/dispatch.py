"""Focused dispatch for the product and operational gateway families."""

from __future__ import annotations

from typing import Any

from astrid.core.contracts.errors import AstridError


def _dispatch(raw: list[str]) -> int:
    from . import _print_entrypoint_help

    if not raw:
        _print_entrypoint_help()
        return 0

    first = raw[0]
    try:
        if first not in _CORE_ROUTE_NAMES:
            _register_installed_pack_routes()
        if first not in frozenset(_TOP_LEVEL_HANDLERS):
            raise AstridError(
                f"unknown command '{first}'",
                valid_options=sorted(_TOP_LEVEL_HANDLERS),
                recovery_command="astrid --help",
                state_snapshot={"command": first},
            )
        parser = _build_dispatch_parser()
        parsed, tail = parser.parse_known_args(raw)
        try:
            return int(parsed.handler(tail))
        except SystemExit as exc:
            # Nested product parsers use argparse's normal help/usage exit.
            # Keep the Python gateway boundary integer-returning while
            # preserving argparse's documented 0/2 status codes.
            return int(exc.code) if isinstance(exc.code, int) else 1
    finally:
        # Pack routes are request-local discovery results. Keeping them in the
        # process-global core table makes later help/tests depend on which
        # command happened to run first.
        _clear_dynamic_pack_routes()


def _top_level_commands() -> frozenset[str]:
    """Return the stable core gateway census, excluding pack routes."""
    return _CORE_ROUTE_NAMES


def _build_dispatch_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(prog="astrid", add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)
    for command, handler in _TOP_LEVEL_HANDLERS.items():
        command_parser = sub.add_parser(command, add_help=False)
        command_parser.set_defaults(handler=handler)
    return parser


def _dispatch_projects(args: list[str]) -> int:
    return _dispatch_product(["projects", *args])


def _dispatch_timelines(args: list[str]) -> int:
    if args and args[0] in {"show", "visualize"} and "--list-views" in args:
        # Declaration inspection needs no Runtime client or workspace lifecycle.
        from astrid.packs.timeline.cli import build_parser
        parsed = build_parser(None).parse_args(args)
        return int(parsed.handler(parsed))
    if args and args[0] == "inspect":
        from astrid.packs.timeline.cli import offline_inspect_main
        return offline_inspect_main(args[1:])
    return _dispatch_product(["timelines", *args])


def _dispatch_media(args: list[str]) -> int:
    return _dispatch_product(["media", *args])


def _dispatch_setup(args: list[str]) -> int:
    """Use the shared setup plan/apply core; preview remains the default."""
    from astrid.setup import main

    return int(main(args))


def _dispatch_status(args: list[str]) -> int:
    """Observe workspace/Runtime/readiness without launcher or provisioning."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="astrid status", description="Read-only workspace and Runtime status.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--diagnostic", action="store_true", help="emit the strict C2 diagnostic document")
    parser.add_argument("--shared", action="store_true", help="redact the diagnostic for shared transport")
    if any(token in {"-h", "--help"} for token in args):
        parser.print_help()
        return 0
    parsed = parser.parse_args(args)
    from astrid.core.auth import contributor_key_present
    from astrid.core.gateway.diagnostics import collect_diagnostic
    from astrid.runtime_cli import RuntimeCLI, RuntimeCLIError
    from astrid.sdk.storage_root import resolve_runtime_data_root

    try:
        support_root = resolve_runtime_data_root()
        runtime = RuntimeCLI()
        diagnostic, workspace, status = collect_diagnostic(
            runtime,
            support_root=support_root,
            mode="shared" if parsed.shared else "local",
            command="status",
        )
    except (RuntimeCLIError, ValueError) as exc:
        payload = {
            "ok": False,
            "problem_code": getattr(exc, "code", "runtime_unavailable"),
            "error": str(exc),
            "effects": ["observe"],
            "authorization_required": False,
            "next_action": "astrid setup",
        }
        if parsed.diagnostic:
            from astrid.core.gateway.diagnostics import build_diagnostic, _failure_from
            code, boundary = _failure_from(exc)
            payload = build_diagnostic(
                mode="shared" if parsed.shared else "local",
                facts={},
                problem_code=code,
                failure_boundary=boundary,
                next_actions=[{
                    "commandId": "runtime-up",
                    "arguments": ["--data-root", "<redacted:path>"] if parsed.shared else [],
                    "effects": ["start-stop-local-service", "configure-install", "write-relocate-change-data"],
                    "authorizationRequired": True,
                    "executable": not parsed.shared,
                }],
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        if parsed.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("Astrid status\nruntime: unavailable\nnext action: astrid setup")
        return 1
    if parsed.diagnostic:
        print(json.dumps(diagnostic, indent=2, sort_keys=True))
        return 0 if diagnostic["problemCode"] is None else 1
    from astrid.core.gateway.diagnostics import redact_for_shared
    workspace_data = redact_for_shared(dict(workspace.data)) if parsed.shared and workspace else (dict(workspace.data) if workspace else {})
    status_data = redact_for_shared(dict(status.data)) if parsed.shared and status else (dict(status.data) if status else {})
    payload = {
        "ok": bool(workspace and workspace.ok and status and status.ok),
        "workspace": workspace_data,
        "runtime": status_data,
        "readiness": {
            "workspace": "ready" if workspace.ok else "unavailable",
            "runtime": "ready" if status.ok else "unavailable",
            "compute_worker": "unavailable",
            "optional_contribution_auth": "local-present-unverified" if contributor_key_present() else "no-local-key",
        },
        "effects": ["observe"],
        "authorization_required": False,
        "diagnostic": diagnostic,
    }
    if parsed.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("Astrid status")
        for key, value in payload["readiness"].items():
            print(f"{key.replace('_', ' ')}: {value}")
        if not payload["ok"]:
            print("next action: astrid setup")
    return 0 if payload["ok"] else 1


def _dispatch_dev(args: list[str]) -> int:
    """Operator commands for developing against a running runtime: status, promote, cache, reclaim."""
    from astrid.core.gateway import dev_operator

    return dev_operator.main(args)


def _dispatch_doctor(args: list[str]) -> int:
    """Read the Runtime observer directly; never connect, start or provision."""
    import argparse
    import json
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="astrid doctor", description="Read-only runtime health check.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--diagnostic", action="store_true", help="emit the strict C2 diagnostic document")
    parser.add_argument("--shared", action="store_true", help="redact the diagnostic for shared transport")
    if any(token in {"-h", "--help"} for token in args):
        parser.print_help()
        return 0
    parsed = parser.parse_args(args)
    try:
        from astrid.core.gateway.diagnostics import collect_diagnostic
        from astrid.runtime_cli import RuntimeCLI, RuntimeCLIError
        from astrid.sdk.storage_root import resolve_runtime_data_root

        diagnostic, _workspace, observed = collect_diagnostic(
            RuntimeCLI(),
            support_root=resolve_runtime_data_root(),
            mode="shared" if parsed.shared else "local",
            command="doctor",
        )
        from astrid.core.gateway.diagnostics import redact_for_shared
        report = dict(redact_for_shared(dict(observed.data))) if parsed.shared and observed else (dict(observed.data) if observed else {"ok": False})
        result_code = observed.returncode if observed else 1
    except (RuntimeCLIError, ValueError) as exc:
        payload = {
            "ok": False,
            "problem_code": getattr(exc, "code", "runtime_unavailable"),
            "state": "runtime_unavailable",
            "next_action": "astrid-runtime up",
            "effects": ["observe"],
            "authorization_required": False,
            "error": str(exc),
        }
        if parsed.diagnostic:
            from astrid.core.gateway.diagnostics import build_diagnostic, _failure_from
            code, boundary = _failure_from(exc)
            payload = build_diagnostic(
                mode="shared" if parsed.shared else "local",
                facts={},
                problem_code=code,
                failure_boundary=boundary,
                next_actions=[{
                    "commandId": "runtime-up",
                    "arguments": ["--data-root", "<redacted:path>"] if parsed.shared else [],
                    "effects": ["start-stop-local-service", "configure-install", "write-relocate-change-data"],
                    "authorizationRequired": True,
                    "executable": not parsed.shared,
                }],
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        if parsed.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Astrid doctor: {payload['error']}", file=sys.stderr)
            print(f"next action: {payload['next_action']}", file=sys.stderr)
        return 1
    report.setdefault("effects", ["observe"])
    report.setdefault("authorization_required", False)
    report["diagnostic"] = diagnostic
    if not parsed.shared:
        try:
            from astrid.sdk.storage_ownership import reclaim
            from astrid.sdk.storage_root import resolve_runtime_data_root as _data_root

            report["reclaimable_storage"] = reclaim(Path(_data_root()), dry_run=True)["reclaimable_bytes"]
        except (OSError, ValueError) as exc:
            report["reclaimable_storage"] = {"error": str(exc)}
    if not parsed.shared:
        try:
            from astrid.core.execution.host_log import host_log_section
            from astrid.core.rendering.node_pin import doctor_section
            from astrid.sdk import visualize_cache
            from astrid.sdk.invocation import _visualize_cache_base
            from astrid.sdk.storage_root import resolve_runtime_data_root as _support_root

            support = _support_root()
            from astrid.core.execution.generic_host import executor_skip_section

            report["executor_skips"] = executor_skip_section()
            report["host_log"] = host_log_section(support)
            report["renderer_node"] = renderer_node = doctor_section(support)
            if renderer_node.get("status") in ("mismatch", "broken"):
                report["healthy"] = False
                report["issues"] = [*report.get("issues", []), f"renderer node: {renderer_node['detail']}; fix: {renderer_node['fix']}"]
                result_code = 1
            report["visualize_cache"] = visualize_cache.cache_size(_visualize_cache_base())
        except (OSError, ValueError) as exc:
            report.setdefault("visualize_cache", {"error": str(exc)})
    compute_fact = diagnostic["facts"]["computeWorker"]
    if diagnostic["problemCode"] == "capability_unavailable":
        result_code = 1
    if parsed.diagnostic:
        print(json.dumps(diagnostic, indent=2, sort_keys=True))
        return 0 if diagnostic["problemCode"] is None else 1
    if parsed.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        state = report.get("state", "ready") if isinstance(report, dict) else "ready"
        print(f"Astrid doctor\nstate: {state}")
        renderer_node = report.get("renderer_node")
        if renderer_node:
            print(f"renderer node: {renderer_node.get('status', 'unknown')} · {renderer_node.get('detail', '')}"
                  + (f"; fix: {renderer_node['fix']}" if renderer_node.get("fix") else ""))
        if isinstance(report, dict) and report.get("recovery_action"):
            print(f"recovery action: {report['recovery_action']}")
    return result_code


def _dispatch_backup(args: list[str]) -> int:
    """Dispatch online backup, restore, export, and realm lifecycle routes."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="astrid backup", add_help=False)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="operation", required=False)
    create = sub.add_parser("create", add_help=False)
    create.add_argument("destination", nargs="?")
    create.add_argument("--out", dest="out", default=None)
    create.add_argument("--json", action="store_true")
    restore = sub.add_parser("restore", add_help=False)
    restore.add_argument("backup")
    restore.add_argument("destination", nargs="?")
    restore.add_argument("--destination", dest="destination_flag", default=None)
    restore.add_argument("--json", action="store_true")
    export = sub.add_parser("export", add_help=False)
    export.add_argument("--out", dest="out", default=None)
    export.add_argument("--json", action="store_true")
    tombstone = sub.add_parser("tombstone", add_help=False)
    tombstone.add_argument("--reason", default=None)
    tombstone.add_argument("--expected-version", type=int, default=None)
    tombstone.add_argument("--json", action="store_true")
    recover = sub.add_parser("recover", add_help=False)
    recover.add_argument("--expected-realm-id", required=True)
    recover.add_argument("--expected-version", type=int, required=True)
    recover.add_argument("--confirm", dest="confirmation", required=True)
    recover.add_argument("--json", action="store_true")
    purge = sub.add_parser("purge", add_help=False)
    purge.add_argument("confirmation")
    purge.add_argument("--json", action="store_true")

    if any(token in {"-h", "--help"} for token in args):
        parser.print_help()
        return 0
    parsed = parser.parse_args(args)
    if parsed.operation == "export" and parsed.out is not None:
        parser.error("backup export does not support --out; the SDK export API has no destination")
    json_mode = "--json" in args
    if parsed.operation is None:
        payload = {"ok": False, "state": "unavailable", "next_action": "banodoco-local up --profile astrid", "error": "backup operation is required"}
        if json_mode:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Astrid backup: {payload['error']}", file=sys.stderr)
            print(f"next action: {payload['next_action']}", file=sys.stderr)
        return 1

    # Reject incomplete or contradictory commands before the launcher can
    # acquire Runtime ownership or start the service. Runtime remains the
    # authority for the actual backup and realm lifecycle effects.
    create_destination = None
    restore_destination = None
    if parsed.operation == "create":
        create_destination = parsed.destination or parsed.out
        if not create_destination:
            parser.error("backup create requires DESTINATION or --out")
    elif parsed.operation == "restore":
        if not parsed.backup:
            parser.error("backup restore requires BACKUP")
        restore_destination = parsed.destination_flag or parsed.destination
        if not restore_destination:
            parser.error("backup restore requires DESTINATION or --destination")
    elif parsed.operation == "tombstone":
        if parsed.expected_version is not None and parsed.expected_version < 1:
            parser.error("backup tombstone --expected-version must be positive")
    elif parsed.operation == "recover":
        if not parsed.expected_realm_id:
            parser.error("backup recover requires --expected-realm-id")
        if parsed.expected_version < 1:
            parser.error("backup recover --expected-version must be positive")
        required_confirmation = f"RECOVER {parsed.expected_realm_id}"
        if parsed.confirmation != required_confirmation:
            parser.error(f"backup recover requires --confirm {required_confirmation!r}")
    elif parsed.operation == "purge" and not parsed.confirmation:
        parser.error("backup purge requires a non-empty confirmation")

    try:
        from astrid.sdk.client import AstridClient
        from astrid.sdk.exceptions import ServiceUnavailableError
        from astrid.sdk.workspace_client import WorkspaceClientError

        # Backup is a workspace-service operation.  It must remain available
        # while the optional pack worker is busy/down and must not reconcile a
        # caller's source checkout or interpreter.
        with AstridClient.open_from_launcher(start_pack_host=False) as client:
            if parsed.operation == "create":
                result = client.create_backup(create_destination)
            elif parsed.operation == "restore":
                result = client.restore_backup(parsed.backup, restore_destination)
            elif parsed.operation == "export":
                result = client.export_realm()
            elif parsed.operation == "tombstone":
                result = client.tombstone_realm(reason=parsed.reason, expected_version=parsed.expected_version)
            elif parsed.operation == "recover":
                result = client.recover_realm(
                    expected_realm_id=parsed.expected_realm_id,
                    expected_version=parsed.expected_version,
                    confirmation=parsed.confirmation,
                    noninteractive=False,
                )
            else:
                result = client.purge_realm(parsed.confirmation)
    except ServiceUnavailableError as exc:
        payload = {
            "ok": False,
            "state": "unavailable",
            "next_action": exc.details.get(
                "next_action", "banodoco-local up --profile astrid"
            ),
            "error": str(exc),
        }
        print(json.dumps(payload, indent=2, sort_keys=True) if json_mode else f"Astrid backup: {payload['error']}\nnext action: {payload['next_action']}", file=None if json_mode else sys.stderr)
        return 1
    except WorkspaceClientError as exc:
        payload = {"ok": False, "error": exc.code, "detail": exc.message, "details": exc.details}
        print(json.dumps(payload, indent=2, sort_keys=True) if json_mode else f"Astrid backup: {exc.message}", file=None if json_mode else sys.stderr)
        return 1

    payload = {"ok": True, **(result if isinstance(result, dict) else dict(result))}
    print(json.dumps(payload, indent=2, sort_keys=True) if json_mode else f"Astrid backup: {parsed.operation} complete")
    return 0


def _dispatch_pack(pack: str, args: list[str]) -> int:
    """Run a declared external-pack command through the Astrid runtime."""
    from .hivemind import dispatch

    return dispatch(pack, args)


def _dispatch_product(args: list[str]) -> int:
    """Run one product-family command through the remote SDK boundary."""
    from astrid.core.cli.domain_product import PRODUCT_FAMILY_SET, run_product_family

    if not args:
        raise AstridError(
            "a product family is required",
            valid_options=sorted(PRODUCT_FAMILY_SET),
            recovery_command="astrid projects --help",
            state_snapshot={"command": "product"},
        )
    family, rest = args[0], args[1:]
    if family not in PRODUCT_FAMILY_SET:
        raise AstridError(
            f"unknown product command '{family}'",
            valid_options=sorted(PRODUCT_FAMILY_SET),
            recovery_command="astrid --help",
            state_snapshot={"command": family},
        )

    if any(token in {"-h", "--help"} for token in rest):
        return run_product_family(family, rest, client=None)

    from astrid.sdk.client import AstridClient

    try:
        if family == "tasks" and rest and rest[0] == "list":
            # Listing tasks is a pure read.  It may reconnect to an already
            # healthy selected Runtime, but must not enter acquisition (which
            # can create a lock file or start Runtime on a cold installation).
            client_context = AstridClient.open_existing_from_launcher()
        else:
            client_context = AstridClient.open_from_launcher(
                start_pack_host=_product_command_needs_pack_host(family, rest)
            )
        with client_context as client:
            return run_product_family(family, rest, client=client)
    except Exception as exc:
        from astrid.sdk.exceptions import ServiceUnavailableError

        if not isinstance(exc, ServiceUnavailableError):
            raise
        from astrid.core.cli.domain_output import print_result
        from astrid.sdk.contracts import DomainResult

        return print_result(
            DomainResult.failure(exc.to_error_object()),
            as_json="--json" in rest,
        )


def _product_command_needs_pack_host(family: str, args: list[str]) -> bool:
    """Return the declarative worker-host requirement for one product route.

    The command registry is the dependency source of truth: workspace CRUD
    and reads default to no pack host, while a capability that actually runs
    pack code opts in on its ``CommandSpec``. Unknown routes fail closed until
    argparse can return their normal usage error.
    """
    from astrid.core.cli.domain_product import command_requires_pack_host

    return command_requires_pack_host(family, args)


def _product_top_level_commands() -> frozenset[str]:
    from astrid.core.cli.domain_product import product_top_level_commands

    return product_top_level_commands()


_TOP_LEVEL_HANDLERS = {
    "projects": _dispatch_projects,
    "documents": lambda args: _dispatch_product(["documents", *args]),
    "preferences": lambda args: _dispatch_product(["preferences", *args]),
    "timelines": _dispatch_timelines,
    "media": _dispatch_media,
    "tasks": lambda args: _dispatch_product(["tasks", *args]),
    "runs": lambda args: _dispatch_product(["runs", *args]),
    "dev": lambda args: _dispatch_dev(args),
    "doctor": _dispatch_doctor,
    "backup": _dispatch_backup,
}
_CORE_ROUTE_NAMES = frozenset(_TOP_LEVEL_HANDLERS)

# External packs opt into the CLI through the same declarative registry. This
# keeps pack installation and command exposure separate: a pack is available
# to the runtime without automatically claiming a top-level command.
from .hivemind import PACK_COMMANDS as _PACK_COMMANDS, installed_pack_ids as _installed_pack_ids

# Pack ids are discoverable, but never allowed to shadow a core family or an
# outer launcher command. This is a blocklist/precedence rule, not a second
# hand-maintained pack allowlist.
_PACK_ROUTE_BLOCKLIST = frozenset(
    {"agent", "auth", "help", "login", "setup", "status", "logout", "revoke", "--help", "--version"}
)
_DYNAMIC_PACK_ROUTE_NAMES: set[str] = set()


def _register_installed_pack_routes() -> None:
    """Register discovered pack ids lazily, preserving core route precedence."""
    for pack_name in _DYNAMIC_PACK_ROUTE_NAMES:
        _TOP_LEVEL_HANDLERS.pop(pack_name, None)
    _DYNAMIC_PACK_ROUTE_NAMES.clear()
    core_names = _CORE_ROUTE_NAMES
    for pack_name in sorted({spec.pack for spec in _PACK_COMMANDS} | _installed_pack_ids()):
        if pack_name in _PACK_ROUTE_BLOCKLIST or pack_name in core_names:
            continue
        _TOP_LEVEL_HANDLERS[pack_name] = (
            lambda args, pack=pack_name: _dispatch_pack(pack, args)
        )
        _DYNAMIC_PACK_ROUTE_NAMES.add(pack_name)


def _clear_dynamic_pack_routes() -> None:
    for pack_name in _DYNAMIC_PACK_ROUTE_NAMES:
        _TOP_LEVEL_HANDLERS.pop(pack_name, None)
    _DYNAMIC_PACK_ROUTE_NAMES.clear()


def compose_profile_handoff(
    manifest_path: str | "Path",
    *,
    support_root: str | "Path",
    registry: "Mapping[str, Any] | None" = None,
    fixtures: "Iterable[Any] | None" = None,
) -> dict[str, Any]:
    """Verify-or-stamp the B-6 handoff at the application composition root.

    This is intentionally separate from transport dispatch.  The generic host
    consumes the resulting stamp, but never discovers profiles or emits it.
    """

    from astrid.core._shared.boot_manifest import (
        manifest_hash,
        stamp_boot_manifest,
        validate_manifest_path,
    )
    from astrid.packs.shots.conformance import (
        VIBE_PROFILE_REGISTRY,
        vibe_profile_specs,
    )
    validated_manifest_path = validate_manifest_path(
        manifest_path, support_root, require_existing=False
    )

    active_registry = VIBE_PROFILE_REGISTRY if registry is None else registry
    active_fixtures = vibe_profile_specs() if fixtures is None else fixtures
    manifest = stamp_boot_manifest(
        validated_manifest_path,
        support_root=support_root,
        registry=active_registry,
        fixtures=active_fixtures,
    )
    return {
        "path": str(validated_manifest_path),
        "sha256": manifest_hash(manifest),
        "manifest": manifest,
    }


def emit_boot_manifest(
    manifest_path: str | "Path",
    *,
    support_root: str | "Path",
    registry: "Mapping[str, Any] | None" = None,
    fixtures: "Iterable[Any] | None" = None,
) -> dict[str, Any]:
    """Explicit composition-root name for callers that do not need routing."""
    return compose_profile_handoff(
        manifest_path,
        support_root=support_root,
        registry=registry,
        fixtures=fixtures,
    )
