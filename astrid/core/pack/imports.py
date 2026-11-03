"""Canonical raw-source imports for selected pack action workers."""

from __future__ import annotations

import importlib.abc
import importlib.util
import keyword
import re
import sys
from pathlib import Path
from typing import Mapping


class _PackSourceLoader(importlib.abc.Loader):
    """Compile admitted Python source directly; never consult pack bytecode."""

    def __init__(self, source: Path | None, directory: Path) -> None:
        self.source = source
        self.directory = directory

    def create_module(self, spec):
        return None

    def get_code(self, fullname):
        if self.source is None:
            return compile("", str(self.directory), "exec")
        data = self.source.read_bytes()
        return compile(data, str(self.source), "exec")

    def exec_module(self, module) -> None:
        if self.source is not None:
            module.__file__ = str(self.source)
            exec(self.get_code(module.__name__), module.__dict__)


class _PackSourceFinder(importlib.abc.MetaPathFinder):
    """Resolve only the selected canonical pack, directly from raw source."""

    def __init__(self, prefix: str, root: Path, path_names: Mapping[str, str], source_digest: str) -> None:
        self.prefix, self.root, self.path_names = prefix, root, path_names
        self.source_digest = source_digest

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.prefix and not fullname.startswith(self.prefix + "."):
            if fullname.startswith("astrid.packs."):
                pack_id = fullname.split(".")[2]
                if not (Path(__file__).resolve().parents[2] / "packs" / pack_id).is_dir():
                    raise ModuleNotFoundError(f"pack {pack_id!r} was not selected for this execution")
            return None
        suffix = fullname[len(self.prefix):].lstrip(".")
        parts = suffix.split(".") if suffix else []
        if any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
            raise ValueError("invalid action module name")
        base = self.root.joinpath(*(self.path_names.get(part, part) for part in parts))
        directory = base.resolve()
        source = None
        package = directory.is_dir()
        if package:
            initializer = base / "__init__.py"
            if initializer.exists():
                source = initializer.resolve()
        else:
            source = Path(str(base) + ".py").resolve()
            if not source.is_file():
                raise ModuleNotFoundError(f"no selected Python source for {fullname!r}")
        if not directory.is_relative_to(self.root) or (source is not None and not source.is_relative_to(self.root)):
            raise ValueError("action import escapes admitted pack root")
        loader = _PackSourceLoader(source, directory)
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


def install_pack_imports(pack_id: str, root: Path, path_names: Mapping[str, str] | None = None,
                         *, source_digest: str | None = None) -> str:
    """Bind this single-run worker to one canonical source identity.

    The host creates a fresh interpreter for each action. Keep imported modules
    until that interpreter exits; updates never evict live module objects.
    Dependency maps and descendant propagation are supplied by the host layer.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]*", pack_id) or root.name != pack_id:
        raise ValueError("selected pack id must match its directory name")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("selected pack root is unavailable")
    builtin = Path(__file__).resolve().parents[2] / "packs" / pack_id
    if builtin.is_dir() and builtin.resolve() != root:
        raise ValueError("external selected pack collides with an in-tree pack")
    import astrid.packs

    prefix = "astrid.packs." + pack_id
    # Reuse the namespace's reserved-id/core policy; do not maintain a second list.
    roots = astrid.packs._PACK_ROOTS
    if root != builtin.resolve():
        roots = tuple(dict.fromkeys((*roots, root.parent)))
    try:
        astrid.packs._check_collisions(roots)
    except ImportError as exc:
        raise ValueError(str(exc)) from exc
    if source_digest is None:
        from astrid.core.pack.identity import pack_digest

        source_digest = pack_digest(root)
    names = dict(path_names or {})
    for finder in sys.meta_path:
        if isinstance(finder, _PackSourceFinder) and finder.prefix == prefix:
            if (finder.root != root or finder.path_names != names
                    or finder.source_digest != source_digest):
                raise ValueError("selected pack changed; start a fresh action worker")
            return prefix
    if any(name == prefix or name.startswith(prefix + ".") for name in sys.modules):
        raise ValueError("selected pack was imported before action admission")
    sys.meta_path.insert(0, _PackSourceFinder(prefix, root, names, source_digest))
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
