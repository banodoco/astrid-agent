"""Basic pack CLI handlers: validate, new, list, status.

Extracted from ``astrid/core/pack/cli.py``.
Contains ``cmd_validate``, ``cmd_new``, ``cmd_list`` and their supporting
helpers plus the ``_handle_*`` wrappers used by ``build_parser``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from astrid.core.contracts.errors import AstridError
from astrid.core.env_vars import ASTRID_PACKS_PATH
from astrid.core.pack import (
    PackDefinition,
    discover_packs,
    packs_root,
)
from astrid.core.pack.validate import (
    is_first_party_packs_root_candidate,
    validate_first_party_packs_root,
    validate_first_party_packs_root_report,
    validate_pack,
    validate_pack_roots,
)

from ._cli_shared import _TAXONOMY_FIELDS, _pack_payload

# Must match the pack_id pattern in _defs.json: lowercase, digits, underscore
_PACK_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_SKILL_MD_STUB = """---
name: {pack_id}
description: Use this pack for {pack_name} tasks.
---

# {pack_name}

Use the declared pack surface for this task. Add supporting guidance beside
this file and link to it with a normal relative Markdown link.

## Start here

See [the authoring reference](references/guide.md) and the [input template](templates/input.json).
"""

_STARTER_PROFILES = {
    "standalone": "standalone",
    "wrapper": "wrapper",
    "nested": "nested",
}
_STARTER_ROLES = ("action", "ui", "rendering", "shared")
_DEFAULT_STARTER_ROLES = ("action",)


# Shared stderr sink for non-fatal warnings and diagnostics.
def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _pack_id_is_valid(pack_id: str) -> bool:
    """Check that a pack id matches the v1 schema pattern."""
    return bool(_PACK_ID_RE.fullmatch(pack_id))


def _normalise_starter(starter: str) -> str:
    try:
        return _STARTER_PROFILES[starter.strip().lower()]
    except KeyError as exc:
        options = ", ".join(sorted({"standalone", "wrapper", "nested"}))
        raise AstridError(
            f"packs new: unknown starter {starter!r}; choose one of: {options}",
            valid_options=["standalone", "wrapper", "nested"],
            recovery_command="Choose --starter standalone, --starter wrapper, or --starter nested",
        ) from exc


def _normalise_starter_roles(roles: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    selected = tuple(roles or _DEFAULT_STARTER_ROLES)
    normalized: list[str] = []
    for role in selected:
        value = role.strip().lower()
        if value not in _STARTER_ROLES:
            raise AstridError(
                f"packs new: unknown role {role!r}; choose from: {', '.join(_STARTER_ROLES)}",
                valid_options=list(_STARTER_ROLES),
                recovery_command="Choose one or more --role action|ui|rendering|shared values",
            )
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _starter_target(
    pack_id: str,
    *,
    starter: str,
    destination: str | Path | None,
) -> Path:
    if destination is not None:
        return Path(destination).expanduser().resolve()
    if starter == "nested":
        return (Path.cwd() / "integrations" / ("astrid" if pack_id == "astrid" else pack_id)).resolve()
    return (Path.cwd() / pack_id).resolve()


def _starter_manifest(
    pack_id: str,
    pack_name: str,
    *,
    starter: str,
    roles: tuple[str, ...],
    dependency: str,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": 3,
        "id": pack_id,
        "name": pack_name,
        "version": "0.1.0",
        "description": f"A pack for {pack_name}.",
        "domain": "integration" if starter in {"wrapper", "nested"} else "general",
        "stability": "experimental",
        "support": "project",
        "documentation": {"kind": "skill", "path": "docs/SKILL.md"},
    }

    if "action" in roles:
        if starter == "wrapper":
            manifest["actions"] = {
                "adapt": {
                    "description": "Call the external package's public adapter API.",
                    "invocation": {
                        "kind": "python",
                        "path": "actions/adapter.py",
                        "function": "run",
                    },
                    "inputs": [{"name": "value", "type": "string", "required": True}],
                    "outputs": {"type": "string"},
                }
            }
            manifest["dependencies"] = {"python": [dependency]}
        else:
            action_name = "run" if starter == "nested" else "echo"
            manifest["actions"] = {
                action_name: {
                    "description": "Return the supplied value.",
                    "invocation": {
                        "kind": "python",
                        "path": f"actions/{action_name}.py",
                        "function": "run",
                    },
                    "inputs": [{"name": "value", "type": "string", "required": True}],
                    "outputs": {"type": "string"},
                }
            }

    if "ui" in roles:
        manifest["ui"] = {
            "editor": {
                "type": "editor",
                "entry": "ui/editor/extension.tsx",
                "target": "video-editor",
            }
        }

    if "rendering" in roles:
        manifest["rendering"] = {
            "demo": {
                "type": "renderer",
                "path": "rendering/demo/renderer.yaml",
            }
        }

    resources = [
        {"kind": "reference", "path": "docs/references/guide.md"},
        {"kind": "template", "path": "docs/templates/input.json"},
    ]
    if "shared" in roles:
        resources.append({"kind": "shared", "path": "shared/README.md"})
    if starter == "nested":
        resources.append({"kind": "setup-example", "path": "docs/references/source-declaration.example.json"})
    manifest["resources"] = resources
    return manifest


def _starter_skill(pack_id: str, pack_name: str) -> str:
    return _SKILL_MD_STUB.format(pack_id=pack_id, pack_name=pack_name)


def _starter_readme(
    pack_id: str,
    pack_name: str,
    *,
    starter: str,
    roles: tuple[str, ...],
    nested_subpath: str,
) -> str:
    role_text = ", ".join(roles)
    if starter == "wrapper":
        route = (
            "This is a thin wrapper around an unchanged external package. Install the dependency "
            "with the repository's normal package manager, then update `actions/adapter.py` to call "
            "its documented public API. The external source is not vendored here."
        )
    elif starter == "nested":
        route = (
            f"This pack lives at `{nested_subpath}/` inside an existing repository. Keep the "
            "repository's normal build and install path; the pack only declares the public adapter. "
            "The source setup example records this `pack_subpath` for a pinned checkout."
        )
    else:
        route = (
            "This standalone pack owns its small public implementation. Add only the role folders "
            "you use; supporting references, templates, and assets stay beside their owner."
        )
    return f"""# {pack_name}

