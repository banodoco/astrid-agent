"""Canonical raw-source imports for selected pack action workers."""

from __future__ import annotations

import importlib.abc
import importlib.util
import keyword
import os
import re
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from astrid.core.pack.identity import _PackSourceSnapshot


def _frozen_entries(entries):
    return MappingProxyType({key: MappingProxyType(dict(value)) for key, value in entries.items()})


@dataclass(frozen=True)
class _ImportAdmission:
    selected: Mapping[str, Mapping[str, str]]
    relocations: Mapping[str, Mapping[str, str]]


_import_admission: ContextVar[_ImportAdmission | None] = ContextVar("pack_import_admission", default=None)


def _check_pack_import_admission(selected, verified_relocations) -> None:
    """Share one verified result only within the synchronous import admission.

    Public validation and every later install still read the complete tree.
    The finder retains the Python bytes read by this admission, so subsequent
    imports cannot execute a file changed after that content check.
    """
    admission = _import_admission.get()
    if admission is not None and admission.selected == selected and admission.relocations == verified_relocations:
        return
    validate_selected_pack_map(selected, verified_relocations=verified_relocations)


class _PackSourceLoader(importlib.abc.Loader):
    """Compile admitted Python source directly; never consult pack bytecode."""

    def __init__(self, source: Path | None, directory: Path, data: bytes | None) -> None:
        self.source = source
        self.directory = directory
        self.data = data

    def create_module(self, spec):
        return None

    def get_code(self, fullname):
        if self.source is None:
            return compile("", str(self.directory), "exec")
        if self.data is None:
            raise ImportError("Python source was not included in pack admission")
        return compile(self.data, str(self.source), "exec")

    def exec_module(self, module) -> None:
        if self.source is not None:
            module.__file__ = str(self.source)
            exec(self.get_code(module.__name__), module.__dict__)


class _PackSourceFinder(importlib.abc.MetaPathFinder):
    """Resolve the admitted pack map directly from raw source."""

    def __init__(self, selected: Mapping[str, Mapping[str, str]], aliases: Mapping[str, Mapping[str, str]],
                 snapshots: Mapping[str, _PackSourceSnapshot]) -> None:
        self.selected, self.aliases = selected, aliases
        self.snapshots = snapshots

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("astrid.packs."):
            return None
        pack_id = fullname.split(".")[2]
        if pack_id not in self.selected:
            if not (Path(__file__).resolve().parents[2] / "packs" / pack_id).is_dir():
                raise ModuleNotFoundError(f"pack {pack_id!r} was not selected for this execution")
            return None
        root = Path(self.selected[pack_id]["root"])
        snapshot = self.snapshots[str(root)]
        path_names = self.aliases.get(pack_id, {})
        suffix = fullname[len("astrid.packs." + pack_id):].lstrip(".")
        parts = suffix.split(".") if suffix else []
        if any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
            raise ValueError("invalid action module name")
        base = root.joinpath(*(path_names.get(part, part) for part in parts))
        directory = base.resolve()
        if not directory.is_relative_to(root):
            raise ValueError("action import escapes admitted pack root")
        relative = base.relative_to(root).as_posix()
        if relative == ".":
            relative = ""
        source = None
        data = None
        package = relative in snapshot.directories
        if package:
            initializer = f"{relative}/__init__.py" if relative else "__init__.py"
            if initializer in snapshot.sources:
                source = (base / "__init__.py").resolve()
                data = snapshot.sources[initializer]
        else:
            source = Path(str(base) + ".py").resolve()
            data = snapshot.sources.get(relative + ".py")
            if data is None:
                raise ModuleNotFoundError(f"no selected Python source for {fullname!r}")
        if not directory.is_relative_to(root) or (source is not None and not source.is_relative_to(root)):
            raise ValueError("action import escapes admitted pack root")
        loader = _PackSourceLoader(source, directory, data)
        spec = importlib.util.spec_from_loader(fullname, loader, is_package=package)
        if source is not None:
            spec.origin = str(source)
            spec.has_location = True
        if package:
            spec.submodule_search_locations = [str(directory)]
        return spec


def pack_module_path(relative: Path) -> tuple[str, dict[str, str]]:
    """Give manifest paths with punctuation a stable canonical spelling."""
    names, aliases = [], {}
    for part in (*relative.parts[:-1], relative.stem):
        name = part if part.isidentifier() and not keyword.iskeyword(part) else "_path_" + part.encode("utf-8").hex()
        if name != part:
            aliases[name] = part
        names.append(name)
    return ".".join(names), aliases


