"""Host-owned pins for declared helpers; never constructed from task inputs.

Discovery reads Mach-O load commands, without executing the selected helper.
Only exact installed files and OS shared-cache install names enter the policy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading


class ExecutableAdmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FilePin:
    path: Path
    resolved: Path
    aliases: tuple[Path, ...]
    identity: tuple[int, ...]
    sha256: str

    @classmethod
    def measure(cls, path: Path) -> FilePin:
        try:
            if not path.is_absolute() or any(ord(c) < 32 or ord(c) == 127 for c in str(path)):
                raise ValueError("invalid absolute artifact path")
            resolved = path.resolve(strict=True)
            # Seatbelt also sees the physical-parent spelling before following
            # a library's final symlink (Homebrew opt -> Cellar -> versioned lib).
            aliases = [path]
            current = path.parent.resolve(strict=True) / path.name
            visited = set()
            while current not in visited:
                visited.add(current)
                if current not in aliases:
                    aliases.append(current)
                if not current.is_symlink():
                    break
                target = current.readlink()
                target = target if target.is_absolute() else current.parent / target
                current = target.parent.resolve(strict=True) / target.name
            with resolved.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("artifact is not a regular file")
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                after = os.fstat(stream.fileno())
            identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if (identity(before) != identity(after) or identity(resolved.stat()) != identity(after)
                    or path.resolve(strict=True) != resolved):
                raise ValueError("artifact changed during measurement")
            return cls(path, resolved, tuple(aliases), identity(after), "sha256:" + digest)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ExecutableAdmissionError(f"declared executable artifact unavailable: {path}: {exc}") from exc

    def verify(self) -> None:
        if self.measure(self.path) != self:
            raise ExecutableAdmissionError(f"declared executable artifact replaced: {self.path}")


def _system_library(path: Path) -> bool:
    return path.is_absolute() and any(path.is_relative_to(root) for root in (
        Path("/usr/lib"), Path("/System/Library"))) and ".." not in path.parts


def _load_commands(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    # No shell, user otool, DYLD settings or request environment participates.
    result = subprocess.run(["/usr/bin/otool", "-l", str(path)], capture_output=True,
                            text=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False)
    if result.returncode:
        raise ExecutableAdmissionError(f"cannot inspect declared executable library closure: {path}")
    libraries, rpaths = [], []
    command = ""
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("cmd "):
            command = line[4:]
        elif command in {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB", "LC_LOAD_UPWARD_DYLIB"} and line.startswith("name "):
            libraries.append(line[5:].rsplit(" (offset ", 1)[0])
        elif command == "LC_RPATH" and line.startswith("path "):
            rpaths.append(line[5:].rsplit(" (offset ", 1)[0])
    return tuple(dict.fromkeys(libraries)), tuple(dict.fromkeys(rpaths))


@dataclass(frozen=True)
class ExecutableBinding:
    name: str
    executable: FilePin
    libraries: tuple[FilePin, ...]
    system_libraries: tuple[Path, ...]
    platform_identity: tuple[str, ...]

    def verify(self) -> None:
        if tuple(os.uname()) != self.platform_identity:
            raise ExecutableAdmissionError("declared executable platform identity changed")
        for pin in (self.executable, *self.libraries):
            pin.verify()
        if not os.access(self.executable.path, os.X_OK):
            raise ExecutableAdmissionError(f"declared executable is not executable: {self.name}")

    def evidence(self) -> dict:
        def pin(value):
            return {**asdict(value), "path": str(value.path), "resolved": str(value.resolved),
                    "aliases": [str(p) for p in value.aliases]}
        return {"name": self.name, "executable": pin(self.executable),
                "libraries": [pin(p) for p in self.libraries],
                "system_libraries": [str(p) for p in self.system_libraries],
                "platform_identity": self.platform_identity}


def _bind(name: str, selected: Path) -> ExecutableBinding:
    executable = FilePin.measure(selected)
    if not os.access(selected, os.X_OK):
        raise ExecutableAdmissionError(f"declared executable is not executable: {name}")
    libraries: dict[Path, FilePin] = {}
    system: set[Path] = set()
    visited: set[tuple[Path, tuple[Path, ...]]] = set()

    def expand(value: str, loader: Path) -> Path:
        for token, base in (("@loader_path", loader.parent), ("@executable_path", executable.resolved.parent)):
            if value == token or value.startswith(token + "/"):
                return Path(os.path.normpath(str(base) + value[len(token):]))
        if not value.startswith("/"):
            raise ExecutableAdmissionError(f"unsupported declared executable load path: {value}")
        return Path(value)

    def walk(loader: Path, inherited: tuple[Path, ...]) -> None:
        # Non-Darwin readiness still pins files; the required launch backend
        # independently refuses unsupported platforms before workload effects.
        if sys.platform != "darwin":
            return
        key = (loader, inherited)
        if key in visited:
            return
        visited.add(key)
        names, raw_rpaths = _load_commands(loader)
        rpaths = tuple(dict.fromkeys((*[expand(p, loader) for p in raw_rpaths], *inherited)))
        for value in names:
            if value.startswith("@rpath/"):
                candidates = [p / value[7:] for p in rpaths]
                path = next((p for p in candidates if p.is_file() or _system_library(p)), None)
                if path is None:
                    raise ExecutableAdmissionError(f"unavailable declared executable library: {value}")
            else:
                path = expand(value, loader)
            if _system_library(path):
                system.add(path)
                continue  # OS shared-cache entries need not exist as files.
            pin = FilePin.measure(path)
            if path in libraries and libraries[path] != pin:
                raise ExecutableAdmissionError(f"declared executable library changed: {path}")
            # Revisit only distinct resolved images, keeping aliases in evidence.
            seen = any(p.resolved == pin.resolved for p in libraries.values())
            libraries[path] = pin
            if not seen:
                walk(pin.resolved, rpaths)

    walk(executable.resolved, ())
    binding = ExecutableBinding(name, executable, tuple(libraries[p] for p in sorted(libraries)),
                                tuple(sorted(system)), tuple(os.uname()))
    binding.verify()
    return binding


class ExecutableAdmission:
    """One host launch's search configuration and immutable first selections."""

    def __init__(self, search_path: str):
        self.search_path = search_path
        self._bindings: dict[str, ExecutableBinding] = {}
        self._lock = threading.RLock()

    def require(self, names: tuple[str, ...]) -> tuple[ExecutableBinding, ...]:
        with self._lock:
            result = []
            for name in names:
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", name):
                    raise ExecutableAdmissionError("declared executable requires a simple binary name")
                if name not in self._bindings:
                    selected = shutil.which(name, path=self.search_path)
                    if selected is None or not Path(selected).is_absolute():
                        raise ExecutableAdmissionError(f"declared executable unavailable: {name}")
                    self._bindings[name] = _bind(name, Path(selected))
                binding = self._bindings[name]
                binding.verify()
                result.append(binding)
            return tuple(result)