This starter uses the `{starter}` authoring route and declares the `{role_text}` role(s).

{route}

Run the static author check from this directory:

```bash
python3 -m astrid.core.pack.cli validate .
```

The authored guide is [docs/SKILL.md](docs/SKILL.md). It links to the supporting
[reference](docs/references/guide.md) and [input template](docs/templates/input.json).
"""


def _starter_reference(*, starter: str) -> str:
    if starter == "wrapper":
        return """# External adapter reference

Keep the upstream repository's package layout intact. Install its published package or use its
documented command, then keep the adapter in `actions/adapter.py` small and public-API based.
"""
    if starter == "nested":
        return """# Nested repository reference

The pack root is the selected `integrations/<pack-id>/` subpath. The parent repository remains responsible for normal
build and dependency installation. Update the import in `actions/run.py` to the parent project's
public package after the repository is installed.
"""
    return """# Pack reference

Keep action-specific helpers next to their action. Add `ui/`, `rendering/`, or `shared/` only when
the pack declares that role; there are no required empty category folders.
"""


def _starter_files(
    pack_id: str,
    pack_name: str,
    *,
    starter: str,
    roles: tuple[str, ...],
    external_module: str,
    nested_subpath: str,
) -> dict[str, str]:
    files: dict[str, str] = {
        "README.md": _starter_readme(
            pack_id,
            pack_name,
            starter=starter,
            roles=roles,
            nested_subpath=nested_subpath,
        ),
        "docs/SKILL.md": _starter_skill(pack_id, pack_name),
        "docs/references/guide.md": _starter_reference(starter=starter),
        "docs/templates/input.json": '{"value": "example"}\n',
    }
    if starter == "nested":
        files["docs/references/source-declaration.example.json"] = (
            '{\n'
            f'  "sources": [{{"pack_id": "{pack_id}", "repository": ".",\n'
            f'    "revision": "<full-commit-sha>", "pack_subpath": "{nested_subpath}"}}]\n'
            '}\n'
        )
    if "action" in roles:
        if starter == "wrapper":
            files["actions/adapter.py"] = f'''"""Thin adapter around the unchanged external dependency."""\n\nfrom importlib import import_module\n\n\ndef run(value: str):\n    module = import_module("{external_module}")\n    return module.public_api(value)\n'''
        else:
            action_name = "run" if starter == "nested" else "echo"
            files[f"actions/{action_name}.py"] = '''"""Small runnable action starter."""\n\n\ndef run(value: str) -> str:\n    return value\n'''
    if "ui" in roles:
        files["ui/editor/extension.tsx"] = '''// Existing editor host contribution. Keep host-specific helpers beside this entry.\nexport default function extension() {\n  return { id: "editor-starter" };\n}\n'''
    if "rendering" in roles:
        files["rendering/demo/renderer.yaml"] = f'''id: {pack_id}.demo\nschema_version: 1\nprotocol_version: 1\ncommand:\n  - python3\n  - run.py\n'''
        files["rendering/demo/run.py"] = '''"""Minimal renderer process placeholder for the existing renderer host."""\n\n\ndef render(value):\n    return value\n\n\nif __name__ == "__main__":\n    raise SystemExit(0)\n'''
    if "shared" in roles:
        files["shared/README.md"] = "# Shared support\n\nPut genuinely shared pack support here.\n"
    return files


def _validate_pack_path(path: Path, must_exist: bool = True) -> Path:
    """Resolve and validate a pack root directory path.

    Args:
        path: The path to resolve.
        must_exist: If True, require the directory to exist.

    Returns:
        The resolved Path.

    Raises:
        SystemExit(2) on invalid paths.
    """
    resolved = path.resolve()
    if must_exist and not resolved.is_dir():
        raise AstridError(
            f"packs validate: {path} is not a directory or does not exist",
            recovery_command=f"ls -d {path}  # verify the path exists and is a directory",
        )
    return resolved


def _validate_target(path: Path) -> tuple[list[str], list[str]]:
    if is_first_party_packs_root_candidate(path):
        return validate_first_party_packs_root(path)
    return validate_pack(path)


def _validation_report(path: Path):
    """Build the shared diagnostic report for one CLI validation target."""
    if is_first_party_packs_root_candidate(path):
        return validate_first_party_packs_root_report(path)
    return validate_pack_roots((path,), context="standalone", root=path)


def _print_validation_dispositions(report: Any) -> None:
    counts = report.counts
    support_counts: dict[str, int] = {}
    for result in report.results:
        support_counts[result.support] = support_counts.get(result.support, 0) + 1
    print(
        "  roots: "
        f"{counts['roots']} (active={counts['active']}, valid={counts['valid']}, "
        f"invalid={counts['invalid']})"
    )
    print(
        "  support: "
        + (
            ", ".join(f"{key}={support_counts[key]}" for key in sorted(support_counts))
            if support_counts
            else "none"
        )
    )
    print(
        "  documentation: "
        f"skill={counts['skill']}, agents={counts['agents']}, none={counts['none']}, "
        f"undocumented={counts['undocumented']}"
    )
    for result in report.results:
        label = result.pack_id or str(result.path)
        document = result.documentation
        if result.documentation_path:
            document += f":{result.documentation_path}"
        identity = f"skill={result.skill_name!r}" if result.skill_name else "skill=unavailable"
        if result.skill_description:
            identity += f" description={result.skill_description!r}"
        print(
            f"  pack {label}: status={result.status} visibility={result.visibility} "
            f"support={result.support} documentation={document} {identity}"
        )
    print(
        "  runtime mounts: "
        f"{', '.join(report.runtime_mounts) if report.runtime_mounts else 'none'}"
    )
    print(
        "  excluded: "
        f"{', '.join(report.excluded) if report.excluded else 'none'}"
    )


def cmd_validate(argv: list[str]) -> int:
    """Run static validation on a pack root directory.

    Usage: python3 -m astrid.core.pack.cli validate <path>
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m astrid.core.pack.cli validate",
        description="Statically validate a pack directory.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to the pack root directory (default: current directory).",
    )
    parser.add_argument(
        "--warnings",
        action="store_true",
        help="Also print non-fatal warnings.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable validation diagnostics.",
    )
    args = parser.parse_args(argv)

    pack_root = _validate_pack_path(Path(args.path))

    report = _validation_report(pack_root)
    errors, warnings = report.errors, report.warnings

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0 if not errors else 1

    if errors:
        _print_validation_dispositions(report)
        raise AstridError(
            "\n".join(errors),
            recovery_command="Fix the validation errors listed above and re-run: python3 -m astrid.core.pack.cli validate",
        )

    if args.warnings and warnings:
        for w in warnings:
            _eprint(f"warning: {w}")

    resolved = pack_root.resolve()
    if is_first_party_packs_root_candidate(resolved):
        print(f"valid: {resolved} (context={report.context})")
        _print_validation_dispositions(report)
    else:
        print(f"valid: {resolved} (context={report.context})")
    return 0


