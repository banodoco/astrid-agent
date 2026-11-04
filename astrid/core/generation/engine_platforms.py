"""Linux target spelling and local install locking for the shared runner."""
from __future__ import annotations

from contextlib import contextmanager
import math
import os
import stat
import string
import time

from . import _engine_python_bootstrap as _bootstrap
from .engine_contracts import (CommandRequest, DeadlineExceeded, RecipeValidationError,
                               UnsafePathError, UnsupportedPlatform)
from .engine_runner import _root

_LINUX = frozenset({"linux-x86_64-cu124", "linux-x86_64-cu128", "linux-x86_64-cu130"})
_FIELDS = frozenset({"install_root", "python", "comfy_root", "models_root", "models_config", "host", "port"})


def _deadline(deadline):
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise RecipeValidationError("deadline must be a finite monotonic time")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded("engine deadline exceeded", retryable=True)
    return remaining


def _expand(value, values):
    try:
        for _, field, spec, conversion in string.Formatter().parse(value):
            if field is not None and (field not in _FIELDS or spec or conversion):
                raise ValueError("unsupported placeholder")
        return value.format_map(values)
    except (KeyError, ValueError, AttributeError, IndexError) as exc:
        raise RecipeValidationError("invalid start placeholder") from exc


class LinuxPlatformAdapter:
    def __init__(self, key):
        if key not in _LINUX:
            raise UnsupportedPlatform("unsupported Linux target platform")
        self.key = key

    def validate_root(self, root):
        return _root(root, self.key)

    def join(self, root, *relative):
        root = self.validate_root(root)
        for part in relative:
            if (not isinstance(part, str) or not part or part.startswith("/")
                    or "\\" in part or ":" in part or any(ord(c) < 32 for c in part)
                    or any(p in {"", ".", ".."} for p in part.split("/"))):
                raise UnsafePathError("unsafe target relative path")
            root += "/" + part
        return root

    def venv_python(self, venv_root):
        return self.join(venv_root, "bin/python")

    def uv_executable(self, runtime_root):
        return self.join(runtime_root, "bootstrap/bin/uv")

    def environment(self, recipe, root):
        self._recipe(recipe)
        return _bootstrap.linux_bootstrap_environment(install_root=self.validate_root(root))

    def _recipe(self, recipe):
        if recipe.platform != self.key:
            raise UnsupportedPlatform("recipe and adapter target must match")

    def environment_commands(self, recipe, environment, root):
        self._recipe(recipe)
        # Validate with this adapter's own path rules first, then use the one
        # shared stdlib argv construction that the release handoff also uses.
        self.join(root, environment.relative_path)
        try:
            return _bootstrap.linux_bootstrap_commands(
                install_root=self.validate_root(root), uv_version=environment.uv_version,
                python_version=environment.python_version,
                venv_relative_path=environment.relative_path)
        except ValueError as exc:
            raise RecipeValidationError("invalid environment bootstrap pins") from exc

    def environment_command_env(self, recipe, environment, root, index):
        """Extra public env for environment_commands()[index]; pip --prefix only."""
        self._recipe(recipe)
        return _bootstrap.linux_bootstrap_pip_environment() if index == 0 else {}

    def start_request(self, recipe, root, *, deadline):
        self._recipe(recipe)
        self.validate_root(root)
        if recipe.start.bind_host != "127.0.0.1":
            raise RecipeValidationError("engine bind host must be loopback")
        try:
            primary = next(e for e in recipe.environments if e.name == recipe.primary_environment)
            comfy = next(s for s in recipe.sources if s.name == "comfyui")
        except StopIteration as exc:
            raise RecipeValidationError("missing primary environment or comfyui source") from exc
        values = {"install_root": root, "python": self.venv_python(self.join(root, primary.relative_path)),
                  "comfy_root": self.join(root, comfy.relative_path), "models_root": self.join(root, "models"),
                  "models_config": self.join(root, "runtime/extra_model_paths.yaml"),
                  "host": recipe.start.bind_host, "port": str(recipe.start.port)}
        return CommandRequest(tuple(_expand(v, values) for v in recipe.start.argv),
                              _expand(recipe.start.cwd, values),
                              {k: _expand(v, values) for k, v in recipe.start.env.items()}, deadline)

    @contextmanager
    def local_install_lock(self, root, *, deadline):
        # Import only at the target; no operational Runtime daemon is involved.
        from runtime_protocol import platform

        self.validate_root(root)
        _deadline(deadline)
        dirs = []
        fd = None
        locked = False
        try:
            parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            dirs.append((None, None, parent))
            for component in (*root[1:].split("/"), ".astrid-engine"):
                _deadline(deadline)
                try:
                    os.mkdir(component, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                try:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=parent)
                except OSError as exc:
                    raise UnsafePathError("unsafe lock directory") from exc
                dirs.append((parent, component, child))
                parent = child
            try:
                fd = os.open("install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
            except OSError as exc:
                raise UnsafePathError("unsafe install lock file") from exc
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafePathError("unsafe install lock inode")
            while True:
                _deadline(deadline)
                try:
                    platform.lock_file(fd, exclusive=True, blocking=False)
                    locked = True
                    break
                except BlockingIOError:
                    time.sleep(min(0.05, _deadline(deadline)))
            _deadline(deadline)
            # Refuse replaced directory entries or lock inode before admission.
            try:
                for directory, name, child in dirs[1:]:
                    entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    held = os.fstat(child)
                    if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (held.st_dev, held.st_ino):
                        raise UnsafePathError("lock directory changed")
                entry = os.stat("install.lock", dir_fd=parent, follow_symlinks=False)
                held = os.fstat(fd)
                if (not stat.S_ISREG(entry.st_mode) or held.st_nlink != 1
                        or (entry.st_dev, entry.st_ino) != (held.st_dev, held.st_ino)):
                    raise UnsafePathError("install lock inode changed")
            except OSError as exc:
                raise UnsafePathError("install lock path changed") from exc
            yield None
        finally:
            try:
                if locked:
                    platform.unlock_file(fd)
            finally:
                if fd is not None:
                    os.close(fd)
                for _, _, directory in reversed(dirs):
                    os.close(directory)


def platform_adapter(key):
    if key not in _LINUX:
        raise UnsupportedPlatform("target platform has no supported adapter")
    return LinuxPlatformAdapter(key)