def validate_selected_pack_map(selected: Mapping[str, Mapping[str, str]], *, verify_digests: bool = True,
                               verified_relocations: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, dict[str, str]]:
    """Validate exact roots and run digests without scanning other collections.

    Relocations are trusted host capture output, keyed by the admitted pack id;
    manifests and ordinary selections cannot grant themselves this authority.
    Their snapshot bytes must still match the capture's authenticated digest.
    """
    from astrid.core.pack.identity import pack_digest

    return _validate_selected_pack_map(selected, verify_digests=verify_digests,
                                       verified_relocations=verified_relocations, digest=pack_digest)


def _validate_selected_pack_map(selected: Mapping[str, Mapping[str, str]], *, verify_digests: bool,
                                verified_relocations: Mapping[str, Mapping[str, str]] | None,
                                digest: Callable[[Path], str]) -> dict[str, dict[str, str]]:
    if not isinstance(selected, Mapping) or not selected:
        raise ValueError("selected pack map must be a nonempty mapping")
    relocations = verified_relocations if verified_relocations is not None else {}
    if not isinstance(relocations, Mapping) or not relocations.keys() <= selected.keys():
        raise ValueError("invalid verified pack relocations")
    result = {}
    for pack_id, entry in sorted(selected.items()):
        if (not isinstance(pack_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", pack_id)
                or not isinstance(entry, Mapping) or set(entry) != {"root", "source_digest"}
                or not isinstance(entry["root"], str) or not isinstance(entry["source_digest"], str)):
            raise ValueError("invalid selected pack map entry")
        root = Path(entry["root"]).resolve(strict=True)
        if not root.is_dir() or root.name != pack_id or str(root) != entry["root"]:
            raise ValueError("selected pack id must match its canonical directory name")
        builtin = Path(__file__).resolve().parents[2] / "packs" / pack_id
        original = root
        if pack_id in relocations:
            relocation = relocations[pack_id]
            if (not isinstance(relocation, Mapping)
                    or set(relocation) != {"original_root", "root", "source_digest"}
                    or any(not isinstance(value, str) for value in relocation.values())
                    or relocation["root"] != str(root)
                    or relocation["source_digest"] != entry["source_digest"]):
                raise ValueError("selected pack differs from verified relocation")
            original = Path(relocation["original_root"]).resolve(strict=True)
            if (not original.is_dir() or original.name != pack_id or original == root
                    or str(original) != relocation["original_root"]):
                raise ValueError("invalid verified pack original root")
            from runtime_protocol import platform

            from astrid.sdk.storage_root import resolve_runtime_data_root

            area = resolve_runtime_data_root() / "runtime" / "pack-captures"
            if (root.parent.parent != area or not platform.is_owner_only(area)
                    or not platform.is_owner_only(root.parent)):
                raise ValueError("verified pack snapshot must have private host custody in the capture area")
        if pack_id == "personal":
            from astrid.personal_packs_root import resolve_personal_packs_root

            personal = resolve_personal_packs_root()
            if personal is None or original != personal / "personal":
                raise ValueError("selected personal pack must use the canonical personal root")
        if builtin.is_dir() and builtin.resolve() != original:
            raise ValueError("external selected pack collides with an in-tree pack")
        if not re.fullmatch(r"[0-9a-f]{64}", entry["source_digest"]):
            raise ValueError("invalid selected pack source digest")
        # A relocation is a host-issued authorization, bound to the digest
        # authenticated during capture. Never skip checking its executable bytes,
        # even for admission callers that defer ordinary source digest checks.
        if (verify_digests or pack_id in relocations) and digest(root) != entry["source_digest"]:
            raise ValueError("admitted capability source digest changed")
        result[pack_id] = dict(entry)
    return result


def selected_pack_environment(selected: Mapping[str, Mapping[str, str]], *,
                              verified_relocations: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, str]:
    """Descendant namespace inputs; never add pack collections to PYTHONPATH."""
    import json

    builtin = Path(__file__).resolve().parents[2] / "packs"
    relocations = verified_relocations if verified_relocations is not None else {}
    if relocations:
        _check_pack_import_admission(selected, relocations)
    # Every snapshot keeps its original collection identity. The raw-source
    # finder binds executable imports to the snapshot; discovery must still
    # see only one owner, including for personal and other external packs.
    roots: dict[str, None] = {}
    for pack_id, entry in selected.items():
        collection = Path(relocations.get(pack_id, {}).get("original_root", entry["root"])).parent
        if collection != builtin:
            roots[str(collection)] = None
    environment = {"ASTRID_PACKS_PATH": os.pathsep.join(roots),
                   "ASTRID_SELECTED_PACK_MAP": json.dumps(selected, sort_keys=True),
                   "ASTRID_VERIFIED_PACK_RELOCATIONS": json.dumps(verified_relocations or {}, sort_keys=True)}
    if relocations:
        from astrid.sdk.storage_root import resolve_runtime_data_root

        # The worker must verify custody against the same host-selected area,
        # even when its ordinary subprocess environment omits root selectors.
        environment["ASTRID_RUNTIME_DATA_ROOT"] = str(resolve_runtime_data_root())
    return environment


def install_pack_imports(pack_id: str, root: Path, path_names: Mapping[str, str] | None = None,
                         *, source_digest: str | None = None,
                         selected_map: Mapping[str, Mapping[str, str]] | None = None,
                         verified_relocations: Mapping[str, Mapping[str, str]] | None = None) -> str:
    """Bind this worker to one canonical source identity for its lifetime.

    Ordinary actions use a fresh interpreter. Declared resident actions keep
    the same immutable captured map until their owner retires the interpreter;
    updates never evict live module objects or rebind an existing finder.
    The host supplies the complete transitive map for this execution.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]*", pack_id) or root.name != pack_id:
        raise ValueError("selected pack id must match its directory name")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("selected pack root is unavailable")
    from astrid.core.pack.identity import _read_pack_tree

    snapshots: dict[str, _PackSourceSnapshot] = {}

    def capture_digest(path: Path) -> str:
        key = str(path)
        if key not in snapshots:
            snapshots[key] = _read_pack_tree(path, retain_sources=True)
        return snapshots[key].digest

    if source_digest is None:
        source_digest = capture_digest(root)
    selected = _validate_selected_pack_map(selected_map if selected_map is not None else {
        pack_id: {"root": str(root), "source_digest": source_digest}}, verify_digests=True,
        verified_relocations=verified_relocations, digest=capture_digest)
    if selected.get(pack_id) != {"root": str(root), "source_digest": source_digest}:
        raise ValueError("selected pack differs from admitted map")
    admission = _ImportAdmission(_frozen_entries(selected), _frozen_entries(verified_relocations or {}))
    token = _import_admission.set(admission)
    try:
        environment = selected_pack_environment(selected, verified_relocations=verified_relocations)
        previous = {name: os.environ.get(name) for name in environment}
        os.environ.update(environment)
        prefix = "astrid.packs." + pack_id
        roots = tuple(Path(item) for item in environment["ASTRID_PACKS_PATH"].split(os.pathsep) if item)
        try:
            import astrid.packs

            astrid.packs._check_collisions(roots, selected=selected, verified_relocations=verified_relocations)
        except ImportError as exc:
            # A refused selection leaves no process state behind (PK-4 composition repair).
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            raise ValueError(str(exc)) from exc
    finally:
        _import_admission.reset(token)
    names = dict(path_names or {})
    for finder in sys.meta_path:
        if isinstance(finder, _PackSourceFinder):
            if finder.selected != selected or finder.aliases.get(pack_id, {}) != names:
                raise ValueError("selected pack changed; start a fresh action worker")
            return prefix
    if any(name == "astrid.packs." + item or name.startswith("astrid.packs." + item + ".")
           for name in sys.modules for item in selected):
        raise ValueError("selected pack was imported before action admission")
    sys.meta_path.insert(0, _PackSourceFinder(admission.selected, _frozen_entries({pack_id: names}),
                                           MappingProxyType(snapshots)))
    return prefix


def pack_command_args(arguments: list[str], pack_id: str, root: Path) -> list[str]:
    """Validate the supported Python command routes before importing pack code."""
    args = list(arguments)
    while args:
        if args[0] in {"-u", "-B"}:
            args.pop(0)
        elif args[:2] == ["-X", "utf8"]:
            del args[:2]
        else:
            break
    if (not args or args[0].startswith("-") and args[0] not in {"-m", "-c"}
            or args[0] in {"-m", "-c"} and len(args) < 2):
        raise ValueError("unsupported selected Python command")
    if args[0] == "-m":
        prefix = "astrid.packs." + pack_id
        if (args[1] != prefix and not args[1].startswith(prefix + ".")
                or any(not part.isidentifier() or keyword.iskeyword(part) for part in args[1].split("."))):
            raise ValueError("external command module is outside selected pack namespace")
    elif args[0] != "-c":
        source = Path(args[0]).resolve()
        if not source.is_relative_to(root) or not source.is_file() or source.suffix != ".py":
            raise ValueError("external command script is outside selected pack")
    return args