def _pack_category(pack: PackDefinition) -> str:
    category = pack.metadata.get("category")
    if isinstance(category, str):
        return category
    return ""


def _effective_status(pack: PackDefinition) -> str:
    if pack.agent.get("purpose") == "TODO: describe what this pack is for":
        return "stub"
    return pack.status


def _taxonomy_filters(args: argparse.Namespace) -> dict[str, str]:
    return {
        field: value
        for field in _TAXONOMY_FIELDS
        if isinstance((value := getattr(args, field, None)), str) and value
    }


def _matches_taxonomy_filters(pack: PackDefinition, args: argparse.Namespace) -> bool:
    for field, value in _taxonomy_filters(args).items():
        if getattr(pack, field) != value:
            return False
    return True


def _filtered_packs(
    args: argparse.Namespace, *, include_hidden: bool | None = None
) -> list[PackDefinition]:
    show_hidden = bool(getattr(args, "show_hidden", False))
    roots = [packs_root()]
    raw_roots = [
        *tuple(getattr(args, "pack_roots", None) or ()),
        *(item for item in os.environ.get(ASTRID_PACKS_PATH, "").split(os.pathsep) if item),
    ]
    roots.extend(Path(item).expanduser() for item in raw_roots)
    packs_by_id: dict[str, PackDefinition] = {}
    for root in roots:
        for pack in discover_packs(
            root,
            include_hidden=show_hidden if include_hidden is None else include_hidden,
        ):
            packs_by_id.setdefault(pack.id, pack)
    packs = list(packs_by_id.values())
    category = getattr(args, "category", None)
    status = getattr(args, "status", None)
    visibility = getattr(args, "visibility", None)
    if category:
        packs = [pack for pack in packs if _pack_category(pack) == category]
    packs = [pack for pack in packs if _matches_taxonomy_filters(pack, args)]
    if status:
        packs = [pack for pack in packs if _effective_status(pack) == status]
    if visibility:
        packs = [pack for pack in packs if pack.visibility == visibility]
    return packs


