"""CLI adapter for the Astrid-managed Hivemind pack."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from astrid.core.cli.domain_output import print_result
from astrid.sdk.contracts import DomainResult, ErrorObject


@dataclass(frozen=True)
class PackCommandSpec:
    """Declarative public command mapping for one installed pack."""

    pack: str
    command: str
    capability_id: str
    kind: str = "executor"


PACK_COMMANDS = (
    PackCommandSpec("hivemind", "search", "hivemind.search"),
)


def installed_pack_ids() -> frozenset[str]:
    """Discover installed pack ids without opening the runtime or a project."""
    try:
        from astrid.core.pack.discovery import discover_pack_metadata

        return frozenset(pack.id for pack in discover_pack_metadata())
    except Exception:  # pragma: no cover - minimal installs may omit discovery deps
        return frozenset()


def _error_result(exc: Exception) -> DomainResult[None]:
    details = getattr(exc, "details", {})
    if not isinstance(details, dict):
        details = {}
    code = str(details.get("code") or "runtime_error")
    message = str(exc)
    return DomainResult.failure(ErrorObject(code, message, details))


def _parser(pack: str = "hivemind") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"astrid {pack}",
        description="Run a declared external pack command through the Astrid runtime.",
    )
    sub = parser.add_subparsers(dest="operation")
    search = sub.add_parser("search", help="Search messages and current resources.")
    search.add_argument("query", help="Search terms.")
    search.add_argument("--kinds")
    search.add_argument("--sources")
    search.add_argument("--since")
    search.add_argument("--until")
    search.add_argument("--channel")
    search.add_argument("--author")
    search.add_argument("--thread")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--sort", choices=("relevance", "recent"), default="relevance")
    search.add_argument("--detach", action="store_true", help="Return admission without waiting.")
    search.add_argument("--timeout", type=float, default=120.0, dest="timeout_seconds")
    search.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=False)
    return parser


def _search(parsed: argparse.Namespace, spec: PackCommandSpec) -> int:
    from astrid.sdk.client import AstridClient

    inputs: dict[str, Any] = {
        "query": parsed.query,
        "limit": parsed.limit,
        "offset": parsed.offset,
        "sort": parsed.sort,
    }
    for name in ("kinds", "sources", "since", "until", "channel", "author", "thread"):
        value = getattr(parsed, name, None)
        if value:
            inputs[name] = value
    try:
        with AstridClient.open_from_launcher() as client:
            result = client.invoke_result(
                spec.capability_id,
                kind=spec.kind,
                inputs=inputs,
                wait=not parsed.detach,
                timeout_seconds=parsed.timeout_seconds,
            )
            raw = result.to_dict() if hasattr(result, "to_dict") else dict(result)
            if not raw.get("ok"):
                error = raw.get("error") or {}
                details = dict(error.get("details") or {}) if isinstance(error, dict) else {}
                if isinstance(error, dict):
                    for key in ("type", "sdk_error", "sdk_category", "validation"):
                        if key in error:
                            details.setdefault(key, error[key])
                code = (
                    error.get("code")
                    or error.get("sdk_error")
                    or error.get("type")
                    or "runtime_error"
                ) if isinstance(error, dict) else "runtime_error"
                return print_result(
                    DomainResult.failure(
                        ErrorObject(
                            str(code),
                            str(error.get("message") or "Hivemind search failed"),
                            details,
                        )
                    ),
                    as_json=getattr(parsed, "json", False),
                )
            data: Any = {
                "run_id": raw.get("run_id"),
                "state": "admitted" if parsed.detach else "completed",
                "capability_id": raw.get("capability_id"),
                "capability_type": raw.get("capability_type"),
                "native_kind": raw.get("native_kind"),
                "manifest_path": raw.get("manifest_path"),
                "executor_version": raw.get("executor_version"),
                "kernel_run_id": raw.get("kernel_run_id"),
                "kernel_task_id": raw.get("kernel_task_id"),
                "kernel_attempt_id": raw.get("kernel_attempt_id"),
                "outputs": raw.get("outputs") or {},
                "raw_result": raw.get("raw_result") or {},
            }
            artifacts = ((raw.get("outputs") or {}).get("artifacts") or [])
            if artifacts:
                data["artifacts"] = artifacts
            if not parsed.detach and artifacts:
                primary = next((item for item in artifacts if item.get("name") == "results"), artifacts[0])
                digest = primary.get("digest")
                if digest:
                    data["results"] = json.loads(client.media.read_bytes(digest).decode("utf-8"))
                    data["artifact"] = primary
            result = DomainResult.success(data)
            if getattr(parsed, "json", False):
                return print_result(result, as_json=True)
            results_payload = data.get("results")
            results = (
                results_payload.get("results")
                if isinstance(results_payload, dict)
                else results_payload
            )
            if isinstance(results, list):
                print(f"{len(results)} result(s)")
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    title = (
                        item.get("title")
                        or item.get("body")
                        or item.get("context")
                        or item.get("item_id")
                        or "result"
                    )
                    text = " ".join(str(title).split())
                    print(f"- {text[:240]}{'…' if len(text) > 240 else ''}")
                return 0
            print(f"run_id: {data.get('run_id', '')}")
            return 0
    except Exception as exc:  # noqa: BLE001 - convert to the stable CLI envelope
        return print_result(_error_result(exc), as_json=getattr(parsed, "json", False))


def _pack_cli_guidance(pack: str) -> str:
    """Describe declared SDK actions when an installed pack has no CLI route."""
    actions: tuple[str, ...] = ()
    try:
        from astrid.core.pack.discovery import discover_pack_metadata

        entry = next((item for item in discover_pack_metadata() if item.id == pack), None)
        if entry is not None:
            actions = tuple(f"{pack}.{name}" for name in entry.pack.actions)
    except Exception:  # pragma: no cover - minimal installs may omit discovery deps
        actions = ()

    lines = [f"astrid {pack}: no declared CLI commands."]
    if actions:
        lines.extend((
            f"Available actions: {', '.join(actions)}",
            "Invoke an action from Python:",
            "  import astrid.sdk as sdk",
            f'  result = sdk.invoke("{actions[0]}", kind="action", project="<project>", inputs={{...}})',
        ))
    else:
        lines.append("Invoke a declared capability from Python with astrid.sdk.invoke(...).")
    return "\n".join(lines)


def dispatch(pack: str, args: list[str]) -> int:
    specs = tuple(spec for spec in PACK_COMMANDS if spec.pack == pack)
    if not specs and (not args or any(token in {"-h", "--help"} for token in args)):
        print(_pack_cli_guidance(pack))
        return 0
    if not specs:
        print(_pack_cli_guidance(pack), file=sys.stderr)
        return 2
    parser = _parser(pack)
    if not args:
        parser.print_help()
        return 0
    parsed = parser.parse_args(args)
    spec = next((item for item in specs if item.command == parsed.operation), None)
    if spec is None:
        print(f"astrid {pack}: operation is not declared", file=sys.stderr)
        return 2
    if spec.command != "search":
        print(f"astrid {pack}: command adapter is not implemented", file=sys.stderr)
        return 2
    return _search(parsed, spec)


__all__ = ["PackCommandSpec", "PACK_COMMANDS", "dispatch", "installed_pack_ids"]
