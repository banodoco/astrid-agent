"""Static pack import and Astrid SDK compatibility checks.

The validator never imports pack code. It reads ``pack.yaml`` and Python source
files under the supplied pack root, then reports all findings as data.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class _UniqueLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    pairs = loader.construct_pairs(node, deep=deep)
    result: dict = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise yaml.YAMLError("pack.yaml declaration keys must be strings")
        if key in result:
            raise yaml.YAMLError(f"pack.yaml declaration contains duplicate key {key!r}")
        result[key] = value
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass(frozen=True, slots=True)
class PackImportReport:
    """Immutable result of checking a pack's source imports."""

    root: Path
    imports: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


def _version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or not (match := _VERSION.fullmatch(value.strip())):
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _module_exists(root: Path, module: str) -> bool:
    """Check a pack-local dotted module without importing or executing it."""
    parts = module.split(".") if module else []
    if not parts or any(not part.isidentifier() for part in parts):
        return False
    base = root.joinpath(*parts)
    return (base.with_suffix(".py").is_file() or (base / "__init__.py").is_file())


def _imports(tree: ast.AST) -> Iterable[tuple[str, int, tuple[str, ...]]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno, ()
        elif isinstance(node, ast.ImportFrom):
            yield "." * node.level + (node.module or ""), node.lineno, tuple(
                alias.name for alias in node.names
            )


def check_pack_imports(
    pack_root: str | Path,
    *,
    host_version: str,
    public_sdk_modules: Iterable[str] = (),
) -> PackImportReport:
    """Validate local imports and the pack's declared Astrid SDK version.

    ``astrid_version`` in ``pack.yaml`` is an exact ``MAJOR.MINOR.PATCH`` SDK
    compatibility floor. It must be parseable, share the host's major version,
    and not be newer than the host. The optional ``public_sdk_modules`` list is
    the host's public module inventory; SDK imports outside it fail closed.

    Imports outside the pack and the Astrid SDK are left to the pack's
    environment resolver. Relative imports and imports rooted in the pack's
    own top-level package are checked against source files on disk.
    """
    root = Path(pack_root)
    errors: list[str] = []
    found_imports: list[str] = []

    parsed_host = _version(host_version)
    if parsed_host is None:
        return PackImportReport(root, (), (f"host_version must be MAJOR.MINOR.PATCH, got {host_version!r}",))

    manifest_path = root / "pack.yaml"
    try:
        manifest = yaml.load(manifest_path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return PackImportReport(root, (), (f"pack.yaml could not be read: {exc}",))
    if not isinstance(manifest, dict):
        return PackImportReport(root, (), ("pack.yaml declaration must be a mapping",))

    if "astrid_version" in manifest:
        declared_version = manifest["astrid_version"]
        parsed_declared = _version(declared_version)
        if parsed_declared is None:
            errors.append("pack.yaml: astrid_version must be MAJOR.MINOR.PATCH")
        elif parsed_declared[0] != parsed_host[0] or parsed_declared > parsed_host:
            errors.append(
                f"pack.yaml: astrid_version {declared_version!r} is incompatible with host {host_version!r}"
            )

    available_sdk = frozenset(public_sdk_modules)
    source_files = sorted(path for path in root.rglob("*.py") if not path.is_symlink())
    package_name = root.name
    for source in source_files:
        try:
            text = source.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(source))
        except (OSError, UnicodeError, SyntaxError) as exc:
            errors.append(f"{source.relative_to(root)}: malformed Python source: {exc}")
            continue
        for imported, line, names in _imports(tree):
            found_imports.append(imported)
            if imported.startswith("."):
                level = len(imported) - len(imported.lstrip("."))
                suffix = imported[level:]
                source_module = source.relative_to(root).with_suffix("").as_posix().replace("/", ".")
                is_package_init = source_module == "__init__" or source_module.endswith(".__init__")
                if source_module.endswith(".__init__"):
                    source_module = source_module[: -len(".__init__")]
                elif source_module == "__init__":
                    source_module = ""
                parent = (
                    source_module.split(".")
                    if is_package_init and source_module
                    else source_module.split(".")[:-1]
                )
                ascend = level - 1
                if ascend > len(parent):
                    errors.append(f"{source.relative_to(root)}:{line}: relative import escapes pack root")
                    continue
                target = ".".join(parent[: len(parent) - ascend] + ([suffix] if suffix else []))
                if suffix and not _module_exists(root, target):
                    errors.append(f"{source.relative_to(root)}:{line}: missing module {imported!r}")
                continue

            if imported == "astrid.sdk" or imported.startswith("astrid.sdk."):
                components = imported.split(".")
                private = next((component for component in components[2:] if component.startswith("_")), None)
                if imported == "astrid.sdk":
                    private = next((name for name in names if name.startswith("_")), private)
                if private is not None:
                    errors.append(f"{source.relative_to(root)}:{line}: forbidden private import {imported!r}")
                elif imported != "astrid.sdk" and available_sdk and imported not in available_sdk:
                    errors.append(f"{source.relative_to(root)}:{line}: missing SDK module {imported!r}")
                continue

            if imported == package_name or imported.startswith(package_name + "."):
                local = imported[len(package_name):].lstrip(".")
                if local and not _module_exists(root, local):
                    errors.append(f"{source.relative_to(root)}:{line}: missing module {imported!r}")

    return PackImportReport(root, tuple(sorted(set(found_imports))), tuple(errors))


__all__ = ["PackImportReport", "check_pack_imports"]