def _create_pack_skeleton(
    pack_id: str,
    *,
    starter: str = "standalone",
    roles: tuple[str, ...] | list[str] | None = None,
    destination: str | Path | None = None,
    dependency: str = "external-package",
    external_module: str = "external_package",
) -> int:
    """Create and validate a v3 role-based pack starter.

    The constructor remains the existing ``packs new`` route.  Starter
    profiles only choose the authoring journey; role directories are emitted
    when selected and are never created as empty placeholders.
    """
    if not _pack_id_is_valid(pack_id):
        raise AstridError(
            f"packs new: invalid pack id {pack_id!r}. "
            "Must match pattern: ^[a-z][a-z0-9_]*$",
            valid_options=[],
            recovery_command="Pick a pack id using only lowercase letters, digits, and underscores (e.g., my_pack)",
        )

    starter = _normalise_starter(starter)
    selected_roles = _normalise_starter_roles(roles)
    if not dependency.strip():
        raise AstridError(
            "packs new: --dependency must be non-empty",
            recovery_command="Provide the package name installed by the external repository",
        )
    if not external_module.strip():
        raise AstridError(
            "packs new: --external-module must be non-empty",
            recovery_command="Provide the importable module used by the external adapter",
        )

    target = _starter_target(pack_id, starter=starter, destination=destination)
    if target.exists():
        raise AstridError(
            f"packs new: directory {target} already exists; "
            f"refusing to overwrite",
            recovery_command=f"Choose another --destination or remove the empty target directory: {target}",
        )

    if not target.parent.is_dir() and starter != "nested":
        raise AstridError(
            f"packs new: parent directory {target.parent} does not exist",
            recovery_command="Create the parent directory or run packs new from an existing directory",
        )

    pack_name = pack_id.replace("_", " ").title()

    target.mkdir(parents=True)
    manifest = _starter_manifest(
        pack_id,
        pack_name,
        starter=starter,
        roles=selected_roles,
        dependency=dependency.strip(),
    )
    (target / "pack.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    files = _starter_files(
        pack_id,
        pack_name,
        starter=starter,
        roles=selected_roles,
        external_module=external_module.strip(),
        nested_subpath=target.relative_to(Path.cwd().resolve()).as_posix()
        if target.is_relative_to(Path.cwd().resolve())
        else target.name,
    )
    for relative, content in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    created = ["pack.yaml", *sorted(files)]
    for rel in created:
        print(f"created {target.name}/{rel}")

    errors, warnings = validate_pack(target)
    if errors:
        error_details = "\n".join(f"  {err}" for err in errors)
        raise AstridError(
            f"packs new: scaffolded pack fails validation ({len(errors)} error(s))\n{error_details}",
            recovery_command="Fix the validation errors in the scaffolded pack files, then re-run: python3 -m astrid.core.pack.cli validate",
        )

    if warnings:
        for w in warnings:
            _eprint(f"warning: {w}")

    print(
        f"pack {pack_id!r} created and validated: {target} "
        f"(starter={starter}, roles={','.join(selected_roles)})"
    )
    return 0


def cmd_new(argv: list[str]) -> int:
    """Scaffold a minimal pack directory in the CWD.

    Usage: python3 -m astrid packs new <id>
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m astrid packs new",
        description="Create a v3 role-based pack starter.",
    )
    parser.add_argument(
        "pack_id",
        help="Pack identifier (lowercase, digits, underscore; e.g., my_project).",
    )
    parser.add_argument(
        "--starter",
        default="standalone",
        help="Authoring journey: standalone, wrapper, or nested.",
    )
    parser.add_argument(
        "--role",
        action="append",
        choices=_STARTER_ROLES,
        dest="roles",
        help="Role to emit; repeat for a mixed starter (default: action).",
    )
    parser.add_argument(
        "--destination",
        dest="destination",
        help="Pack root to create (nested defaults to integrations/astrid).",
    )
    parser.add_argument(
        "--dependency",
        default="external-package",
        help="External package name for the wrapper journey.",
    )
    parser.add_argument(
        "--external-module",
        default="external_package",
        dest="external_module",
        help="Importable module used by the wrapper action.",
    )
    args = parser.parse_args(argv)
    return _create_pack_skeleton(
        args.pack_id,
        starter=args.starter,
        roles=tuple(args.roles or ()),
        destination=args.destination,
        dependency=args.dependency,
        external_module=args.external_module,
    )


# pack list
# ---------------------------------------------------------------------------


def _list_discovered_packs() -> int:
    """Render the source/manifest pack list."""
    for pack in discover_packs(packs_root()):
        if pack.visibility != "hidden":
            print(f"{pack.id}\t{pack.name}\t{pack.version}")
    return 0


def cmd_list(argv: list[str]) -> int:
    """List source and explicitly configured external packs.

    Usage: python3 -m astrid packs list
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m astrid packs list",
        description="List source and explicitly configured external packs.",
    )
    parser.parse_args(argv)  # no arguments, just parses --help
    return _list_discovered_packs()


# ── Grouped-output helpers (used by _handle_list / _handle_status) ──────────


def _group_packs_by_domain(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        taxonomy = row.get("taxonomy")
        if isinstance(taxonomy, dict):
            domain = taxonomy.get("domain")
        else:
            domain = row.get("domain")
        label = str(domain or "general")
        groups.setdefault(label, []).append(row)
    return [
        {
            "group_by": "domain",
            "value": domain,
            "taxonomy": {"domain": domain},
            "packs": sorted(group_rows, key=lambda pack_row: str(pack_row["id"])),
        }
        for domain, group_rows in sorted(groups.items())
    ]


def _with_grouped_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"packs": rows, "groups": _group_packs_by_domain(rows)}


def _format_list_row(row: dict[str, Any]) -> None:
    taxonomy = row.get("taxonomy", {})
    print(
        f"{row['id']}\t{row['name']}\t{row['version']}\t"
        f"origin={taxonomy.get('origin', '')}\t"
        f"tier={taxonomy.get('install_tier', '')}\t"
        f"type={taxonomy.get('pack_type', '')}\t"
        f"stability={taxonomy.get('stability', '')}\t"
        f"support={taxonomy.get('support', '')}\t"
        f"{row['description']}"
    )


def _format_status_row(row: dict[str, Any]) -> None:
    validation = row["validation"]
    taxonomy = row.get("taxonomy", {})
    print(
        f"{row['id']}\t{row['effective_status']}\t{row['visibility']}\t"
        f"errors={validation['errors']}\twarnings={validation['warnings']}\t"
        f"origin={taxonomy.get('origin', '')}\t"
        f"tier={taxonomy.get('install_tier', '')}\t"
        f"type={taxonomy.get('pack_type', '')}\t"
        f"stability={taxonomy.get('stability', '')}\t"
        f"support={taxonomy.get('support', '')}\t"
        f"{row['description']}"
    )


def _print_grouped_rows(rows: list[dict[str, Any]], *, row_formatter: Any) -> None:
    for index, group in enumerate(_group_packs_by_domain(rows)):
        if index:
            print()
        print(f"taxonomy: domain={group['value']}")
        for row in group["packs"]:
            row_formatter(row)


# ── Handler wrappers used by build_parser ───────────────────────────────────


def _handle_validate(args: argparse.Namespace) -> int:
    """Handler for ``packs validate``."""
    pack_root = _validate_pack_path(Path(args.path))
    report = _validation_report(pack_root)
    errors, warnings = report.errors, report.warnings

    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0 if not errors else 1

    if errors:
        _print_validation_dispositions(report)
        raise AstridError(
            "\n".join(errors),
            recovery_command="Fix the validation errors listed above and re-run: python3 -m astrid.core.pack.cli validate",
        )

    if args.warnings and warnings:
        for warning in warnings:
            _eprint(f"warning: {warning}")

    resolved = pack_root.resolve()
    if is_first_party_packs_root_candidate(resolved):
        print(f"valid: {resolved} (context={report.context})")
        _print_validation_dispositions(report)
    else:
        print(f"valid: {resolved} (context={report.context})")
    return 0


def _handle_new(args: argparse.Namespace) -> int:
    """Handler for ``packs new``."""
    return _create_pack_skeleton(
        args.pack_id,
        starter=getattr(args, "starter", "standalone"),
        roles=tuple(getattr(args, "roles", None) or ()),
        destination=getattr(args, "destination", None),
        dependency=getattr(args, "dependency", "external-package"),
        external_module=getattr(args, "external_module", "external_package"),
    )


def _handle_list(args: argparse.Namespace) -> int:
    """Handler for ``packs list``."""
    packs = _filtered_packs(args)
    rows = [_pack_payload(pack) for pack in packs]
    if args.json:
        print(json.dumps(_with_grouped_payload(rows), indent=2, sort_keys=True))
        return 0
    _print_grouped_rows(rows, row_formatter=_format_list_row)
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    """Handler for ``packs status``."""
    packs = _filtered_packs(args)
    rows: list[dict[str, Any]] = []
    for pack in packs:
        errors, warnings = validate_pack(pack.root)
        payload = _pack_payload(pack)
        payload["effective_status"] = _effective_status(pack)
        payload["validation"] = {
            "errors": len(errors),
            "warnings": len(warnings),
            "error_messages": errors,
            "warning_messages": warnings,
        }
        rows.append(payload)
    if args.json:
        print(json.dumps(_with_grouped_payload(rows), indent=2, sort_keys=True))
        return 0
    _print_grouped_rows(rows, row_formatter=_format_status_row)
    return 0
